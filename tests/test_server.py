"""server.py 测试：路由注册/卸载生命周期、stdio server 构建、CLI 与客户端行为"""

import json
import threading
import time

import pytest

from conftest import TOKEN

from ErisPulse_ApiMCP.Core import Main
from ErisPulse_ApiMCP.bridge import ApiBridge, sanitize_for_ai
from ErisPulse_ApiMCP.server import BridgeClient, BotOfflineError, build_mcp_server, main


# ---------------- 路由生命周期（经 FakeRouter）----------------


async def test_routes_registered_and_cleaned(apimcp, fake_sdk):
    await apimcp.on_load({"module_name": "ApiMCP"})
    try:
        routes = fake_sdk.router.routes
        assert ("apimcp", "/modules/list") in routes
        assert ("apimcp", "/events/poll") in routes
        assert ("apimcp", "/topology") in routes
        assert ("apimcp", "/health") in routes
        assert routes[("apimcp", "/health")]["methods"] == ["GET"], "health 端点应支持 GET 探活"
        assert routes[("apimcp", "/modules/list")]["methods"] == ["POST"]
        # 全部路由挂默认限流（P1-4）
        for key, entry in routes.items():
            assert entry["rate_limit"] == "30/minute", f"{key} 应带限流"

        # 事件通配处理器已注册
        assert "*" in fake_sdk.adapter.handlers
    finally:
        await apimcp.on_unload({"module_name": "ApiMCP"})
    assert fake_sdk.router.routes == {}, "on_unload 应注销全部 REST 路由"


def test_fake_router_rejects_duplicate_registration():
    """FakeRouter 对齐真 RouterManager：同 path 同方法重复注册抛 ValueError"""
    from conftest import FakeRouter

    router = FakeRouter()

    async def handler(request):  # noqa: ANN001, ARG001
        return {}, 200

    router.register_http_route("apimcp", "/x", handler, methods=["POST"])
    with pytest.raises(ValueError, match="重复注册"):
        router.register_http_route("apimcp", "/x", handler, methods=["POST"])
    # 不同方法不冲突，合并
    router.register_http_route("apimcp", "/x", handler, methods=["GET"])
    assert router.routes[("apimcp", "/x")]["methods"] == ["POST", "GET"]


async def test_token_switch_on_config_update(loaded, fake_sdk):
    old_token = loaded.token
    loaded._conf = lambda: type(loaded).ConfigClass(token="new-fixed-token")
    loaded.on_config_update(None, None)
    assert loaded.token == "new-fixed-token"
    assert loaded.token != old_token


async def test_buffer_resized_on_config_update(loaded):
    loaded._conf = lambda: type(loaded).ConfigClass(token=TOKEN, event_buffer_size=77)
    loaded.on_config_update(None, None)
    assert loaded._buffer.maxlen == 77


# ---------------- handler 启动自检（P2-5）----------------


async def test_on_load_self_check_all_handlers_present(loaded):
    assert loaded._bridge.missing_handlers() == []
    assert loaded._bridge_ready is True


async def test_missing_handler_makes_bridge_503(loaded, fake_sdk, monkeypatch):
    """on_load 自检发现 handler 缺失：log.error + 桥 503 拒绝服务"""
    broken = dict(ApiBridge.HANDLERS)
    broken["modules/list"] = "no_such_handler_method"
    monkeypatch.setattr(ApiBridge, "HANDLERS", broken)
    await loaded.on_unload({"module_name": "ApiMCP"})  # 先卸载，避免路由重复注册
    await loaded.on_load({"module_name": "ApiMCP"})  # 重跑触发自检
    try:
        assert loaded._bridge_ready is False
        errors = [msg for level, msg in fake_sdk.logger.records if level == "error"]
        assert any("modules/list" in msg for msg in errors), "缺失明细应进 error 日志"

        body, status = await loaded._bridge.handle_request("modules/list", _req())
        assert status == 503
        assert body["ok"] is False
        # 其余端点同样拒绝
        body, status = await loaded._bridge.handle_request("health", _req())
        assert status == 503
    finally:
        monkeypatch.undo()
        await loaded.on_unload({"module_name": "ApiMCP"})
        await loaded.on_load({"module_name": "ApiMCP"})  # 恢复后重新就绪
        assert loaded._bridge_ready is True


