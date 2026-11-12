"""
REST 桥 handler 实现

把框架 HTTP 路由（``/apimcp/<action>``）上的请求转换为 SDK 调用：
token 校验（``X-ApiMCP-Token`` 请求头，401 拒绝；token 未配置时除
``health`` 探活外一律 503）→ JSON 体解析 → 按 action 分发到对应
handler → 统一响应包裹 ``{"ok": bool, ...}``。

路由语义依赖：本模块经 ``router.register_http_route("apimcp", "/<action>")``
注册路由；框架 RouterManager 把含斜杠的 path 作为**多级字面路径**挂载
（FastAPI ``APIRoute(path="/apimcp/modules/list")``，非单段路径参数），
因此 client 侧请求路径必须与 ``/apimcp/<action>`` 逐字一致。

净化声明：``modules/call`` 等端点的返回净化为**键名级**（密钥类 / 路径类
键名整键丢弃）+ **字符串叶子路径形态**（盘符 / 前导斜杠 / UNC /
``file://``）丢弃 + 含 ``..`` 的字符串值丢弃——不保证剔除业务数据中的
一切敏感内容，深度净化是目标模块自己的责任。

{!--< tips >!--}
1. handler 均为 ``async def handler(self, body: dict)``，只依赖
   ``module.sdk`` / ``module.token`` / 订阅与缓冲接口，便于假 SDK 单测
2. 业务性拒绝（参数缺失 / 白名单外 / 开关关闭 / 体积超限）抛
   :class:`BridgeError` 携带对应 HTTP 状态码，错误信息面向 AI 客户端可读；
   内部异常只回 ``internal: <类型>``，详情进日志
3. 对外输出统一经 ``_jsonify`` / ``sanitize_for_ai`` 净化；
   ``send_message`` / ``recent_messages`` 的返回体只含设计内字段
   （message_id / 会话行摘要等），不承载用户任意数据
{!--< /tips >!--}
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
from typing import Any, Dict, List, Tuple

from ErisPulse.Core.Bases.router import HttpRequest

__all__ = ["ApiBridge", "BridgeError", "TOKEN_HEADER", "sanitize_for_ai"]

# ---------------------------------------------------------------------------
# 行为参数（集中声明，注明使用位置与修改影响）
# ---------------------------------------------------------------------------

# token 校验使用的请求头名（客户端与 stdio server 均按此名传递）
TOKEN_HEADER = "X-ApiMCP-Token"

# recent_messages 跨会话查询时为平台过滤预留的超采样倍数：
# recent(platform=...) 需要先取较多行再按 session_key 前缀过滤
_RECENT_OVERSAMPLE = 3
# recent_messages 单次拉取的硬上限（防止响应体过大）
_RECENT_HARD_LIMIT = 500
# poll_events 单次返回的硬上限
_POLL_HARD_LIMIT = 500
# sanitize_for_ai 递归深度上限（防循环引用栈溢出）
_SANITIZE_MAX_DEPTH = 12

# 密钥类字段名（命中即整键丢弃，不进入拓扑输出）
_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|api_?key|access_?key|authorization|credential|cookie)",
    re.IGNORECASE,
)
# 路径类字段名（**一律丢弃**，不看值形态：相对路径 / 路径变体同样泄露
# 服务器目录结构，见 sanitize_for_ai）
_PATHISH_KEY_RE = re.compile(
    r"^(path|file|file_?path|abs_?path|module_?path|root|root_?dir|base_?dir|dir|directory|source|origin)$",
    re.IGNORECASE,
)
# 字符串叶子的绝对路径形态：Windows 盘符 / 前导斜杠 / UNC / file:// URI
# （值为该形态的字符串字段整键丢弃，见 sanitize_for_ai）
_STRING_PATH_RE = re.compile(r"^([A-Za-z]:[\\/]|[\\/]{2}|/|file://)", re.IGNORECASE)

# 平台原生事件键后缀（如 onebot11_raw），可能包含平台原始鉴权信息，不外发
_RAW_KEY_SUFFIX = "_raw"

# SendDSL 链式修饰方法名（SDK ``Core/Bases/adapter.py`` 的
# ``_CHAIN_MODIFIER_NAMES`` 快照，随 SDK 演进**可能漂移**）——仅作调用前
# 快速拒绝路径；名单漏判的新修饰方法由调用后的 ``_is_awaitable``
# 运行时判定兜底拒绝（见 send_message），名单漂移不构成安全缺口
_CHAIN_MODIFIER_NAMES = frozenset(
    {"To", "At", "AtAll", "Reply", "Build", "Using", "Account", "Sequential"}
)

# recent_messages 会话键格式（与 SDK transcript ``session_key_from_event``
# 产出一致：``平台:detail_type:target_id``）。
# detail_type 取值集核对结论（SDK 源码 Core/transcript.py + Core/adapter.py）：
# 入站仅 message 事件入 transcript，第二段为事件 detail_type，即 SDK 会话类型集
# ``ReceiveType``{private, group, channel, guild, thread, user}；出站 message.sent
# 上下文经 ``_ctx_key`` 产出同形态同集合。适配器经 ``register_custom_type``
# 注册的自定义会话类型默认不在允许集内（如需支持须扩展本集合）
_SESSION_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):([A-Za-z0-9_-]+):([A-Za-z0-9_:-]+)$")
_SESSION_DETAIL_TYPES = frozenset({"private", "group", "user", "channel", "guild", "thread"})

# send_message 目标类型允许集。核对结论（SDK ``Core/Event/session_type.py``）：
# SDK SendDSL 发送类型集 ``SendType``{user, group, channel, guild, thread}；
# ``Send.To`` 额外接受接收类型 "private"（自动转换为 "user"）。其余值在
# SDK 侧被静默归一为 "user"，桥层提前以 400 拒绝避免意图歧义
_SEND_TARGET_TYPES = frozenset({"user", "group", "channel", "guild", "thread", "private"})
# send_message target_id 长度上限（基本格式校验：非空、无控制字符、≤128）
_TARGET_ID_MAX_LEN = 128

# 请求体大小上限（_read_body 解析后按 JSON 序列化字节估算；413 拒绝）
_MAX_BODY_BYTES = 256 * 1024
# send_message content 大小上限（str/bytes 直接计长，其余按 JSON 序列化估算）
_MAX_CONTENT_BYTES = 32 * 1024


class BridgeError(Exception):
    """
    桥层业务错误（携带 HTTP 状态码）

    :ivar status: int HTTP 状态码（403/404/400 等）
    :ivar message: str 面向 AI 客户端的可读错误说明
    """

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def sanitize_for_ai(value: Any, depth: int = 0) -> Any:
    """
    递归净化数据用于 AI 消费

    规则（键名级 + 字符串叶子路径形态，见模块顶部「净化声明」）：
    - 密钥类键名（token/secret/password/api_key/...）整键丢弃
    - 路径类键名（path/file/root/dir/...）**一律丢弃**（不看值形态）
    - 字段值为含 ``..`` 的字符串（路径穿越样貌）整键丢弃
    - 字段值为绝对路径形态字符串（盘符 / 前导 ``/`` / UNC / ``file://``）整键丢弃
    - ``*_raw`` 键（平台原生事件）丢弃
    - bytes 转 ``<bytes N>B`` 占位，set 转 list，其余非 JSON 标量转 str

    :param value: 任意数据（dict / list / 标量）
    :param depth: 递归深度（内部使用，超限原样字符串化）
    :return: 可安全 JSON 序列化的数据
    """
    if depth > _SANITIZE_MAX_DEPTH:
        return str(value)
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if _SECRET_KEY_RE.search(key):
                continue
            if key.endswith(_RAW_KEY_SUFFIX):
                continue
            if _PATHISH_KEY_RE.match(key):
                # 路径类字段名一律丢弃：相对路径 / 路径变体同样泄露目录结构
                continue
            if isinstance(v, str):
                if ".." in v:
                    # 值含路径穿越样貌（..）的字符串字段丢弃
                    continue
                if _STRING_PATH_RE.match(v):
                    # 值为绝对路径形态（盘符/前导斜杠/UNC/file://）的字符串字段丢弃
                    continue
            out[key] = sanitize_for_ai(v, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [sanitize_for_ai(item, depth + 1) for item in value]
    if isinstance(value, bytes):
        return f"<bytes {len(value)}B>"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class ApiBridge:
    """
    ErisPulse ApiMCP REST 桥

    每个端点对应一个 handler 方法；``Core.Main.on_load`` 按
    :data:`ApiBridge.HANDLERS` 端点表把 ``apimcp/<action>`` 路由挂到框架
    HTTP server，并在启动时自检全部 handler 可解析（缺失则桥返回 503）。
    """

    def __init__(self, module: Any):
        """
        :param module: 宿主 :class:`ErisPulse_ApiMCP.Core.Main` 实例
            （提供 ``sdk`` / ``logger`` / ``token`` / 订阅与缓冲接口）
        """
        self.module = module
        self.sdk = module.sdk
        self.logger = module.logger
        # poll_events 溢出告警的增量基准（上次轮询时的累计淘汰数，见 poll_events）
        self._last_poll_dropped = 0

    # ==================== 请求层 ====================

    def _token_configured(self) -> bool:
        """宿主模块是否已配置 token（未配置 → 桥未启用，见 handle_request）"""
        return bool(str(getattr(self.module, "token", "") or "").strip())

    def _unconfigured_hint(self) -> str:
        """token 未配置时的可读指引文案（i18n，不含任何 token 值）"""
        return self.module._msg(
            "ApiMCP.bridge_unconfigured",
            default=(
                "ApiMCP 访问令牌未配置，REST 桥未启用。"
                "请在配置 [ApiMCP] token=... 或环境变量 ERISPULSE_APIMCP_TOKEN "
                "设置访问令牌，随后热更新配置或重启机器人"
            ),
        )

    def missing_handlers(self) -> List[str]:
        """
        自检：列出无法解析的 handler（on_load 启动时调用）

        :return: 缺失 handler 的端点名列表（空列表 = 全部正常）
        """
        return [
            action
            for action, method_name in self.HANDLERS.items()
            if not callable(getattr(self, method_name, None))
        ]

    def _token_from(self, request: HttpRequest) -> str:
        """从请求头提取 token（starlette Headers 大小写不敏感）"""
        try:
            headers = request.headers or {}
        except Exception:
            return ""
        getter = getattr(headers, "get", None)
        if callable(getter):
            return str(getter(TOKEN_HEADER) or "")
        return ""

    def _check_token(self, request: HttpRequest) -> bool:
        """
        token 鉴权（恒时比较，防时序侧信道）

        两侧统一 ``.encode("utf-8")`` 后用 bytes 版
        :func:`hmac.compare_digest`——str 版对非 ASCII 输入抛
        ``TypeError``（客户端可能携带任意头值）。

        :param request: HTTP 请求
        :return: 鉴权是否通过
        """
        expected = str(getattr(self.module, "token", "") or "")
        if not expected:
            return False
        provided = self._token_from(request)
        return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))

    async def _read_body(self, request: HttpRequest) -> Dict[str, Any]:
        """
        解析 JSON 请求体（GET / 空体视为 ``{}``；体积超限 413 拒绝）

        体积按解析结果的 JSON 序列化字节数估算（框架请求层不暴露原始
        body 字节流的统一接口，估算与真实 body 大小同数量级）。

        :param request: HTTP 请求
        :return: 请求体字典
        :raises BridgeError: 请求体不是 JSON 对象时抛 400；超限时抛 413
        """
        try:
            raw = await request.json()
        except Exception:
            return {}
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise BridgeError(400, "请求体必须是 JSON 对象（{\"key\": value}）")
        try:
            size = len(json.dumps(raw, ensure_ascii=False, default=str).encode("utf-8"))
        except Exception:
            size = _MAX_BODY_BYTES + 1
        if size > _MAX_BODY_BYTES:
            raise BridgeError(
                413,
                f"请求体过大（约 {size} 字节，上限 {_MAX_BODY_BYTES}）。"
                f"请缩小请求体；发送大文件请改用平台 URL 引用而非内联 base64",
            )
        return raw

    async def handle_request(self, action: str, request: HttpRequest) -> Tuple[Dict[str, Any], int]:
        """
        统一入口：未配置门禁 → 鉴权 → 解析 → 分发 → 包裹响应

        token 未配置时桥处于「未启用」状态：除 ``health`` 探活端点
        （返回 200 与未配置状态，用于区分「桥未配置」与「机器人不在线」）
        外所有端点返回 503 + 可读指引。鉴权及后续步骤全部包在 try 内——
        任何异常（含非 ASCII 请求头等输入层意外）都以桥层 JSON 兜底，
        不会穿透成框架 500 HTML 页面。

        :param action: 端点名（如 ``"modules/list"``）
        :param request: HTTP 请求
        :return: ``(响应体, HTTP 状态码)`` 元组（框架元组约定）
        """
        if not self._token_configured():
            if action == "health":
                # 探活端点保持 200：客户端据此区分「桥未配置」（在线但未启用）
                # 与「机器人不在线」（连接失败）；字段名避开 token 前缀，否则
                # 会被 sanitize_for_ai 的密钥类键名规则丢弃
                return {
                    "ok": True,
                    "data": {
                        "status": "unconfigured",
                        "auth_configured": False,
                        "message": self._unconfigured_hint(),
                    },
                }, 200
            return {"ok": False, "error": self._unconfigured_hint()}, 503
        try:
            if not self._check_token(request):
                return {"ok": False, "error": "鉴权失败：缺少或错误的 X-ApiMCP-Token 请求头"}, 401
            if not getattr(self.module, "_bridge_ready", False):
                # on_load 启动自检发现 handler 缺失（安装损坏 / 类被裁剪），
                # 拒绝服务并提示查日志（缺失明细只进 logger.error）
                return {
                    "ok": False,
                    "error": "REST 桥内部不完整：handler 缺失，详见机器人运行日志",
                }, 503
            body = await self._read_body(request)
            method_name = self.HANDLERS.get(action)
            if method_name is None:
                raise BridgeError(404, f"未知端点: /apimcp/{action}")
            handler = getattr(self, method_name, None)
            if not callable(handler):
                # 延迟解析兜底：on_load 已自检，正常运行不可达
                raise BridgeError(503, "REST 桥内部不完整：handler 缺失，详见机器人运行日志")
            data = await handler(body)
            return {"ok": True, "data": self._jsonify(data)}, 200
        except BridgeError as e:
            return {"ok": False, "error": e.message}, e.status
        except Exception as e:  # noqa: BLE001 —— 桥层兜底：任何异常都以可读 JSON 回给客户端
            # 完整 repr 与 traceback 只进日志；对客户端仅暴露异常类型（不外泄内部细节）
            self.logger.error(
                f"ApiMCP 处理 /apimcp/{action} 失败: {type(e).__name__}: {e!r}",
                exc_info=True,
            )
            return {"ok": False, "error": f"internal: {type(e).__name__}"}, 500

    def _jsonify(self, value: Any) -> Any:
        """响应净化（密钥 / 路径不外泄；非 JSON 类型安全转换）"""
        return sanitize_for_ai(value)

    @staticmethod
    def _require_str(body: Dict[str, Any], key: str, what: str) -> str:
        """
        提取必填字符串参数

        :param body: 请求体
        :param key: 参数名
        :param what: 参数的人类可读说明（用于报错）
        :return: 去除首尾空白后的字符串
        :raises BridgeError: 缺失或为空时抛 400
        """
        value = body.get(key)
        if value is None or str(value).strip() == "":
            raise BridgeError(400, f"缺少必填参数 '{key}'（{what}），请在请求体 JSON 中提供")
        return str(value).strip()

    @staticmethod
    def _clamp(value: int, low: int, high: int) -> int:
        """数值收敛到 [low, high] 区间"""
        return max(low, min(high, int(value)))

    @staticmethod
    def _num_param(
        body: Dict[str, Any],
        key: str,
        what: str,
        cast: Any,
        default: Any = None,
    ) -> Any:
        """
        提取可选数值参数（容错 LLM 传字符串数字，如 ``"50"``）

        缺失 / 空白字符串返回 default；字符串数字（"50" / "1.5"）按
        ``cast``（int / float）转换；布尔值与非数字字符串以 400 可读
        错误拒绝。

        :param body: 请求体
        :param key: 参数名
        :param what: 参数的人类可读说明（用于报错）
        :param cast: 转换函数（int / float）
        :param default: 缺省值
        :return: 转换后的数值（或 default）
        :raises BridgeError: 值不可转换为数字时抛 400
        """
        raw = body.get(key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return default
        if isinstance(raw, bool):
            raise BridgeError(400, f"参数 '{key}' 须为数字（{what}），收到: {raw!r}")
        try:
            return cast(raw)
        except (TypeError, ValueError):
            raise BridgeError(
                400,
                f"参数 '{key}' 须为数字（{what}），收到: {str(raw)[:50]}",
            ) from None

    # ==================== 模块管理 ====================

    async def list_modules(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """列出已注册模块及状态摘要"""
        names = list(self.sdk.module.list_registered())
        summary = (self.sdk.module.get_status_summary() or {}).get("modules", {})
        rows = []
        for name in names:
            entry = summary.get(name) or {}
            rows.append(
                {
                    "name": name,
                    "status": entry.get("status", "unknown"),
                    "enabled": bool(entry.get("enabled", True)),
                    "failed": bool(entry.get("failed", False)),
                    "stale": entry.get("stale"),
                }
            )
        return {"modules": rows, "count": len(rows)}

    async def get_module_info(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """查询单个模块的注册信息、元信息与可调用服务清单（含签名/描述）

        services 来自框架 services() 目录（方法名 + 参数签名 + 描述）——
        外部 AI 调 call_module 前先看这里，不用猜参数形态。"""
        name = self._require_str(body, "name", "模块名")
        info = self.sdk.module.get_info(name)
        meta = None
        try:
            meta = self.sdk.module.get_meta(name)
        except Exception:
            meta = None
        if info is None and meta is None:
            registered = ", ".join(self.sdk.module.list_registered()) or "（无）"
            raise BridgeError(
                404,
                f"模块 '{name}' 未注册。当前已注册模块: {registered}",
            )
        services: List[Dict[str, Any]] = []
        try:
            catalog = self.sdk.module.services() or {}
            for entry in catalog.get(name) or []:
                if isinstance(entry, dict) and entry.get("name"):
                    services.append({
                        "name": entry.get("name"),
                        "signature": str(entry.get("signature") or ""),
                        "description": str(entry.get("description") or ""),
                    })
        except Exception:
            pass
        return {"name": name, "info": info, "meta": meta, "services": services}

    async def call_module(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """
        跨模块调用（受 module_call_whitelist 白名单约束）

        RPC 超时：请求字段 ``rpc_timeout``（秒，正数）透传给
        ``sdk.module.call`` 的原生 ``timeout`` 形参（仅对协程方法生效，
        见 SDK ModuleManager.call 文档）。kwargs 中的 ``timeout`` 会被
        ``module.call`` 自身形参吞噬、无法到达目标方法——桥层将其剔除
        并在响应 ``note`` 中提示改用 ``rpc_timeout``。
        """
        name = self._require_str(body, "name", "目标模块名")
        method = self._require_str(body, "method", "目标方法名")
        if method.startswith("_"):
            raise BridgeError(403, f"禁止调用私有方法: {name}.{method}（下划线开头）")

        whitelist = list(getattr(self.module._conf(), "module_call_whitelist", None) or [])
        if whitelist and not self._whitelist_allows(whitelist, name, method):
            raise BridgeError(
                403,
                f"调用 {name}.{method} 被拒绝：不在 module_call_whitelist 白名单内。"
                f"当前白名单: {whitelist}（条目格式为 '模块名' 放行该模块全部公开方法，"
                f"或 '模块名.方法名' 精确放行）",
            )

        args = body.get("args")
        if args is None:
            args = []
        if not isinstance(args, list):
            raise BridgeError(400, "参数 'args' 必须是数组（按位置传参）")
        kwargs = body.get("kwargs")
        if kwargs is None:
            kwargs = {}
        if not isinstance(kwargs, dict):
            raise BridgeError(400, "参数 'kwargs' 必须是对象（按关键字传参）")

        # rpc_timeout：独立请求字段（正数秒数），走 SDK module.call 原生超时
        rpc_timeout = None
        raw_rpc_timeout = body.get("rpc_timeout")
        if raw_rpc_timeout is not None and str(raw_rpc_timeout).strip() != "":
            rpc_timeout = self._num_param(body, "rpc_timeout", "RPC 超时秒数", float)
            if rpc_timeout <= 0:
                raise BridgeError(400, "参数 'rpc_timeout' 须为正数（秒）")

        # kwargs.timeout 剔除：被 sdk.module.call 形参占用，透传必被吞噬
        timeout_note = None
        if "timeout" in kwargs:
            kwargs.pop("timeout")
            timeout_note = (
                "timeout 参数不支持透传（被 SDK module.call 形参占用），已从 kwargs 剔除；"
                "RPC 超时请用请求字段 rpc_timeout"
            )

        call_kwargs = dict(kwargs)
        if rpc_timeout is not None:
            call_kwargs["timeout"] = rpc_timeout
        try:
            result = await self.sdk.module.call(name, method, *args, **call_kwargs)
        except Exception as e:
            # 被调模块异常文本可能含路径 / SQL / 凭据 URL——只回异常类型，
            # 完整 repr 进日志
            self.logger.error(
                f"ApiMCP modules/call {name}.{method} 失败: {type(e).__name__}: {e!r}",
                exc_info=True,
            )
            raise BridgeError(
                400,
                f"调用 {name}.{method} 失败: {type(e).__name__}（详情见机器人日志）",
            ) from e
        response: Dict[str, Any] = {"module": name, "method": method, "result": result}
        if timeout_note:
            response["note"] = timeout_note
        return response

    @staticmethod
    def _whitelist_allows(whitelist: List[str], name: str, method: str) -> bool:
        """
        白名单判定：'模块名' 放行该模块全部公开方法，'模块名.方法名' 精确放行

        :param whitelist: 白名单条目列表
        :param name: 目标模块名
        :param method: 目标方法名
        :return: 是否放行
        """
        for entry in whitelist:
            entry = str(entry).strip()
            if not entry:
                continue
            if entry == name:
                return True
            if entry == f"{name}.{method}":
                return True
        return False

    async def reload_module(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """重载模块（受 allow_module_reload 开关约束）"""
        if not bool(getattr(self.module._conf(), "allow_module_reload", True)):
            raise BridgeError(
                403,
                "模块重载已被禁用：配置 allow_module_reload = false。"
                "如需开启请在 config.toml 的 [ApiMCP] 节设置为 true 后热更新生效",
            )
        name = self._require_str(body, "name", "模块名")
        ok = await self.sdk.module.reload(name)
        if not ok:
            raise BridgeError(500, f"模块 '{name}' 重载失败，详见机器人运行日志")
        return {"module": name, "reloaded": True}

    # ==================== 适配器管理 ====================

    async def list_adapters(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """列出已注册适配器（平台）及状态摘要"""
        platforms = list(self.sdk.adapter.list_registered())
        summary = (self.sdk.adapter.get_status_summary() or {}).get("adapters", {})
        rows = []
        for platform in platforms:
            entry = summary.get(platform) or {}
            bots = entry.get("bots") or {}
            online = [bot_id for bot_id, info in bots.items()
                      if isinstance(info, dict) and info.get("status") == "online"]
            rows.append(
                {
                    "platform": platform,
                    "status": entry.get("status", "unknown"),
                    "enabled": bool(entry.get("enabled", True)),
                    "bots_total": len(bots),
                    "bots_online": online,
                }
            )
        return {"adapters": rows, "count": len(rows)}

    async def get_adapter_status(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """查询单个适配器（平台）的运行状态与 Bot 概况"""
        platform = self._require_str(body, "platform", "平台名")
        summary = (self.sdk.adapter.get_status_summary() or {}).get("adapters", {})
        entry = summary.get(platform)
        if entry is None:
            registered = ", ".join(self.sdk.adapter.list_registered()) or "（无）"
            raise BridgeError(
                404,
                f"平台 '{platform}' 未注册。当前已注册平台: {registered}",
            )
        return {"platform": platform, "status": entry}

    async def reload_adapter(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """重载适配器（受 allow_adapter_reload 开关约束，默认关闭）"""
        if not bool(getattr(self.module._conf(), "allow_adapter_reload", False)):
            raise BridgeError(
                403,
                "适配器重载默认禁用（重载会断开平台连接，风险较高）。"
                "如需开启请在 config.toml 的 [ApiMCP] 节设置 allow_adapter_reload = true",
            )
        platform = self._require_str(body, "platform", "平台名")
        ok = await self.sdk.adapter.reload(platform)
        if not ok:
            raise BridgeError(500, f"适配器 '{platform}' 重载失败，详见机器人运行日志")
        return {"platform": platform, "reloaded": True}

    # ==================== 消息 ====================

    async def send_message(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """
        经平台适配器发送消息（SendDSL 转发）

        目标校验（多层）：
        1. ``target_type`` 限定枚举（见 ``_SEND_TARGET_TYPES``，依据 SDK
           ``SendType`` 集合 + ``To`` 的 private 自动转换）
        2. ``target_id`` 基本格式（非空 / ≤128 字符 / 无控制字符）
        3. ``content`` 体积 ≤ ``_MAX_CONTENT_BYTES``（32KB，413 拒绝）
        4. ``send_target_whitelist`` 白名单（空 = 不限；条目 ``"user:123"``
           精确匹配，``"group:*"`` 通配 target_id）
        """
        platform = self._require_str(body, "platform", "平台名，如 onebot11")
        target_type = self._require_str(body, "target_type", "目标类型，如 user / group")
        target_id = self._require_str(body, "target_id", "目标 ID")
        method = self._require_str(body, "method", "发送方法名，如 Text / Image / Voice")

        # (a) 目标类型枚举：SDK SendType 集 + private（To 自动转 user）
        if target_type not in _SEND_TARGET_TYPES:
            raise BridgeError(
                400,
                f"非法 target_type '{target_type}'。允许值: {sorted(_SEND_TARGET_TYPES)}"
                f"（SDK SendDSL 发送类型集；private 会自动转换为 user）",
            )
        # (b) 目标 ID 基本格式：非空（_require_str 已保证）、长度与控制字符
        if len(target_id) > _TARGET_ID_MAX_LEN or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in target_id):
            raise BridgeError(
                400,
                f"非法 target_id：须为非空、无控制字符且长度不超过 {_TARGET_ID_MAX_LEN} 的字符串",
            )
        if method.startswith("_") or method in _CHAIN_MODIFIER_NAMES:
            raise BridgeError(400, f"非法发送方法 '{method}'（私有或链式修饰方法）")
        if "content" not in body:
            raise BridgeError(400, "缺少必填参数 'content'（消息内容：Text 为文本，Image/Voice/Video/File 为 URL 或 base64）")

        # content 体积上限：str/bytes 按 UTF-8 字节计长，其余按 JSON 序列化估算
        content = body.get("content")
        if isinstance(content, str):
            content_size = len(content.encode("utf-8"))
        elif isinstance(content, bytes):
            content_size = len(content)
        else:
            try:
                content_size = len(json.dumps(content, ensure_ascii=False, default=str).encode("utf-8"))
            except Exception:
                content_size = _MAX_CONTENT_BYTES + 1
        if content_size > _MAX_CONTENT_BYTES:
            raise BridgeError(
                413,
                f"消息内容过大（约 {content_size} 字节，上限 {_MAX_CONTENT_BYTES}）。"
                f"发送大文件请改用平台 URL 引用而非内联 base64",
            )

        # (c) 发送目标白名单：空 = 不限（保持现能力，风险见 README 安全节）
        whitelist = list(getattr(self.module._conf(), "send_target_whitelist", None) or [])
        if whitelist and not self._send_target_allowed(whitelist, target_type, target_id):
            raise BridgeError(
                403,
                f"发送目标 {target_type}:{target_id} 被拒绝：不在 send_target_whitelist "
                f"白名单内。白名单条目格式为 '类型:ID'（如 'user:123'）或 '类型:*' "
                f"（放行该类型全部 ID），当前白名单: {whitelist}",
            )

        adapter = self.sdk.adapter.get(platform)
        if adapter is None:
            registered = ", ".join(self.sdk.adapter.list_registered()) or "（无）"
            raise BridgeError(
                404,
                f"平台 '{platform}' 未注册或未启用。当前已注册平台: {registered}",
            )

# 多账户：account 给定时经 Using(account) 指定发送账户（多 bot 部署下缺省会落
# 适配器默认账户——张冠李戴问题与 QvQChat 投递层同款解法；Using 不可用时忽略）
        account = str(body.get("account") or "").strip()
        dsl = adapter.Send
        if account:
            using = getattr(dsl, "Using", None)
            if callable(using):
                dsl = using(account)
        dsl = dsl.To(target_type, str(target_id))
        try:
            send_fn = getattr(dsl, method)
        except AttributeError:
            available = ", ".join(self._list_sends(dsl))
            raise BridgeError(
                400,
                f"平台 '{platform}' 不支持发送方法 '{method}'。可用方法: {available}",
            ) from None

# 签名预检：方法除 content 外还有必填参数时提前拒绝并列出（QvQChat 同款
# needs_params 引导——不发不入队，比 TypeError 栈对 AI 客户端可读得多）
        import inspect as _inspect

        try:
            sig = _inspect.signature(send_fn)
            required = [
                p.name for p in sig.parameters.values()
                if p.kind in (_inspect.Parameter.POSITIONAL_ONLY, _inspect.Parameter.POSITIONAL_OR_KEYWORD)
                and p.default is _inspect.Parameter.empty
            ]
            missing = [p for p in required if p != "content" and p != "text"]
            if missing:
                raise BridgeError(
                    400,
                    f"发送方法 '{method}' 需要额外参数: {missing}（本次仅提供了 content）。"
                    f"请改用支持该参数的调用方式（如 call_module 直调适配器 API）或换用单参数方法",
                )
        except (TypeError, ValueError):
            pass  # 内建/C 扩展方法无签名可查：放行走运行时判定

        result = send_fn(content)
        # 运行时判定兜底：名单（_CHAIN_MODIFIER_NAMES）漏判的链式修饰方法
        # 返回 Self 而非 Awaitable，此处统一拒绝，避免静默无发送
        if not _is_awaitable(result):
            raise BridgeError(
                400,
                f"发送方法 '{method}' 未产生发送动作（可能为链式修饰方法）。"
                f"可用方法: {', '.join(self._list_sends(dsl))}",
            )
        message_id = await result
        return {
            "platform": platform,
            "target": f"{target_type}:{target_id}",
            "method": method,
            "message_id": message_id,
        }

    @staticmethod
    def _send_target_allowed(whitelist: List[str], target_type: str, target_id: str) -> bool:
        """
        发送目标白名单判定

        :param whitelist: 白名单条目列表（``"类型:ID"`` 或 ``"类型:*"``）
        :param target_type: 目标类型（user / group / ...）
        :param target_id: 目标 ID
        :return: 是否放行
        """
        for entry in whitelist:
            entry = str(entry).strip()
            if not entry or ":" not in entry:
                continue
            entry_type, _, entry_id = entry.partition(":")
            if entry_type.strip() != target_type:
                continue
            entry_id = entry_id.strip()
            if entry_id == "*" or entry_id == target_id:
                return True
        return False

    @staticmethod
    def _list_sends(dsl: Any) -> List[str]:
        """列出 DSL 可用发送方法（适配器实现差异兜底）"""
        try:
            return list(dsl.list_sends())
        except Exception:
            return ["Text", "Image", "Voice", "Video", "File"]

    async def recent_messages(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """
        查询最近消息（经 transcript，可按平台 / 会话过滤）

        ``session`` 强制格式校验（防越权跨会话枚举）：必须匹配 SDK
        transcript ``session_key_from_event`` 的产出形态
        ``平台:detail_type:target_id``（详见 ``_SESSION_KEY_RE`` /
        ``_SESSION_DETAIL_TYPES`` 处的核对结论注释），格式非法返回 400；
        ``platform`` 给出时叠加前缀归属校验，不匹配同样 400。
        """
        limit = self._clamp(
            self._num_param(body, "limit", "最多返回条数", int, default=50), 1, _RECENT_HARD_LIMIT
        )
        platform = body.get("platform")
        session = body.get("session")
        transcript = self.sdk.transcript
        window = int(getattr(self.module._conf(), "recent_window_seconds", 3600) or 3600)

        if session is not None and str(session).strip():
            key = str(session).strip()
            match = _SESSION_KEY_RE.match(key)
            if match is None or match.group(2) not in _SESSION_DETAIL_TYPES:
                raise BridgeError(
                    400,
                    f"参数 'session' 格式非法: '{key}'。会话键格式为 '平台:类型:ID'"
                    f"（如 onebot11:group:123456），类型允许值: {sorted(_SESSION_DETAIL_TYPES)}",
                )
            platform_name = str(platform).strip() if platform else ""
            if platform_name and not key.startswith(f"{platform_name}:"):
                raise BridgeError(
                    400,
                    f"会话键 '{key}' 不属于平台 '{platform_name}'（会话键格式: 平台:类型:ID）",
                )
            rows = transcript.get(key, limit)
            return {"messages": rows, "count": len(rows), "session": key}

        rows = transcript.recent(window, limit=min(_RECENT_HARD_LIMIT, limit * _RECENT_OVERSAMPLE))
        if platform and str(platform).strip():
            prefix = f"{str(platform).strip()}:"
            rows = [r for r in rows if str(r.get("session_key", "")).startswith(prefix)]
        rows = rows[-limit:]
        return {"messages": rows, "count": len(rows), "window_seconds": window}

    # ==================== 事件订阅 ====================

    async def subscribe_events(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """订阅事件类型（订阅后匹配事件进入环形缓冲），返回全部活跃订阅"""
        types = body.get("types")
        if not isinstance(types, list) or not types:
            raise BridgeError(
                400,
                "缺少必填参数 'types'（事件类型数组，如 [\"message\", \"notice\"]，"
                "\"*\" 表示全部类型）",
            )
        return self.module.subscribe_events(types)

    async def unsubscribe(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """取消事件订阅，返回剩余全部活跃订阅"""
        sid = self._require_str(body, "subscription_id", "订阅时返回的 subscription_id")
        if not self.module.unsubscribe(sid):
            active = ", ".join(s["id"] for s in self.module.active_subscriptions()) or "（无）"
            raise BridgeError(404, f"订阅 '{sid}' 不存在。当前活跃订阅: {active}")
        return {
            "subscription_id": sid,
            "unsubscribed": True,
            "active_subscriptions": self.module.active_subscriptions(),
        }

    async def poll_events(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """
        轮询拉取缓冲事件（since_ts 增量游标）

        响应附 ``dropped``（缓冲累计淘汰数）；当本次无新事件但淘汰数
        较上次轮询增加时附 ``warning``（提示调大缓冲或缩短轮询间隔）。
        可选 ``subscription_id`` 按该订阅的类型集过滤返回事件。
        """
        since_ts = self._num_param(body, "since_ts", "游标时间戳", float, default=0.0)
        since_seq = self._num_param(body, "since_seq", "游标序号", int, default=0)
        raw_limit = body.get("limit")
        limit = (
            self._clamp(self._num_param(body, "limit", "最多返回条数", int), 1, _POLL_HARD_LIMIT)
            if raw_limit is not None
            else None
        )
        subscription_id = body.get("subscription_id")
        if subscription_id is not None and not str(subscription_id).strip():
            subscription_id = None
        result = self.module.poll_events(
            since_ts=since_ts,
            since_seq=since_seq,
            limit=limit,
            subscription_id=str(subscription_id) if subscription_id else None,
        )
        # 淘汰可见性（增量判断）：只有"无新事件但仍在淘汰"才告警，避免每次轮询重复刷
        dropped = int(result.get("dropped") or 0)
        last_dropped = self._last_poll_dropped
        self._last_poll_dropped = dropped
        if not result.get("events") and dropped > last_dropped:
            result["warning"] = self.module._msg(
                "ApiMCP.buffer_overflow_warning",
                default=(
                    "事件缓冲溢出已淘汰最旧记录，考虑调大 event_buffer_size 或缩短轮询间隔"
                ),
            )
        return result

    # ==================== 观测 ====================

    async def get_topology(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """获取运行时拓扑（脱敏：去除绝对路径 / 密钥类字段）"""
        topology = self.sdk.get_topology()
        return {"topology": sanitize_for_ai(topology)}

    async def sdk_health(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """SDK 健康/运行摘要"""
        module = self.module
        conf = module._conf()
        modules_summary = (self.sdk.module.get_status_summary() or {}).get("modules", {})
        loaded_modules = sum(1 for v in modules_summary.values() if v.get("status") == "loaded")
        adapters_summary = (self.sdk.adapter.get_status_summary() or {}).get("adapters", {})
        uptime = round(time.time() - module.loaded_at, 1) if module.loaded_at else None
        return {
            "sdk_version": str(getattr(self.sdk, "version", "") or ""),
            "module_version": str(module.module_version),
            "auth_configured": True,
            "uptime_seconds": uptime,
            "modules": {
                "registered": len(modules_summary),
                "loaded": loaded_modules,
            },
            "adapters": {
                platform: entry.get("status")
                for platform, entry in adapters_summary.items()
            },
            "event_buffer": module.buffer_stats(),
            "allow_module_reload": bool(conf.allow_module_reload),
            "allow_adapter_reload": bool(conf.allow_adapter_reload),
        }

    # 端点 → handler 方法名映射（字符串键 + getattr 延迟解析，见 handle_request；
    # on_load 经 missing_handlers() 启动自检，缺失则桥 503 拒绝服务）
    HANDLERS = {
        "modules/list": "list_modules",
        "modules/info": "get_module_info",
        "modules/call": "call_module",
        "modules/reload": "reload_module",
        "adapters/list": "list_adapters",
        "adapters/status": "get_adapter_status",
        "adapters/reload": "reload_adapter",
        "messages/send": "send_message",
        "messages/recent": "recent_messages",
        "events/subscribe": "subscribe_events",
        "events/unsubscribe": "unsubscribe",
        "events/poll": "poll_events",
        "topology": "get_topology",
        "health": "sdk_health",
        "docs/search": "docs_search",
        "docs/read": "docs_read",
        "docs/refresh": "docs_refresh",
    }

    # ==================== 文档检索（ApiMCP 自有文档引擎） ====================

    def _docs(self):
        engine = getattr(self.module, "docs", None)
        if engine is None:
            from .docs import DocsEngine

            engine = DocsEngine(self.module)
            self.module.docs = engine
        return engine

    async def docs_search(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """检索 ErisPulse 框架文档（标题+正文双路词面打分，返回片段与来源文件）"""
        query = self._require_str(body, "query", "检索关键词")
        try:
            top_k = max(1, min(int(body.get("top_k") or 5), 20))
        except (TypeError, ValueError):
            top_k = 5
        engine = self._docs()
# 懒同步：缓存为空时先对齐一次（本地源命中零网络；网络失败沿用旧缓存）
        if engine.active_root() is None:
            await engine.ensure_synced()
        results = engine.search(query, top_k=top_k)
        if not results:
            return {"results": [], "note": "no match; try different keywords or call docs/refresh",
                    "status": engine.status()}
        return {"results": results, "count": len(results), "status": engine.status()}

    async def docs_read(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """读取框架文档单文件（相对文档根目录路径，路径穿越已防护）"""
        file = self._require_str(body, "file", "文档相对路径，如 sdk/module.md")
        try:
            limit = max(500, min(int(body.get("limit") or 8000), 40000))
        except (TypeError, ValueError):
            limit = 8000
        engine = self._docs()
        content = await asyncio.to_thread(engine.read, file, limit)
        return {"file": file, "content": content, "status": engine.status()}

    async def docs_refresh(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """强制同步框架文档缓存到已安装框架版本（失败保留旧版）"""
        engine = self._docs()
        info = await engine.ensure_synced(force=True)
        return {"sync": info, "status": engine.status()}


def _is_awaitable(value: Any) -> bool:
    """判断是否为可等待对象（import 局部化避免顶层 inspect 开销）"""
    import inspect

    return inspect.isawaitable(value)
