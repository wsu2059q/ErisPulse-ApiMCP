"""
erispulse-apimcp —— stdio MCP server

以官方 mcp SDK 起 stdio MCP server，把 MCP tools 转发到机器人进程内的
REST 桥（``/apimcp/*``，``X-ApiMCP-Token`` 鉴权）。机器人地址 / token 来源：

1. 命令行参数 ``--url`` / ``--token``（优先）
2. 环境变量 ``ERISPULSE_APIMCP_URL`` / ``ERISPULSE_APIMCP_TOKEN`` / ``ERISPULSE_APIMCP_TIMEOUT``
3. 默认 ``http://127.0.0.1:8000``（ErisPulse 框架 HTTP server 默认端口）

{!--< tips >!--}
1. 兼容 mcp 1.x（FastMCP）与 2.x（改名 MCPServer）两个大版本
2. 桥客户端走 ``http.client`` 持久连接（keep-alive 复用，连接失败重建；
   仅幂等 GET 类请求在连接失败时重试 1 次，POST 不重试）
3. 机器人不在线 / token 错误 / 桥未配置时，tools 返回可读错误文本（不抛栈）
4. Claude Desktop 等 AI 客户端经 ``claude_desktop_config.json`` 拉起本进程
{!--< /tips >!--}
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

# mcp 1.x 导出 FastMCP；mcp 2.x 改名 MCPServer —— 双版本兼容导入
try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore[no-redef]

__all__ = ["BotOfflineError", "BridgeClient", "build_mcp_server", "main"]

# ---------------------------------------------------------------------------
# 行为参数（集中声明，注明使用位置与修改影响）
# ---------------------------------------------------------------------------

# 环境变量名（与 README / Claude Desktop 配置示例一致）
ENV_URL = "ERISPULSE_APIMCP_URL"
ENV_TOKEN = "ERISPULSE_APIMCP_TOKEN"
ENV_TIMEOUT = "ERISPULSE_APIMCP_TIMEOUT"

# 默认机器人地址（ErisPulse HTTP server 默认监听 0.0.0.0:8000）
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
# HTTP 转发默认超时（秒）：机器人侧 handler 卡死时快速失败回可读文案
DEFAULT_TIMEOUT = 10.0

# REST 桥路径前缀（与模块内 _ROUTE_NAMESPACE 对应；框架 RouterManager 把
# 含斜杠 path 作为多级字面路径挂载，故 client 侧逐字拼接即可）
_BRIDGE_PREFIX = "/apimcp/"
# token 请求头（与 ErisPulse_ApiMCP.bridge.TOKEN_HEADER 一致；复制常量避免
# server 进程 import ErisPulse 全链路——stdio server 可独立于 SDK 安装使用）
_TOKEN_HEADER = "X-ApiMCP-Token"


def _env_timeout() -> float:
    """读取超时环境变量（非法值回退默认，不阻断启动）"""
    raw = os.environ.get(ENV_TIMEOUT, "")
    try:
        return float(raw) if raw else DEFAULT_TIMEOUT
    except ValueError:
        return DEFAULT_TIMEOUT


def _fmt_timeout(timeout: float) -> str:
    """超时秒数格式化（整数无小数点，用于错误文案）"""
    return f"{timeout:.0f}" if float(timeout).is_integer() else str(timeout)


def _package_version() -> str:
    """读取发行包版本（importlib.metadata 失败时回退源码常量）"""
    try:
        from importlib.metadata import version

        return version("ErisPulse-ApiMCP")
    except Exception:
        return "0.1.0"


class _ConnectionFailure(Exception):
    """连接层失败（可经重建连接重试的类别，见 BridgeClient._request_once）"""


class BotOfflineError(RuntimeError):
    """机器人 HTTP 不可达（进程未启动 / 地址错误 / 模块未加载）"""


def _validate_base_url(base_url: str) -> None:
    """
    校验机器人地址格式：scheme 必须为 http/https 且主机名非空

    无 scheme 的地址（如 ``localhost:8000``）会被 ``urlsplit`` 误解析成
    ``scheme=localhost``，随后静默连到 127.0.0.1:80——必须显式拒绝。

    :param base_url: 机器人 HTTP 地址
    :raises ValueError: scheme 非法或主机名缺失
    """
    parts = urlsplit(base_url or "")
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(
            f"机器人地址必须以 http:// 或 https:// 开头，收到: '{base_url}'"
            "（无 scheme 的地址如 localhost:8000 会被误解析，请写全 http://localhost:8000）"
        )
    if not parts.hostname:
        raise ValueError(f"机器人地址缺少主机名，收到: '{base_url}'")


class BridgeClient:
    """
    机器人 REST 桥 HTTP 客户端（http.client 持久连接，stdio server 内使用）

    - keep-alive 连接复用（多次 tool 调用不重建 TCP 连接）
    - **线程安全**：mcp stdio server（1.x/2.x）并发分发请求、同步 tool 在
      工作线程执行——共享持久连接经 ``threading.Lock`` 全程串行化，杜绝
      并发读写同一 ``http.client`` 连接导致的响应串号 / 连接错乱
    - 连接层失败时丢弃旧连接；**仅幂等 GET 类**请求重建后重试 1 次，
      POST（可能已触发发送等副作用）不重试
    - 统一把网络层异常转成面向 AI 的可读错误（超时 → 繁忙提示）
    """

    def __init__(self, base_url: str, token: str, timeout: float = DEFAULT_TIMEOUT):
        """
        :param base_url: 机器人 HTTP 地址（如 ``http://127.0.0.1:8000``，
            必须带 http:// 或 https:// scheme）
        :param token: ApiMCP 访问 token
        :param timeout: 单次请求超时（秒）
        :raises ValueError: base_url 无 scheme / scheme 非法 / 主机名缺失
        """
        _validate_base_url(base_url)
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.token = token or ""
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        parts = urlsplit(self.base_url)
        self._scheme = parts.scheme or "http"
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or (443 if self._scheme == "https" else 80)
        self._conn: Optional[http.client.HTTPConnection] = None
        # mcp stdio server 并发分发：串行化共享连接的"选连接→发请求→收响应"
        self._lock = threading.Lock()

    # ---------------- 连接层 ----------------

    def _new_connection(self) -> http.client.HTTPConnection:
        """按 scheme 建新连接（测试经 monkeypatch 本方法注入假连接）"""
        if self._scheme == "https":
            return http.client.HTTPSConnection(self._host, self._port, timeout=self.timeout)
        return http.client.HTTPConnection(self._host, self._port, timeout=self.timeout)

    def _close_connection(self) -> None:
        """丢弃当前连接（静默，连接可能已坏）"""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _request_once(
        self,
        method: str,
        path: str,
        data: Optional[bytes],
        headers: Dict[str, str],
    ) -> Any:
        """
        单次请求（不含重试逻辑）

        :raises _ConnectionFailure: 连接层失败（调用方决定是否重建重试）
        :raises BotOfflineError: 机器人不可达（连接失败且不重试 / 重试耗尽）
        :raises RuntimeError: 超时（繁忙）/ HTTP 错误 / 响应异常
        """
        if self._conn is None:
            self._conn = self._new_connection()
        try:
            self._conn.request(method, path, body=data, headers=headers)
            resp = self._conn.getresponse()
            raw = resp.read()
            status = resp.status
        except TimeoutError:
            # 超时：机器人在线但处理慢/卡死（连接可能仍半开，丢弃重建）
            self._close_connection()
            raise RuntimeError(
                f"机器人繁忙（超时 {_fmt_timeout(self.timeout)}s），请稍后重试"
            ) from None
        except (http.client.HTTPException, OSError) as e:
            # 连接层失败（拒绝/重置/断开）：丢弃旧连接，交由调用方重试策略
            self._close_connection()
            raise _ConnectionFailure(str(e)) from None

        return self._parse_response(status, raw)

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[bytes],
        headers: Dict[str, str],
        *,
        idempotent: bool,
    ) -> Any:
        """
        请求入口：加锁串行化 + 持久连接 + 失败重建；幂等请求重试 1 次

        全程持锁（含响应解析前的收包）：mcp stdio server 在多个工作线程
        并发调用同步 tool，共享连接的"选连接→发送→收响应"必须原子，
        否则响应串号（A 线程收到 B 线程的响应体）。锁粒度覆盖整个
        请求-响应周期，单连接吞吐即模块设计吞吐（stdio 场景足够）。

        :param method: HTTP 方法（GET / POST）
        :param path: 请求路径（含 _BRIDGE_PREFIX）
        :param data: 请求体字节
        :param headers: 请求头
        :param idempotent: 是否幂等（GET 类）：连接失败时重建连接重试 1 次；
            POST 可能已产生副作用，不做重试
        :return: 桥响应中的 ``data`` 字段
        """
        with self._lock:
            attempts = 2 if idempotent else 1
            last_failure: Optional[_ConnectionFailure] = None
            for _ in range(attempts):
                try:
                    return self._request_once(method, path, data, headers)
                except _ConnectionFailure as e:
                    last_failure = e
                    # _request_once 已丢弃坏连接；下一轮循环经 _new_connection 重建
            raise BotOfflineError(
                f"无法连接 ErisPulse 机器人（{self.base_url}）：{last_failure}。"
                f"请确认：1) 机器人进程已启动；2) ApiMCP 模块已加载；"
                f"3) 地址正确（--url 或环境变量 {ENV_URL}）"
            ) from None

    def _parse_response(self, status: int, raw: bytes) -> Any:
        """解析桥响应 JSON 并映射 HTTP 错误（401 / 其他非 2xx）"""
        if status == 401:
            raise RuntimeError(
                "鉴权失败（HTTP 401）：token 无效或已变更。"
                "请检查 ERISPULSE_APIMCP_TOKEN / --token 与机器人配置是否一致"
                f"{self._detail_from_raw(raw)}"
            ) from None
        if not 200 <= status < 300:
            raise RuntimeError(f"机器人返回 HTTP {status}: {self._detail_from_raw(raw) or '无详情'}") from None

        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"机器人响应不是有效 JSON: {e}") from None
        if not isinstance(body, dict):
            raise RuntimeError(f"机器人响应格式异常（非 JSON 对象）: {str(body)[:200]}")
        if not body.get("ok"):
            raise RuntimeError(str(body.get("error") or f"未知错误: {body}"))
        return body.get("data")

    @staticmethod
    def _detail_from_raw(raw: bytes) -> str:
        """从错误响应体提取桥层 error 字段（尽力而为）"""
        try:
            body = json.loads(raw.decode("utf-8"))
            if isinstance(body, dict) and body.get("error"):
                return f"：{body['error']}"
        except Exception:
            pass
        return ""

    # ---------------- 公开 API ----------------

    def post(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        """
        调用 REST 桥端点（POST，不重试——可能已触发发送等副作用）

        :param action: 端点名（如 ``"modules/list"``）
        :param payload: JSON 请求体
        :return: 桥响应中的 ``data`` 字段
        :raises BotOfflineError: 机器人不可达
        :raises RuntimeError: 业务失败 / HTTP 错误 / 响应异常，附可读说明
        """
        data = json.dumps(payload or {}, ensure_ascii=False, default=str).encode("utf-8")
        return self._request(
            "POST",
            _BRIDGE_PREFIX + action,
            data,
            {"Content-Type": "application/json", _TOKEN_HEADER: self.token, "Content-Length": str(len(data))},
            idempotent=False,
        )

    def get(self, action: str) -> Any:
        """
        幂等 GET 请求（如 health 探活；连接层失败重建后重试 1 次）

        :param action: 端点名（如 ``"health"``）
        :return: 桥响应中的 ``data`` 字段
        :raises BotOfflineError: 机器人不可达
        :raises RuntimeError: 业务失败 / HTTP 错误 / 响应异常，附可读说明
        """
        return self._request(
            "GET",
            _BRIDGE_PREFIX + action,
            None,
            {_TOKEN_HEADER: self.token, "Accept": "application/json"},
            idempotent=True,
        )


def _tool_call(client: BridgeClient, action: str, payload: Optional[Dict[str, Any]] = None) -> str:
    """
    tool 统一出口：成功返回 JSON 文本，失败返回可读错误文本

    :param client: 桥客户端
    :param action: 端点名
    :param payload: 请求体
    :return: 面向 AI 的文本结果
    """
    try:
        data = client.post(action, payload)
        return json.dumps(data, ensure_ascii=False, default=str)
    except (BotOfflineError, RuntimeError) as e:
        return f"错误: {e}"


def build_mcp_server(client: BridgeClient) -> FastMCP:
    """
    构建 stdio MCP server（注册全部 ApiMCP tools）

    :param client: 桥客户端
    :return: FastMCP（mcp 1.x）/ MCPServer（mcp 2.x）实例
    """

    server: FastMCP = FastMCP(
        "erispulse-apimcp",
        instructions=(
            "本服务把 ErisPulse 机器人运行时开放为 MCP tools：收发消息、调用模块 API、"
            "管理模块/适配器、订阅并轮询事件流、查看运行拓扑与健康状态。"
            "事件观测请先 subscribe_events 再用 poll_events 增量轮询。"
        ),
        version=_package_version(),
    )

    # ---------------- 模块管理 ----------------

    @server.tool(name="list_modules")
    def list_modules() -> str:
        """列出机器人已注册的全部模块及状态（status: loaded/registered/disabled，enabled，failed，stale）。
        排查模块问题、了解机器人装了哪些模块时先调用本工具。"""
        return _tool_call(client, "modules/list")

    @server.tool(name="get_module_info")
    def get_module_info(name: str) -> str:
        """查询指定模块的注册信息与元信息（版本/描述/作者/服务契约/最近错误现场）。

        Args:
            name: 模块名（先用 list_modules 获取候选）
        """
        return _tool_call(client, "modules/info", {"name": name})

    @server.tool(name="call_module")
    def call_module(
        name: str,
        method: str,
        args: Optional[List[Any]] = None,
        kwargs: Optional[Dict[str, Any]] = None,
        rpc_timeout: Optional[float] = None,
    ) -> str:
        """调用机器人内某模块的公开服务方法（跨模块 RPC）。

        仅允许公开方法；机器人侧可配置 module_call_whitelist 白名单限制可调用范围，
        白名单外调用会被拒绝并返回原因。注意：kwargs 中的 timeout 无法透传（被
        SDK 占用），RPC 超时请用独立的 rpc_timeout 参数。

        Args:
            name: 目标模块名
            method: 目标方法名（可先用 get_module_info 查看模块声明的 services 契约）
            args: 位置参数列表（可省略）
            kwargs: 关键字参数对象（可省略；timeout 键会被剔除）
            rpc_timeout: RPC 超时秒数（可省略；仅对协程方法生效）
        """
        payload: Dict[str, Any] = {"name": name, "method": method}
        if args is not None:
            payload["args"] = args
        if kwargs is not None:
            payload["kwargs"] = kwargs
        if rpc_timeout is not None:
            payload["rpc_timeout"] = rpc_timeout
        return _tool_call(client, "modules/call", payload)

    @server.tool(name="reload_module")
    def reload_module(name: str) -> str:
        """热重载指定模块（改动模块代码后无需重启机器人）。

        受机器人配置 allow_module_reload 开关控制（默认允许）；重载失败时返回失败原因。

        Args:
            name: 模块名
        """
        return _tool_call(client, "modules/reload", {"name": name})

    # ---------------- 适配器管理 ----------------

    @server.tool(name="list_adapters")
    def list_adapters() -> str:
        """列出机器人已注册的平台适配器及状态（status: started/stopped/disabled，Bot 在线情况）。
        发消息前可用本工具确认目标平台名与在线状态。"""
        return _tool_call(client, "adapters/list")

    @server.tool(name="get_adapter_status")
    def get_adapter_status(platform: str) -> str:
        """查询指定平台适配器的运行状态与各 Bot 在线概况。

        Args:
            platform: 平台名（如 onebot11 / telegram）
        """
        return _tool_call(client, "adapters/status", {"platform": platform})

    @server.tool(name="reload_adapter")
    def reload_adapter(platform: str) -> str:
        """热重载指定平台适配器（会断开并重连平台连接，影响在线消息收发）。

        受机器人配置 allow_adapter_reload 开关控制，默认禁用；开启后调用才生效。

        Args:
            platform: 平台名
        """
        return _tool_call(client, "adapters/reload", {"platform": platform})

    # ---------------- 消息 ----------------

    @server.tool(name="send_message")
    def send_message(platform: str, target_type: str, target_id: str, method: str, content: str, account: Optional[str] = None) -> str:
        """向指定平台发送消息（经平台适配器 SendDSL）。

        Args:
            platform: 平台名（如 onebot11，先用 list_adapters 确认）
            target_type: 目标类型：user（私聊）/ group（群聊）/ channel / guild / thread（private 也可，自动转为 user）
            target_id: 目标 ID（用户 ID / 群号，字符串，≤128 字符）
            method: 发送方法名：Text（文本）/ Image / Voice / Video / File（后四者为 URL 或 base64）
            content: 消息内容（Text 为文本字符串；媒体类为 URL 或 base64 字符串）
            account: 可选，指定发送账户 ID（多 bot 部署时区分用哪个账户发；缺省用适配器默认账户）
        """
        return _tool_call(
            client,
            "messages/send",
            {
                "platform": platform,
                "target_type": target_type,
                "target_id": target_id,
                "method": method,
                "content": content,
                **({"account": account} if account else {}),
            },
        )

    @server.tool(name="search_erispulse_docs")
    def search_erispulse_docs(query: str, top_k: int = 5) -> str:
        """检索 ErisPulse 框架文档（写模块/查 API 用法时先查这里再动手；需机器人装有 QvQChat 模块）。

        Args:
            query: 检索关键词（如 "发送消息"、"register_http_route"、"适配器开发"）
            top_k: 最多返回条数（默认 5，最大 20）
        """
        return _tool_call(client, "docs/search", {"query": query, "top_k": top_k})

    @server.tool(name="read_erispulse_doc")
    def read_erispulse_doc(file: str, limit: int = 8000) -> str:
        """读取框架文档单文件全文（路径来自 search_erispulse_docs 结果的 file 字段）。

        Args:
            file: 文档相对路径（相对文档根目录）
            limit: 最多返回字符数（默认 8000）
        """
        return _tool_call(client, "docs/read", {"file": file, "limit": limit})

    @server.tool(name="refresh_erispulse_docs")
    def refresh_erispulse_docs() -> str:
        """强制同步框架文档缓存到当前已安装框架版本（框架升级后文档对不上时用）。"""
        return _tool_call(client, "docs/refresh", {})

    @server.tool(name="recent_messages")
    def recent_messages(platform: Optional[str] = None, session: Optional[str] = None, limit: int = 50) -> str:
        """查询机器人最近收发的消息（会话记录，非实时订阅）。

        实时观测新消息请用 subscribe_events + poll_events；本工具适合回看上下文。

        Args:
            platform: 可选，按平台过滤（如 onebot11）
            session: 可选，会话键 "平台:类型:ID"（如 "onebot11:group:123456"，类型 private/group/user/channel/guild/thread）精确查询该会话；与 platform 同给时须前缀匹配
            limit: 最多返回条数（默认 50，上限 500）
        """
        payload: Dict[str, Any] = {"limit": limit}
        if platform:
            payload["platform"] = platform
        if session:
            payload["session"] = session
        return _tool_call(client, "messages/recent", payload)

    # ---------------- 事件订阅 ----------------

    @server.tool(name="subscribe_events")
    def subscribe_events(types: List[str]) -> str:
        """订阅机器人事件流（订阅后匹配事件进入机器人侧环形缓冲，供 poll_events 拉取）。
        响应含本次 subscription_id 与当前全部活跃订阅（active_subscriptions）。

        Args:
            types: 事件类型列表（OneBot12 标准类型："message" / "notice" / "request" / "meta"，或 "*" 全部）
        """
        return _tool_call(client, "events/subscribe", {"types": types})

    @server.tool(name="unsubscribe")
    def unsubscribe(subscription_id: str) -> str:
        """取消事件订阅（停止向环形缓冲写入对应类型事件）。响应含剩余全部活跃订阅。

        Args:
            subscription_id: subscribe_events 返回的 subscription_id
        """
        return _tool_call(client, "events/unsubscribe", {"subscription_id": subscription_id})

    @server.tool(name="poll_events")
    def poll_events(
        since_ts: float = 0.0,
        since_seq: int = 0,
        limit: Optional[int] = None,
        subscription_id: Optional[str] = None,
    ) -> str:
        """轮询拉取已订阅的新事件（增量游标）。首次传 since_ts=0；之后传上次返回的 next_since_ts / next_since_seq。
        响应含 dropped（缓冲累计淘汰数）；无新事件但淘汰增加时附 warning 提示。

        Args:
            since_ts: 游标时间戳（首次 0；之后用上次返回的 next_since_ts）
            since_seq: 游标序号（同秒事件区分；用上次返回的 next_since_seq）
            limit: 本次最多返回条数（默认机器人侧配置）
            subscription_id: 可选，只返回该订阅声明类型的事件（subscribe_events 返回的 ID）
        """
        payload: Dict[str, Any] = {"since_ts": since_ts, "since_seq": since_seq}
        if limit is not None:
            payload["limit"] = limit
        if subscription_id:
            payload["subscription_id"] = subscription_id
        return _tool_call(client, "events/poll", payload)

    # ---------------- 观测 ----------------

    @server.tool(name="get_topology")
    def get_topology() -> str:
        """获取机器人运行时拓扑（模块的命令/事件处理器/路由归属、适配器与 Bot 状态、作用域绑定）。
        已脱敏：不包含绝对路径与密钥类字段。"""
        return _tool_call(client, "topology")

    @server.tool(name="sdk_health")
    def sdk_health() -> str:
        """机器人健康摘要：SDK 版本、运行时长、已加载模块数、各适配器状态、事件缓冲与订阅情况。"""
        return _tool_call(client, "health")

    return server


def main(argv: Optional[List[str]] = None) -> int:
    """
    console script 入口：解析参数 → 构建 server → stdio 运行

    :param argv: 命令行参数（默认 sys.argv[1:]）
    :return: 进程退出码（正常情况下 ``run("stdio")`` 阻塞至进程退出，
        不会执行到返回；此处保留签名供测试与直接调用）
    """
    parser = argparse.ArgumentParser(
        prog="erispulse-apimcp",
        description=(
            "ErisPulse MCP 网关（stdio server）：把 ErisPulse 机器人能力"
            "（收发消息/模块调用/模块与适配器管理/事件流）开放为 MCP tools"
        ),
    )
    parser.add_argument(
        "--url",
        default=os.environ.get(ENV_URL, DEFAULT_BASE_URL),
        help=(
            f"机器人 HTTP 地址，必须带 http:// 或 https://（默认环境变量 {ENV_URL} 或 {DEFAULT_BASE_URL}；"
            "无 scheme 的地址如 localhost:8000 会被拒绝）"
        ),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get(ENV_TOKEN, ""),
        help=(
            f"ApiMCP 访问 token（默认环境变量 {ENV_TOKEN}，优先使用环境变量——"
            "命令行参数会暴露在进程列表中；未配置时机器人侧桥不启用，调用将得到 503）"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=_env_timeout(),
        help=f"HTTP 转发超时秒数（默认 {DEFAULT_TIMEOUT}）",
    )
    parser.add_argument("--version", action="version", version=f"erispulse-apimcp {_package_version()}")
    args = parser.parse_args(argv)

    if not args.token:
        # stdio 协议占用 stdout，提示只能走 stderr
        print(
            f"警告：未提供 token（--token 或环境变量 {ENV_TOKEN}）。"
            "机器人侧 ApiMCP 必须显式配置 token（[ApiMCP] token 或环境变量 "
            "ERISPULSE_APIMCP_TOKEN），未配置时桥返回 503、所有调用将被拒绝",
            file=sys.stderr,
        )

    try:
        client = BridgeClient(args.url, args.token, args.timeout)
    except ValueError as e:
        # 地址格式错误（无 scheme 等）：明确报错退出，而非静默错连
        print(f"错误：{e}", file=sys.stderr)
        return 2

    server = build_mcp_server(client)
    server.run("stdio")
    # run("stdio") 正常情况下阻塞至进程退出（此行不可达）；返回 0 仅在
    # mcp 实现变化、run 提前返回时作为语义正确的兜底退出码
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