# ---------------- 500 错误脱敏（P2-6）----------------


async def test_internal_error_returns_type_only(loaded, fake_sdk, monkeypatch):
    """未捕获异常：客户端只见 internal: <类型>，完整 repr 只进日志"""
    def boom():
        raise ValueError("secret detail /D:/path leak")

    monkeypatch.setattr(loaded.sdk, "get_topology", boom)
    body, status = await loaded._bridge.handle_request("topology", _req())
    assert status == 500
    assert body == {"ok": False, "error": "internal: ValueError"}
    errors = [msg for level, msg in fake_sdk.logger.records if level == "error"]
    assert any("secret detail" in msg for msg in errors), "完整 repr 应进 logger.error"


def _req():
    from conftest import FakeRequest

    return FakeRequest(token=TOKEN)


# ---------------- sanitize_for_ai 单元 ----------------


def test_sanitize_drops_secrets_paths_and_raw():
    data = {
        "api_token": "s3cret",
        "password": "p",
        "module_path": "/abs/path/mod.py",
        "relative_path": "ok/relative.py",
        "name": "Fine",
        "onebot11_raw": {"secret": 1},
        "payload": b"\x00\x01",
        "nested": {"access_key": "k", "keep": 1},
    }
    out = sanitize_for_ai(data)
    text = str(out)
    assert "s3cret" not in text and "api_token" not in out
    assert "password" not in out
    assert "module_path" not in out
    assert out["relative_path"] == "ok/relative.py"
    assert "onebot11_raw" not in out
    assert out["payload"] == "<bytes 2B>"
    assert "access_key" not in out["nested"]
    assert out["nested"]["keep"] == 1


def test_sanitize_drops_pathish_keys_regardless_of_value():
    """路径类字段名一律丢弃（不再看值形态，相对路径也丢弃）"""
    out = sanitize_for_ai({
        "path": "relative/ok.py",
        "file": "small.png",
        "root": "./here",
        "source": "somewhere",
        "kept": "value",
    })
    assert set(out) == {"kept"}


def test_sanitize_drops_fields_with_dotdot_values():
    """字段值含 ".." 的字符串整键丢弃（路径穿越样貌，保守策略）"""
    out = sanitize_for_ai({
        "note": "see ../../etc/passwd",
        "fine": "a..b",  # 含 ".." 一律丢弃
        "ok": "normal.value",
        "nested": {"cmd": "cat ../secret", "keep": 1},
    })
    assert set(out) == {"ok", "nested"}
    assert out["ok"] == "normal.value"
    assert out["nested"] == {"keep": 1}


def test_sanitize_drops_string_leaves_with_path_shape():
    """字符串叶子为绝对路径形态（盘符/前导斜杠/UNC/file://）：整键丢弃"""
    out = sanitize_for_ai({
        "drive": "C:\\Users\\bot\\secret",
        "posix": "/etc/passwd",
        "unc": "\\\\fileserver\\share",
        "fileuri": "file:///var/log/bot.log",
        "url_kept": "https://example.com/ok",  # http(s) URL 不是路径形态，保留
        "relative_kept": "docs/readme.md",
    })
    assert "drive" not in out
    assert "posix" not in out
    assert "unc" not in out
    assert "fileuri" not in out
    assert out["url_kept"] == "https://example.com/ok"
    assert out["relative_kept"] == "docs/readme.md"


async def test_default_posture_warnings_on_load(loaded, fake_sdk):
    """P2-7：宽默认姿态启动时 log.warning 提示（不改行为）"""
    warnings = [msg for level, msg in fake_sdk.logger.records if level == "warning"]
    assert any("module_call_whitelist" in msg and "为空" in msg for msg in warnings), \
        "白名单为空应有风险告警"
    assert any("allow_module_reload" in msg for msg in warnings), \
        "允许模块重载应有风险告警"


