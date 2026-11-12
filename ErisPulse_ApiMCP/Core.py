"""
ErisPulse-ApiMCP 模块主逻辑

机器人进程内开 REST 桥（框架 HTTP 路由 ``/apimcp/*``，``X-ApiMCP-Token``
鉴权），管理事件订阅与环形缓冲；独立 console script
``erispulse-apimcp``（见 :mod:`ErisPulse_ApiMCP.server`）经 HTTP 消费本桥。

{!--< tips >!--}
1. 配置经嵌套 ``ConfigClass`` 声明，框架自动生成带注释的 ``[ApiMCP]`` 配置节
2. token 必须显式配置（配置文件或环境变量 ``ERISPULSE_APIMCP_TOKEN``）；
   未配置时桥进入「未配置」状态——除 ``health`` 探活（200）外所有端点
   返回 503 + 可读指引，不自动生成临时 token
3. 事件订阅为模块内环形缓冲 + AI 轮询模型（无推送依赖）
4. 路由 / 事件处理器在 ``on_load`` 内注册（框架 owner 上下文），
   卸载时由框架归属权回收 + 本模块显式注销双重清理
{!--< /tips >!--}
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from ErisPulse import sdk
from ErisPulse.Core.Bases import BaseConfig, BaseI18n, BaseModule, I18nKey
from ErisPulse.Core.Bases.module import ModuleMeta
from ErisPulse.Core.i18n import i18n
from ErisPulse.loaders import ModuleLoadStrategy

from .bridge import ApiBridge, BridgeError

__all__ = ["Main", "EventBuffer"]


# ---------------------------------------------------------------------------
# 行为参数（集中声明，注明使用位置与修改影响）
# ---------------------------------------------------------------------------

# REST 路由命名空间：最终对外路径 /apimcp/<action>（见 _register_routes）
_ROUTE_NAMESPACE = "apimcp"
# 事件环形缓冲容量上下限（ConfigClass.event_buffer_size 的收敛区间）
_MIN_BUFFER_SIZE = 10
_MAX_BUFFER_SIZE = 100000
# poll_events 单次返回默认/硬上限（见 Main.poll_events）
_DEFAULT_POLL_LIMIT = 200
_POLL_HARD_LIMIT = 500
# 单条缓冲事件文本快照截断长度（见 Main._sanitize_event）
_EVENT_TEXT_MAX = 2000
# 事件 message 段净化参数（见 Main._sanitize_message / _sanitize_segment）：
# 每段文本类字段截断上限 / 大块字段（file/base64/data/url 类键）替换占位 /
# 段数上限（超出截断并附省略标记）
_MSG_TEXT_FIELD_MAX = 500
_MSG_SEGMENT_LIMIT = 50

# 消息段中大块字段键名（file/base64/data/url 类，字符串值替换为
# "<omitted:len=N>"；dict 值视为容器递归，见 Main._sanitize_field）
_MSG_BULKY_KEY_RE = re.compile(r"(file|base64|data|url)", re.IGNORECASE)

# 活跃事件订阅总数上限（Main.subscribe_events 超出 403 拒绝；
# 防失控客户端无限叠订阅耗尽缓冲与轮询开销）
_MAX_SUBSCRIPTIONS = 32

# REST 路由默认限流（_register_routes 透传 SDK register_http_route 的
# rate_limit 参数，格式 "N/minute"=N 请求/分钟/客户端 IP；SDK 内置
# 内存滑动窗实现，超限返回 429 + Retry-After）。30/min：AI 客户端正常
# 轮询节奏远低于此，异常循环重试会被截断，防打爆机器人事件循环
_ROUTE_RATE_LIMIT = "30/minute"


class EventBuffer:
    """
    事件环形缓冲区（定容 FIFO，容量满后淘汰最旧事件）

    :ivar dropped: 因容量淘汰而丢弃的事件总数（观测溢出用）
    """

    def __init__(self, maxlen: int):
        """
        :param maxlen: 缓冲容量（<=0 视为 1）
        """
        self._maxlen = max(1, int(maxlen))
        self._buf: Deque[Dict[str, Any]] = deque()
        self._dropped = 0
        self._seq = 0

    @property
    def maxlen(self) -> int:
        """缓冲容量"""
        return self._maxlen

    @property
    def dropped(self) -> int:
        """累计淘汰事件数"""
        return self._dropped

    def resize(self, maxlen: int) -> None:
        """
        调整容量（保留现存事件，超限部分从最旧端淘汰）

        :param maxlen: 新容量
        """
        self._maxlen = max(1, int(maxlen))
        while len(self._buf) > self._maxlen:
            self._buf.popleft()
            self._dropped += 1

    def append(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """
        追加事件（自动补 ``seq`` 递增序号与 ``ts`` 时间戳）

        :param event: 事件字典
        :return: 实际入缓冲的事件（含 seq/ts）
        """
        self._seq += 1
        item = dict(event)
        item.setdefault("seq", self._seq)
        item.setdefault("ts", time.time())
        if len(self._buf) >= self._maxlen:
            self._buf.popleft()
            self._dropped += 1
        self._buf.append(item)
        return item

    def after(self, since_ts: float, since_seq: int, limit: int) -> List[Dict[str, Any]]:
        """
        取游标之后的事件（升序）

        游标语义：``(ts, seq)`` 双键，``ts`` 相同（同秒事件）时以 ``seq``
        区分先后；``since_ts=0`` 表示从头取。

        :param since_ts: 游标时间戳
        :param since_seq: 游标序号
        :param limit: 最多返回条数
        :return: 事件列表（缓冲内升序）
        """
        out: List[Dict[str, Any]] = []
        for ev in self._buf:
            ts = float(ev.get("ts") or 0.0)
            seq = int(ev.get("seq") or 0)
            if ts > since_ts or (ts == since_ts and seq > since_seq):
                out.append(ev)
                if len(out) >= limit:
                    break
        return out

    def latest_cursor(self) -> Tuple[float, int]:
        """最新事件游标 ``(ts, seq)``（空缓冲返回 ``(0.0, 0)``）"""
        if not self._buf:
            return (0.0, 0)
        last = self._buf[-1]
        return (float(last.get("ts") or 0.0), int(last.get("seq") or 0))

    def clear(self) -> None:
        """清空缓冲（dropped / seq 计数保留）"""
        self._buf.clear()

    def __len__(self) -> int:
        return len(self._buf)


class Main(BaseModule):
    """
    ApiMCP 模块主类

    对外服务（可经 ``sdk.module.call`` / MCP tool 调用）：
    ``subscribe_events`` / ``unsubscribe`` / ``poll_events``。
    REST 端点实现见 :class:`ErisPulse_ApiMCP.bridge.ApiBridge`。
    """

    MODULE_VERSION = "0.1.0"

    # ==================== 声明式配置 ====================

    @dataclass
    class ConfigClass(BaseConfig):
        """
        ApiMCP 配置

        :ivar token: 访问令牌；**必须显式配置**，留空则桥进入未配置状态
            （除 health 探活外所有端点 503，不自动生成临时 token）
        :ivar allow_module_reload: 是否允许 AI 触发模块重载
        :ivar allow_adapter_reload: 是否允许 AI 触发适配器重载（默认关闭）
        :ivar module_call_whitelist: 可经 MCP 调用的模块/方法白名单（空 = 不限制）
        :ivar send_target_whitelist: 允许发送的目标白名单（空 = 不限制，有越权
            发送风险；条目 ``"user:123"`` 精确匹配，``"group:*"`` 通配 ID）
        :ivar event_types_allowed: 允许订阅的事件类型（空 = 不限制）
        :ivar event_buffer_size: 事件环形缓冲容量
        :ivar max_poll_events: 单次轮询最多返回事件数
        :ivar recent_window_seconds: 跨会话最近消息回溯窗口（秒）
        """

        token: str = field(
            default="",
            metadata={
                "description": "REST 桥访问令牌（请求头 X-ApiMCP-Token）；必须显式配置，留空则桥不启用（health 探活仍 200）",
                "required": False,
                "secret": True,
                "env": "ERISPULSE_APIMCP_TOKEN",
                "ui": {"widget": "password", "group": "security", "order": 1},
            },
        )
        allow_module_reload: bool = field(
            default=True,
            metadata={
                "description": "是否允许 AI 客户端触发模块热重载",
                "ui": {"widget": "switch", "group": "permissions", "order": 2},
            },
        )
        allow_adapter_reload: bool = field(
            default=False,
            metadata={
                "description": "是否允许 AI 客户端触发适配器重载（会断开平台连接，默认关闭）",
                "ui": {"widget": "switch", "group": "permissions", "order": 3},
            },
        )
        module_call_whitelist: List[str] = field(
            default_factory=list,
            metadata={
                "description": "允许经 MCP 调用的模块白名单；条目 '模块名' 放行全部公开方法，'模块名.方法名' 精确放行；空列表不限制",
                "ui": {"widget": "textarea", "group": "permissions", "order": 4},
            },
        )
        send_target_whitelist: List[str] = field(
            default_factory=list,
            metadata={
                "description": "允许发送消息的目标白名单；条目 '类型:ID'（如 'user:123'）精确放行，'类型:*'（如 'group:*'）放行该类型全部 ID；空列表不限制（任何目标均可发送，注意越权风险）",
                "ui": {"widget": "textarea", "group": "permissions", "order": 5},
            },
        )
        event_types_allowed: List[str] = field(
            default_factory=list,
            metadata={
                "description": "允许订阅的事件类型白名单（如 message/notice/request/meta）；空列表不限制",
                "ui": {"widget": "textarea", "group": "events", "order": 6},
            },
        )
        event_buffer_size: int = field(
            default=1000,
            metadata={
                "description": "事件环形缓冲容量（超出淘汰最旧事件）",
                "ui": {"widget": "number", "group": "events", "min": 10, "max": 100000, "order": 7},
            },
        )
        max_poll_events: int = field(
            default=200,
            metadata={
                "description": "events/poll 单次最多返回事件数",
                "ui": {"widget": "number", "group": "events", "min": 1, "max": 500, "order": 8},
            },
        )
        recent_window_seconds: int = field(
            default=3600,
            metadata={
                "description": "messages/recent 不指定会话时的回溯窗口（秒）",
                "ui": {"widget": "number", "group": "events", "min": 60, "max": 86400, "order": 9},
            },
        )
        docs_auto_sync: bool = field(
            default=True,
            metadata={
                "description": "启动与每日检查：本地文档缓存与已安装框架版本不一致时自动按 tag 下载框架仓库 docs/",
                "ui": {"widget": "switch", "group": "docs", "order": 10},
            },
        )
        docs_lang: str = field(
            default="zh-CN",
            metadata={
                "description": "文档语言目录（框架仓库 docs/ 下的语言目录）",
                "ui": {"widget": "select", "options": ["zh-CN", "zh-TW", "en", "ja", "ru"], "group": "docs", "order": 11},
            },
        )
        docs_dir: str = field(
            default="data/ApiMCP/docs",
            metadata={
                "description": "文档缓存目录（按 版本/语言 存放）",
                "ui": {"widget": "text", "group": "docs", "order": 12},
            },
        )
        docs_source: str = field(
            default="",
            metadata={
                "description": "文档来源覆盖：本地 docs/ 目录路径（开发环境零网络），或 zip 直链 URL；留空按框架版本从 GitHub 拉取",
                "ui": {"widget": "text", "group": "docs", "order": 13},
            },
        )
        docs_timeout: int = field(
            default=30,
            metadata={
                "description": "文档下载超时（秒）",
                "ui": {"widget": "number", "group": "docs", "min": 5, "max": 300, "order": 14},
            },
        )

    # ==================== i18n 声明 ====================

    class I18nClass(BaseI18n):
        """ApiMCP 运行时文案（键路径 ApiMCP.<属性名>）"""

        loaded = I18nKey(
            default="ApiMCP 已加载：REST 桥监听于 /apimcp/*（token 鉴权）",
            zh_CN="ApiMCP 已加载：REST 桥监听于 /apimcp/*（token 鉴权）",
            zh_TW="ApiMCP 已載入：REST 橋監聽於 /apimcp/*（token 鑑權）",
            en="ApiMCP loaded: REST bridge at /apimcp/* (token auth)",
            ja="ApiMCP をロードしました：REST ブリッジは /apimcp/*（トークン認証）",
            ru="ApiMCP загружен: REST-мост на /apimcp/* (токен-аутентификация)",
        )
        unloaded = I18nKey(
            default="ApiMCP 已卸载：REST 路由与事件订阅已清理",
            zh_CN="ApiMCP 已卸载：REST 路由与事件订阅已清理",
            zh_TW="ApiMCP 已卸載：REST 路由與事件訂閱已清理",
            en="ApiMCP unloaded: REST routes and event subscriptions cleaned",
            ja="ApiMCP をアンロードしました：REST ルートとイベント購読を解放",
            ru="ApiMCP выгружен: REST-маршруты и подписки очищены",
        )
        token_missing = I18nKey(
            default="ApiMCP.token 未配置，REST 桥处于未配置状态（health 探活仍可用，其余端点返回 503）；请在配置 [ApiMCP] token 或环境变量 ERISPULSE_APIMCP_TOKEN 设置访问令牌",
            zh_CN="ApiMCP.token 未配置，REST 桥处于未配置状态（health 探活仍可用，其余端点返回 503）；请在配置 [ApiMCP] token 或环境变量 ERISPULSE_APIMCP_TOKEN 设置访问令牌",
            zh_TW="ApiMCP.token 未配置，REST 橋處於未配置狀態（health 探活仍可用，其餘端點返回 503）；請在配置 [ApiMCP] token 或環境變數 ERISPULSE_APIMCP_TOKEN 設置訪問令牌",
            en="ApiMCP.token is not set; the REST bridge stays unconfigured (health probe still works, other endpoints return 503). Set [ApiMCP] token or env ERISPULSE_APIMCP_TOKEN",
            ja="ApiMCP.token が未設定のため、REST ブリッジは未構成状態です（health プローブは利用可、他のエンドポイントは 503 を返します）。[ApiMCP] token または環境変数 ERISPULSE_APIMCP_TOKEN を設定してください",
            ru="ApiMCP.token не задан; REST-мост остаётся ненастроенным (проба health работает, остальные эндпоинты возвращают 503). Задайте [ApiMCP] token или переменную ERISPULSE_APIMCP_TOKEN",
        )
        bridge_unconfigured = I18nKey(
            default="ApiMCP 访问令牌未配置，REST 桥未启用。请在配置 [ApiMCP] token=... 或环境变量 ERISPULSE_APIMCP_TOKEN 设置访问令牌，随后热更新配置或重启机器人",
            zh_CN="ApiMCP 访问令牌未配置，REST 桥未启用。请在配置 [ApiMCP] token=... 或环境变量 ERISPULSE_APIMCP_TOKEN 设置访问令牌，随后热更新配置或重启机器人",
            zh_TW="ApiMCP 訪問令牌未配置，REST 橋未啟用。請在配置 [ApiMCP] token=... 或環境變數 ERISPULSE_APIMCP_TOKEN 設置訪問令牌，隨後熱更新配置或重啟機器人",
            en="ApiMCP access token is not configured; the REST bridge is disabled. Set [ApiMCP] token=... or env ERISPULSE_APIMCP_TOKEN, then hot-reload config or restart the bot",
            ja="ApiMCP アクセストークンが未設定のため、REST ブリッジは無効です。[ApiMCP] token=... または環境変数 ERISPULSE_APIMCP_TOKEN を設定し、設定を再読み込みするかボットを再起動してください",
            ru="Токен доступа ApiMCP не настроен; REST-мост отключён. Задайте [ApiMCP] token=... или переменную ERISPULSE_APIMCP_TOKEN, затем перезагрузите конфигурацию или перезапустите бота",
        )
        token_updated = I18nKey(
            default="ApiMCP token 已随配置热更新切换",
            zh_CN="ApiMCP token 已随配置热更新切换",
            zh_TW="ApiMCP token 已隨配置熱更新切換",
            en="ApiMCP token switched by config hot-reload",
            ja="ApiMCP トークンが設定のホットリロードで切り替わりました",
            ru="Токен ApiMCP обновлён горячей перезагрузкой конфигурации",
        )
        token_deactivated = I18nKey(
            default="ApiMCP token 已被清空，REST 桥进入未配置状态（health 探活仍可用，其余端点返回 503）",
            zh_CN="ApiMCP token 已被清空，REST 桥进入未配置状态（health 探活仍可用，其余端点返回 503）",
            zh_TW="ApiMCP token 已被清空，REST 橋進入未配置狀態（health 探活仍可用，其餘端點返回 503）",
            en="ApiMCP token was cleared; the REST bridge is now unconfigured (health probe still works, other endpoints return 503)",
            ja="ApiMCP token が空になったため、REST ブリッジは未構成状態になりました（health プローブは利用可、他のエンドポイントは 503）",
            ru="Токен ApiMCP очищен; REST-мост перешёл в ненастроенное состояние (проба health работает, остальные эндпоинты возвращают 503)",
        )
        handlers_missing = I18nKey(
            default="ApiMCP 桥 handler 缺失: {names}，REST 桥将以 503 拒绝服务（请检查 ErisPulse-ApiMCP 安装完整性）",
            zh_CN="ApiMCP 桥 handler 缺失: {names}，REST 桥将以 503 拒绝服务（请检查 ErisPulse-ApiMCP 安装完整性）",
            zh_TW="ApiMCP 橋 handler 缺失: {names}，REST 橋將以 503 拒絕服務（請檢查 ErisPulse-ApiMCP 安裝完整性）",
            en="ApiMCP bridge handlers missing: {names}; the REST bridge will reject requests with 503 (check your ErisPulse-ApiMCP installation)",
            ja="ApiMCP ブリッジハンドラーが不足しています: {names}。REST ブリッジは 503 で拒否します（ErisPulse-ApiMCP のインストールを確認してください）",
            ru="Отсутствуют обработчики моста ApiMCP: {names}; REST-мост будет отклонять запросы с 503 (проверьте целостность установки ErisPulse-ApiMCP)",
        )
        buffer_overflow_warning = I18nKey(
            default="事件缓冲溢出已淘汰最旧记录，考虑调大 event_buffer_size 或缩短轮询间隔",
            zh_CN="事件缓冲溢出已淘汰最旧记录，考虑调大 event_buffer_size 或缩短轮询间隔",
            zh_TW="事件緩衝溢出已淘汰最舊記錄，考慮調大 event_buffer_size 或縮短輪詢間隔",
            en="Event buffer overflowed and oldest records were dropped; consider increasing event_buffer_size or polling more frequently",
            ja="イベントバッファが溢れて最も古い記録を破棄しました。event_buffer_size を増やすかポーリング間隔を短くしてください",
            ru="Буфер событий переполнен, старейшие записи отброшены; увеличьте event_buffer_size или сократите интервал опроса",
        )
        posture_no_whitelist = I18nKey(
            default="ApiMCP.module_call_whitelist 为空：AI 客户端可调用任意已注册模块的公开方法。不信任的 AI 环境请在 [ApiMCP] 配置收窄白名单",
            zh_CN="ApiMCP.module_call_whitelist 为空：AI 客户端可调用任意已注册模块的公开方法。不信任的 AI 环境请在 [ApiMCP] 配置收窄白名单",
            zh_TW="ApiMCP.module_call_whitelist 為空：AI 客戶端可調用任意已註冊模組的公開方法。不信任的 AI 環境請在 [ApiMCP] 配置收窄白名單",
            en="ApiMCP.module_call_whitelist is empty: AI clients can call any registered module's public methods. Configure the whitelist in [ApiMCP] for untrusted AI environments",
            ja="ApiMCP.module_call_whitelist が空です：AI クライアントは登録済みモジュールの公開メソッドをすべて呼び出せます。信頼できない AI 環境では [ApiMCP] でホワイトリストを設定してください",
            ru="ApiMCP.module_call_whitelist пуст: AI-клиенты могут вызывать публичные методы любого зарегистрированного модуля. Для недоверенных сред настройте белый список в [ApiMCP]",
        )
        posture_reload_enabled = I18nKey(
            default="ApiMCP.allow_module_reload = true：AI 客户端可触发模块热重载（默认姿态）。不信任的 AI 环境请在 [ApiMCP] 设置 allow_module_reload = false",
            zh_CN="ApiMCP.allow_module_reload = true：AI 客户端可触发模块热重载（默认姿态）。不信任的 AI 环境请在 [ApiMCP] 设置 allow_module_reload = false",
            zh_TW="ApiMCP.allow_module_reload = true：AI 客戶端可觸發模組熱重載（默認姿態）。不信任的 AI 環境請在 [ApiMCP] 設置 allow_module_reload = false",
            en="ApiMCP.allow_module_reload = true: AI clients can trigger module hot-reload (default posture). Set allow_module_reload = false in [ApiMCP] for untrusted AI environments",
            ja="ApiMCP.allow_module_reload = true：AI クライアントがモジュールのホットリロードを触发できます（デフォルト姿勢）。信頼できない AI 環境では [ApiMCP] で allow_module_reload = false を設定してください",
            ru="ApiMCP.allow_module_reload = true: AI-клиенты могут запускать горячую перезагрузку модулей (поза по умолчанию). Для недоверенных сред задайте allow_module_reload = false в [ApiMCP]",
        )
        buffer_resized = I18nKey(
            default="ApiMCP 事件缓冲容量调整为 {size}",
            zh_CN="ApiMCP 事件缓冲容量调整为 {size}",
            zh_TW="ApiMCP 事件緩衝容量調整為 {size}",
            en="ApiMCP event buffer resized to {size}",
            ja="ApiMCP イベントバッファ容量を {size} に変更しました",
            ru="Размер буфера событий ApiMCP изменён на {size}",
        )
        event_handler_failed = I18nKey(
            default="ApiMCP 事件缓冲写入失败（忽略，不影响事件分发）：{error}",
            zh_CN="ApiMCP 事件缓冲写入失败（忽略，不影响事件分发）：{error}",
            zh_TW="ApiMCP 事件緩衝寫入失敗（忽略，不影響事件分發）：{error}",
            en="ApiMCP event buffer write failed (ignored, dispatch unaffected): {error}",
            ja="ApiMCP イベントバッファ書き込みに失敗（無視、ディスパッチには影響なし）：{error}",
            ru="Ошибка записи в буфер событий ApiMCP (игнорируется, доставка не затронута): {error}",
        )
        config_updated = I18nKey(
            default="ApiMCP 配置已热更新",
            zh_CN="ApiMCP 配置已热更新",
            zh_TW="ApiMCP 配置已熱更新",
            en="ApiMCP config hot-reloaded",
            ja="ApiMCP 設定をホットリロードしました",
            ru="Конфигурация ApiMCP перезагружена",
        )

    # ==================== 生命周期 ====================

    def __init__(self):
        self.sdk = sdk
        self.logger = sdk.logger.get_child("ApiMCP")
        self._bridge: Optional[ApiBridge] = None
        self._buffer = EventBuffer(1000)
        self._subscriptions: Dict[str, Dict[str, Any]] = {}
        self._token = ""
        self._loaded_at: Optional[float] = None
        self._registered_actions: List[str] = []
        self._event_handler_registered = False
        # on_load 启动自检结果：全部 handler 可解析才置 True（桥据此决定 503）
        self._bridge_ready = False

    @staticmethod
    def get_meta() -> ModuleMeta:
        """模块元信息"""
        return ModuleMeta(
            name="ApiMCP",
            description="MCP 网关：经 MCP（Model Context Protocol）向 AI 客户端开放收发消息、模块调用、模块/适配器管理与事件流观测",
            version=Main.MODULE_VERSION,
            group="工具",
            tags=["mcp", "ai", "bridge", "api"],
            services=[
                {"name": "subscribe_events", "description": "订阅事件类型（'*' 或 message/notice/request/meta 等），订阅后匹配事件进入模块内环形缓冲；返回全部活跃订阅"},
                {"name": "unsubscribe", "description": "按 subscription_id 取消事件订阅，返回剩余全部活跃订阅"},
                {"name": "poll_events", "description": "以 (since_ts, since_seq) 游标增量拉取缓冲事件（可选 subscription_id 按订阅类型集过滤；响应含 dropped 累计淘汰数与活跃订阅）"},
                {"name": "active_subscriptions", "description": "列出当前全部活跃订阅（id + types）"},
            ],
        )

    @staticmethod
    def get_load_strategy() -> ModuleLoadStrategy:
        """启动即加载（REST 桥需随框架 HTTP server 尽早可用）"""
        return ModuleLoadStrategy(lazy_load=False, priority=10)

    def _conf(self) -> "Main.ConfigClass":
        """
        读取当前配置（实时）

        {!--< internal-use >!--}
        测试以实例属性覆写本方法注入内存配置，避免触盘。
        {!--< /internal-use >!--}

        :return: ConfigClass 实例
        """
        return self.cfg

    @staticmethod
    def _msg(key: str, default: str, **params: Any) -> str:
        """
        取译并格式化（统一文案出口）

        SDK 的 :meth:`i18n.t` 仅在命中注册翻译时格式化 ``{参数}``，
        兜底 default 原样返回；本方法补齐兜底路径的格式化，
        确保未注册翻译键（如单测环境）下参数也正确展开。

        :param key: 翻译键（如 ``"ApiMCP.loaded"``）
        :param default: 兜底文本（可含 ``{参数}`` 占位）
        :param params: 占位参数
        :return: 格式化后的文本
        """
        text = i18n.t(key, default=default)
        if params and "{" in text:
            try:
                return text.format(**params)
            except (KeyError, IndexError):
                return text
        return text

    @property
    def module_version(self) -> str:
        """模块版本号"""
        return self.MODULE_VERSION

    @property
    def loaded_at(self) -> Optional[float]:
        """模块本次加载时间戳（uptime 基准）"""
        return self._loaded_at

    @property
    def token(self) -> str:
        """当前生效的 REST 桥 token"""
        return self._token

    async def on_load(self, event: Dict[str, Any]) -> bool:
        """
        模块加载：解析 token → 重建缓冲 → 注册事件处理器与 REST 路由 →
        handler 启动自检 → 全部成功后记录加载时间

        :param event: 加载事件 ``{"module_name": "ApiMCP"}``
        :return: 是否成功
        """
        conf = self._conf()
        self._buffer = EventBuffer(self._clamp_buffer_size(conf.event_buffer_size))
        self._subscriptions.clear()
        self._resolve_token(conf)

        if self._bridge is None:
            self._bridge = ApiBridge(self)
# 文档引擎（自有实现）：只建实例，不做网络动作——首次检索/显式 refresh 时才同步，
# 启动零联网（离线部署不会被文档下载拖慢）
        from .docs import DocsEngine

        self.docs = DocsEngine(self)
        self._register_event_handler()
        self._register_routes()

        # 启动自检：全部 handler 可解析才对外服务（缺失则桥 503，明细进日志）
        missing = self._bridge.missing_handlers()
        if missing:
            self._bridge_ready = False
            self.logger.error(
                self._msg(
                    "ApiMCP.handlers_missing",
                    default="ApiMCP 桥 handler 缺失: {names}，REST 桥将以 503 拒绝服务（请检查 ErisPulse-ApiMCP 安装完整性）",
                    names=", ".join(missing),
                )
            )
        else:
            self._bridge_ready = True

        # 默认姿态风险提示（只告警不改行为）：宽默认是能力取舍，运维应知情
        if not list(getattr(conf, "module_call_whitelist", None) or []):
            self.logger.warning(
                self._msg(
                    "ApiMCP.posture_no_whitelist",
                    default=(
                        "ApiMCP.module_call_whitelist 为空：AI 客户端可调用任意已注册模块的公开方法。"
                        "不信任的 AI 环境请在 [ApiMCP] 配置收窄白名单"
                    ),
                )
            )
        if bool(getattr(conf, "allow_module_reload", True)):
            self.logger.warning(
                self._msg(
                    "ApiMCP.posture_reload_enabled",
                    default=(
                        "ApiMCP.allow_module_reload = true：AI 客户端可触发模块热重载（默认姿态）。"
                        "不信任的 AI 环境请在 [ApiMCP] 设置 allow_module_reload = false"
                    ),
                )
            )

        # uptime 基准只在全部初始化成功后落定（失败中途不留下"已加载"假象）
        self._loaded_at = time.time()
        self.logger.info(self._msg("ApiMCP.loaded", default="ApiMCP 已加载：REST 桥监听于 /apimcp/*（token 鉴权）"))
        return True

    async def on_unload(self, event: Dict[str, Any]) -> bool:
        """
        模块卸载：显式注销 REST 路由，清空订阅与缓冲

        （事件处理器由框架 owner 归属回收兜底清理）

        :param event: 卸载事件 ``{"module_name": "ApiMCP"}``
        :return: 是否成功
        """
        router = getattr(self.sdk, "router", None)
        if router is not None:
            for action in self._registered_actions:
                try:
                    router.unregister_http_route(_ROUTE_NAMESPACE, f"/{action}")
                except Exception:
                    pass
        self._registered_actions.clear()
        self._event_handler_registered = False
        self._bridge_ready = False
        self._subscriptions.clear()
        self._buffer.clear()
        # 生命周期态一并复位：重载后 token 重新解析，未配置告警可再次触发
        self._token = ""
        self.logger.info(self._msg("ApiMCP.unloaded", default="ApiMCP 已卸载：REST 路由与事件订阅已清理"))
        return True

    def on_config_update(self, old_config: Any, new_config: Any) -> None:
        """
        配置热更新：切换 token（含清空 → 桥停用）、重建缓冲容量

        :param old_config: 变更前配置实例
        :param new_config: 变更后配置实例
        """
        conf = self._conf()
        new_size = self._clamp_buffer_size(conf.event_buffer_size)
        if new_size != self._buffer.maxlen:
            self._buffer.resize(new_size)
            self.logger.info(self._msg("ApiMCP.buffer_resized", default="ApiMCP 事件缓冲容量调整为 {size}", size=new_size))

        configured = (getattr(conf, "token", "") or "").strip()
        if configured != self._token:
            was_configured = bool(self._token)
            self._resolve_token(conf)
            if configured and was_configured:
                self.logger.info(self._msg("ApiMCP.token_updated", default="ApiMCP token 已随配置热更新切换"))
            elif not configured and was_configured:
                self.logger.warning(
                    self._msg(
                        "ApiMCP.token_deactivated",
                        default="ApiMCP token 已被清空，REST 桥进入未配置状态（health 探活仍可用，其余端点返回 503）",
                    )
                )
        self.logger.debug(self._msg("ApiMCP.config_updated", default="ApiMCP 配置已热更新"))

    # ==================== token ====================

    def _resolve_token(self, conf: "Main.ConfigClass") -> None:
        """
        解析生效 token：配置值（含环境变量覆盖）→ 为空时进入「未配置」状态

        环境变量覆盖说明：``token`` 字段经 ConfigClass 声明
        ``env: "ERISPULSE_APIMCP_TOKEN"``，框架 env 覆盖在配置读取
        （``dict_to_dataclass``，frame_config 内存合并语义）时已生效——
        本方法在 on_load / on_config_update 拿到的 ``conf.token`` 已是
        「环境变量 > config.toml > 默认值」合并后的最终值，无需重复读 env。

        未配置时**不生成临时 token**（避免泄漏到日志）：桥进入未配置状态，
        除 health 探活（200）外所有端点 503；log.warning 一次性提示
        （文案不含任何 token 值）。

        :param conf: 当前配置实例
        """
        token = (getattr(conf, "token", "") or "").strip()
        if token:
            self._token = token
            return
        was_active = bool(self._token)
        self._token = ""
        if not was_active:
            # 仅在「从配置态 → 未配置态」的首次进入时提示，避免热更新反复刷日志
            self.logger.warning(
                self._msg(
                    "ApiMCP.token_missing",
                    default=(
                        "ApiMCP.token 未配置，REST 桥处于未配置状态（health 探活仍可用，"
                        "其余端点返回 503）；请在配置 [ApiMCP] token 或环境变量 "
                        "ERISPULSE_APIMCP_TOKEN 设置访问令牌"
                    ),
                )
            )

    @staticmethod
    def _clamp_buffer_size(size: Any) -> int:
        """缓冲容量收敛到 [_MIN_BUFFER_SIZE, _MAX_BUFFER_SIZE]"""
        try:
            value = int(size)
        except (TypeError, ValueError):
            value = 1000
        return max(_MIN_BUFFER_SIZE, min(_MAX_BUFFER_SIZE, value))

    # ==================== 事件订阅与缓冲 ====================

    def _register_event_handler(self) -> None:
        """
        注册框架级事件通配处理器（``sdk.adapter.on("*")``）

        注册发生在模块加载 owner 上下文内，卸载时框架归属权回收兜底；
        处理器只做内存判定与追加，不阻塞事件分发。
        """
        if self._event_handler_registered:
            return
        decorator = self.sdk.adapter.on("*")
        decorator(self._on_any_event)
        self._event_handler_registered = True

    async def _on_any_event(self, data: Any) -> None:
        """
        事件通配处理器（热路径）：有活跃订阅且类型匹配时才入缓冲

        :param data: OneBot12 标准事件字典
        """
        try:
            if not self._subscriptions or not isinstance(data, dict):
                return
            etype = str(data.get("type") or "")
            if not etype:
                return
            for sub in self._subscriptions.values():
                types = sub.get("types") or ()
                if etype in types or "*" in types:
                    self._buffer.append(self._sanitize_event(etype, data))
                    return
        except Exception as e:  # noqa: BLE001 —— 观测路径异常不得影响事件分发
            self.logger.debug(
                self._msg(
                    "ApiMCP.event_handler_failed",
                    default="ApiMCP 事件缓冲写入失败（忽略，不影响事件分发）：{error}",
                    error=e,
                )
            )

    @staticmethod
    def _truncate_text(text: str, limit: int) -> str:
        """
        文本截断（超限保留头部并附原长标记）

        :param text: 原文本
        :param limit: 截断上限
        :return: 截断后的文本（未超限原样返回）
        """
        if len(text) <= limit:
            return text
        return text[:limit] + f"...(截断，原长 {len(text)})"

    @staticmethod
    def _sanitize_field(key: str, value: Any) -> Any:
        """
        消息段单字段净化（叶子层规则）

        - str 值且键名命中大块类（file/base64/data/url）→ ``"<omitted:len=N>"``
        - str 值其余键名 → 截断到 ``_MSG_TEXT_FIELD_MAX``
        - dict 值为容器（如 OneBot12 段规范的 ``data``），递归逐字段同规则
        - bytes → ``"<bytes N>B>"`` 占位；其余类型原样

        :param key: 字段名
        :param value: 字段值
        :return: 净化后的字段值
        """
        if isinstance(value, str):
            if _MSG_BULKY_KEY_RE.search(key):
                return f"<omitted:len={len(value)}>"
            return Main._truncate_text(value, _MSG_TEXT_FIELD_MAX)
        if isinstance(value, bytes):
            return f"<bytes {len(value)}B>"
        if isinstance(value, dict):
            return {k: Main._sanitize_field(str(k), v) for k, v in value.items()}
        return value

    @classmethod
    def _sanitize_segment(cls, segment: Dict[str, Any]) -> Dict[str, Any]:
        """
        单个消息段净化：保留 ``type`` 与摘要文本，大块字段替换为占位

        规则（见模块顶部行为参数注释与 :meth:`_sanitize_field`）：
        - ``type`` 始终保留
        - file/base64/data/url 类键名的**字符串值**（base64 / URL / 文件
          内容等大块负载）替换为 ``"<omitted:len=N>"``（保留体量信息便于
          排查）；dict 值视为容器递归，不整体占位

        :param segment: 消息段字典（OneBot12 消息段）
        :return: 净化后的消息段
        """
        out: Dict[str, Any] = {"type": segment.get("type", "")}
        for key, value in segment.items():
            if key == "type":
                continue
            out[key] = cls._sanitize_field(str(key), value)
        return out

    @classmethod
    def _sanitize_message(cls, message: Any) -> Any:
        """
        消息负载净化（防 base64 / 大段数据撑爆缓冲与响应）

        - 字符串消息：截断到 ``_EVENT_TEXT_MAX``
        - 段数组：逐段净化（见 :meth:`_sanitize_segment`），段数超
          ``_MSG_SEGMENT_LIMIT`` 截断并附省略标记段
        - 单段 dict：按单段净化

        :param message: 原始 message 字段（str / 段列表 / 段 dict）
        :return: 摘要形态的消息
        """
        if isinstance(message, str):
            return cls._truncate_text(message, _EVENT_TEXT_MAX)
        if isinstance(message, dict):
            return cls._sanitize_segment(message)
        if isinstance(message, (list, tuple)):
            segments = [
                cls._sanitize_segment(seg) if isinstance(seg, dict) else seg
                for seg in message[:_MSG_SEGMENT_LIMIT]
            ]
            if len(message) > _MSG_SEGMENT_LIMIT:
                segments.append(
                    {
                        "type": "omitted",
                        "count": len(message),
                        "note": f"段数超过 {_MSG_SEGMENT_LIMIT} 已截断（共 {len(message)} 段）",
                    }
                )
            return segments
        return message

    @staticmethod
    def _sanitize_event(etype: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        事件瘦身拷贝：仅保留观测所需字段，超长文本截断，消息负载深度净化

        :param etype: 事件类型
        :param data: 原始事件
        :return: 缓冲用事件字典
        """
        keep: Dict[str, Any] = {"type": etype}
        for key in (
            "platform", "detail_type", "user_id", "group_id", "guild_id",
            "channel_id", "message_id", "self", "time", "sub_type",
        ):
            if key in data:
                keep[key] = data[key]
        if "message" in data:
            keep["message"] = Main._sanitize_message(data["message"])
        # 常见文本快照（纯文本消息时便于 AI 直接读取；超长截断并留标记）
        text = data.get("alt_message") or data.get("text")
        if isinstance(text, str) and text:
            if len(text) > _EVENT_TEXT_MAX:
                keep_len = _EVENT_TEXT_MAX // 2
                text = text[:keep_len] + f"...(截断，原长 {len(text)})..." + text[-keep_len:]
            keep["text"] = text
        return keep

    def subscribe_events(self, types: List[str]) -> Dict[str, Any]:
        """
        订阅事件类型（订阅后匹配事件进入环形缓冲）

        :param types: 事件类型列表（``"*"`` 或 message / notice / request / meta 等）
        :return: ``{"subscription_id": str, "types": [...],
            "active_subscriptions": [{"id", "types"}, ...]}``
        :raises BridgeError: 参数为空、类型不在允许列表或活跃订阅数超
            ``_MAX_SUBSCRIPTIONS``（32）时抛出
        """
        cleaned: List[str] = []
        for item in types or []:
            name = str(item).strip()
            if name and name not in cleaned:
                cleaned.append(name)
        if not cleaned:
            raise BridgeError(400, "订阅失败：'types' 为空或全是空白项，需至少一个事件类型（如 [\"message\"]）")

        allowed = list(getattr(self._conf(), "event_types_allowed", None) or [])
        if allowed:
            denied = [name for name in cleaned if name != "*" and name not in allowed]
            if denied:
                raise BridgeError(
                    403,
                    f"订阅被拒绝：事件类型 {denied} 不在 event_types_allowed 允许列表内。允许: {allowed}",
                )

        if len(self._subscriptions) >= _MAX_SUBSCRIPTIONS:
            active = ", ".join(self.subscription_ids()) or "（无）"
            raise BridgeError(
                403,
                f"活跃订阅数已达上限 {_MAX_SUBSCRIPTIONS}，请先 unsubscribe 部分订阅。当前活跃: {active}",
            )

        sid = f"sub_{secrets.token_hex(4)}"
        self._subscriptions[sid] = {"id": sid, "types": cleaned, "created_ts": time.time()}
        return {
            "subscription_id": sid,
            "types": cleaned,
            "active_subscriptions": self.active_subscriptions(),
        }

    def unsubscribe(self, subscription_id: str) -> bool:
        """
        取消事件订阅

        :param subscription_id: 订阅 ID
        :return: 是否成功（不存在返回 False）
        """
        return self._subscriptions.pop(str(subscription_id), None) is not None

    def subscription_ids(self) -> List[str]:
        """活跃订阅 ID 列表"""
        return list(self._subscriptions.keys())

    def active_subscriptions(self) -> List[Dict[str, Any]]:
        """
        全部活跃订阅（id + types，订阅/取消/轮询响应共用形态）

        :return: ``[{"id": str, "types": [...]}, ...]``（创建顺序）
        """
        return [
            {"id": sub["id"], "types": list(sub["types"])}
            for sub in self._subscriptions.values()
        ]

    def poll_events(
        self,
        since_ts: float = 0.0,
        since_seq: int = 0,
        limit: Optional[int] = None,
        subscription_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        增量拉取缓冲事件（AI 轮询入口）

        可选 ``subscription_id``：按该订阅声明的类型集过滤返回事件
        （缓冲本身全桥共享，过滤只作用于返回视图；未知订阅 ID 抛 404）。

        :param since_ts: 游标时间戳（首次传 0）
        :param since_seq: 游标序号（同秒事件区分用）
        :param limit: 最多返回条数（默认取配置 max_poll_events）
        :param subscription_id: 可选，按订阅类型集过滤
        :return: 含 events / next_since_ts / next_since_seq / dropped
            （缓冲累计淘汰数）/ 溢出统计
        :raises BridgeError: subscription_id 不存在时抛 404
        """
        type_filter = None
        if subscription_id:
            sub = self._subscriptions.get(str(subscription_id))
            if sub is None:
                active = ", ".join(self.subscription_ids()) or "（无）"
                raise BridgeError(
                    404,
                    f"订阅 '{subscription_id}' 不存在。当前活跃订阅: {active}",
                )
            sub_types = set(sub.get("types") or ())
            type_filter = lambda ev: "*" in sub_types or str(ev.get("type") or "") in sub_types  # noqa: E731

        conf_limit = int(getattr(self._conf(), "max_poll_events", _DEFAULT_POLL_LIMIT) or _DEFAULT_POLL_LIMIT)
        effective = conf_limit if limit is None else int(limit)
        effective = max(1, min(effective, _POLL_HARD_LIMIT))
        events = self._buffer.after(float(since_ts or 0.0), int(since_seq or 0), effective)
        if type_filter is not None:
            # 过滤只作用于返回视图：非本订阅类型的事件对该订阅无意义，随游标
            # 跳过；游标推进到最后一条匹配事件，其后的事件留待下次窗口
            events = [ev for ev in events if type_filter(ev)]
        if events:
            last = events[-1]
            next_ts = float(last.get("ts") or 0.0)
            next_seq = int(last.get("seq") or 0)
        else:
            next_ts, next_seq = self._buffer.latest_cursor()
        return {
            "events": events,
            "count": len(events),
            "next_since_ts": next_ts,
            "next_since_seq": next_seq,
            "dropped": self._buffer.dropped,
            "buffer": self.buffer_stats(),
            "active_subscriptions": self.active_subscriptions(),
        }

    def buffer_stats(self) -> Dict[str, Any]:
        """缓冲统计（容量 / 当前条数 / 累计淘汰数）"""
        return {
            "size": len(self._buffer),
            "maxlen": self._buffer.maxlen,
            "dropped": self._buffer.dropped,
        }

    # ==================== REST 路由 ====================

    def _register_routes(self) -> None:
        """
        按 bridge.HANDLERS 端点表注册 REST 路由（全部挂默认限流）

        路由挂载于命名空间 ``apimcp``（对外路径 ``/apimcp/<action>``）；
        注册发生在模块加载 owner 上下文内（框架自动建立 owner → namespace
        归属映射，卸载时兜底回收）。所有路由透传 ``rate_limit``（SDK
        RouterManager 内置限流中间件，30 请求/分钟/客户端 IP，超限 429）。
        """
        router = self.sdk.router
        for action in ApiBridge.HANDLERS:
            handler = self._make_route_handler(action)
            methods = ["GET"] if action == "health" else ["POST"]
            router.register_http_route(
                _ROUTE_NAMESPACE,
                f"/{action}",
                handler,
                methods=methods,
                rate_limit=_ROUTE_RATE_LIMIT,
            )
            self._registered_actions.append(action)

    def _make_route_handler(self, action: str):
        """
        生成单端点路由 handler（闭包绑定 action）

        :param action: 端点名（如 ``"modules/list"``）
        :return: ``async def handler(request) -> (dict, int)``
        """
        bridge = self._bridge

        async def _handler(request):  # noqa: ANN001 —— 框架自动注入 HttpRequest
            return await bridge.handle_request(action, request)

        return _handler