async def test_tightened_posture_no_warnings(loaded, fake_sdk):
    """收紧姿态（白名单非空 + 禁重载）不触发姿态告警"""
    fake_sdk.logger.records.clear()
    loaded._conf = lambda: Main.ConfigClass(token=TOKEN, module_call_whitelist=["Safe"], allow_module_reload=False)
    await loaded.on_unload({"module_name": "ApiMCP"})
    await loaded.on_load({"module_name": "ApiMCP"})
    posture_warnings = [
        msg for level, msg in fake_sdk.logger.records
        if level == "warning" and ("module_call_whitelist" in msg or "allow_module_reload" in msg)
    ]
    assert posture_warnings == []


# ---------------- BridgeClient（http.client 持久连接）----------------


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw


class FakeConnection:
    """可编程假连接：按脚本依次执行行为（成功响应 / 抛异常）"""

    created = 0

    def __init__(self, script):
        self.script = list(script)
        self.requests = 0
        self.closed = False
        type(self).created += 1

    def request(self, method, path, body=None, headers=None):
        self.requests += 1
        self.last = (method, path, headers)

    def getresponse(self):
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action

    def close(self):
        self.closed = True


class EchoConnection(FakeConnection):
    """回声连接：响应体回显最近一次请求的 path（用于并发串号检测）"""

    def __init__(self):
        super().__init__([])
        self._echo_lock = threading.Lock()
        self._pending_path = None

    def request(self, method, path, body=None, headers=None):
        with self._echo_lock:
            self._pending_path = path

    def getresponse(self):
        # 拉宽竞态窗口：无锁实现的"发送→收响应"间隙在此期间可被其它线程插入
        time.sleep(0.002)
        with self._echo_lock:
            path = self._pending_path
            self._pending_path = None
        return FakeResponse(200, {"ok": True, "data": {"echo": path}})


def _client_with(script, timeout=10.0):
    client = BridgeClient("http://127.0.0.1:8000", "t", timeout=timeout)
    FakeConnection.created = 0
    client._new_connection = lambda: FakeConnection(script)
    return client


def test_bridge_client_reuses_persistent_connection():
    """两次调用复用同一连接（keep-alive，不重建 TCP）"""
    ok = FakeResponse(200, {"ok": True, "data": {"n": 1}})
    client = _client_with([ok, FakeResponse(200, {"ok": True, "data": {"n": 2}})])
    assert client.post("modules/list") == {"n": 1}
    assert client.post("modules/list") == {"n": 2}
    assert FakeConnection.created == 1, "keep-alive 下不应重建连接"


def test_bridge_client_thread_safe_no_cross_talk():
    """P0-1 回归：多线程并发 post 共享持久连接——无异常、无响应串号

    mcp stdio server 在工作线程并发调用同步 tool；EchoConnection 回显
    请求 path，若"发送→收响应"未原子化，线程会读到别的线程的响应体
    （echo 与自身 action 不符）。
    """
    client = BridgeClient("http://127.0.0.1:8000", "t")
    conn = EchoConnection()
    client._new_connection = lambda: conn

    threads_count, per_thread = 8, 12
    mismatches: list = []
    errors: list = []

    def worker(idx: int):
        try:
            for n in range(per_thread):
                action = f"stress/{idx}/{n}"
                data = client.post(action)
                echo = (data or {}).get("echo")
                if echo != f"/apimcp/{action}":
                    mismatches.append(f"t{idx}#{n} expect /apimcp/{action} got {echo}")
        except BaseException as e:  # noqa: BLE001 —— 线程内异常必须显式收集
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], f"并发请求不得抛异常: {errors[:3]}"
    assert mismatches == [], f"并发请求不得响应串号: {mismatches[:3]}"


def test_bridge_client_get_retries_once_on_connection_failure():
    """幂等 GET：连接层失败重建连接重试 1 次"""
    broken = FakeConnection([ConnectionResetError("stale")])
    ok = FakeConnection([FakeResponse(200, {"ok": True, "data": {"status": "up"}})])
    client = BridgeClient("http://127.0.0.1:8000", "t")
    connections = [broken, ok]

    def factory():
        return connections.pop(0)

    client._new_connection = factory
    assert client.get("health") == {"status": "up"}
    assert broken.requests == 1 and broken.closed


def test_bridge_client_post_does_not_retry():
    """POST 不重试（可能已触发发送副作用），连接失败直接可读报错"""
    client = _client_with([ConnectionResetError("boom")])
    with pytest.raises(BotOfflineError) as exc_info:
        client.post("messages/send", {"content": "x"})
    message = str(exc_info.value)
    assert "无法连接" in message
    assert "127.0.0.1:8000" in message
    assert FakeConnection.created == 1, "POST 失败后不得重试"


def test_bridge_client_timeout_message_is_readable():
    """超时：可读的繁忙文案（含超时秒数），默认 10s"""
    client = _client_with([TimeoutError()], timeout=10.0)
    with pytest.raises(RuntimeError) as exc_info:
        client.post("modules/list")
    message = str(exc_info.value)
    assert "机器人繁忙（超时 10s）" in message
    assert "请稍后重试" in message


def test_bridge_client_error_detail_from_bridge_body():
    """桥层 4xx/5xx 的 error 字段透传到可读错误"""
    denied = FakeResponse(403, {"ok": False, "error": "发送目标被拒绝：不在白名单"})
    client = _client_with([denied])
    with pytest.raises(RuntimeError) as exc_info:
        client.post("messages/send", {})
    assert "HTTP 403" in str(exc_info.value)
    assert "不在白名单" in str(exc_info.value)


def test_bridge_client_401_mentions_token_check():
    unauthorized = FakeResponse(401, {"ok": False, "error": "鉴权失败：缺少或错误的 X-ApiMCP-Token 请求头"})
    client = _client_with([unauthorized])
    with pytest.raises(RuntimeError) as exc_info:
        client.post("modules/list")
    message = str(exc_info.value)
    assert "401" in message
    assert "ERISPULSE_APIMCP_TOKEN" in message
    assert "临时 token" not in message, "不再有临时 token 话术"


def test_bridge_client_offline_error_is_readable():
    """机器人不在线（真实拒绝/超时）：返回面向 AI 的可读错误（含排查指引）"""
    client = BridgeClient("http://127.0.0.1:1", "t", timeout=2)
    with pytest.raises((BotOfflineError, RuntimeError)) as exc_info:
        client.post("modules/list")
    message = str(exc_info.value)
    # Windows 防火墙可能把拒绝拖成超时：两种可读出口均合法
    assert ("无法连接" in message and "127.0.0.1:1" in message) or "机器人繁忙" in message


def test_bridge_client_url_joining():
    client = BridgeClient("http://127.0.0.1:8000/", "t")
    assert client.base_url == "http://127.0.0.1:8000"


def test_bridge_client_default_timeout_is_10s():
    assert BridgeClient("http://127.0.0.1:8000", "t").timeout == 10.0


# ---------------- URL 格式校验（P2-6）----------------


def test_bridge_client_rejects_url_without_scheme():
    """无 scheme 地址（localhost:8000 会被误解析成 scheme=localhost）：拒绝"""
    for bad in ("localhost:8000", "127.0.0.1:8000", "example.com", "tcp://x", ""):
        with pytest.raises(ValueError, match="http"):
            BridgeClient(bad, "t")


def test_bridge_client_accepts_valid_schemes():
    for ok_url in ("http://127.0.0.1:8000", "https://bot.example.com", "https://host:8443/base"):
        client = BridgeClient(ok_url, "t")
        assert client._scheme in ("http", "https")


def test_main_rejects_bad_url_with_stderr_and_exit_code(capsys):
    """main 对无 scheme 的 --url：stderr 明确报错并退出（不进入 server.run）"""
    code = main(["--url", "localhost:8000", "--token", "t"])
    assert code == 2
    err = capsys.readouterr().err
    assert "http://" in err
    assert "localhost:8000" in err


# ---------------- stdio server ----------------


async def test_build_mcp_server_constructs():
    client = BridgeClient("http://127.0.0.1:8000", "t")
    server = build_mcp_server(client)
    assert server is not None
    # 全部 14 个 tool 已注册（list_tools 为 mcp 1.x/2.x 公共异步 API）
    tools = await server.list_tools()
    names = sorted(t.name for t in tools)
    assert len(names) == 14, f"应注册 14 个 tool，实际: {names}"
    assert "send_message" in names and "poll_events" in names and "sdk_health" in names


def test_main_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--url" in out and "--token" in out


def test_main_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
