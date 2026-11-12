"""模块管理端点测试：call_module 白名单、reload 开关、info/list"""

from conftest import TOKEN, FakeRequest

from ErisPulse_ApiMCP.Core import Main


async def test_list_modules(bridge):
    body, status = await bridge.handle_request("modules/list", FakeRequest(token=TOKEN))
    assert status == 200
    names = [row["name"] for row in body["data"]["modules"]]
    assert "Safe" in names and "Other" in names


async def test_get_module_info(bridge):
    body, status = await bridge.handle_request(
        "modules/info", FakeRequest(token=TOKEN, body={"name": "Safe"})
    )
    assert status == 200
    assert body["data"]["meta"]["name"] == "Safe"

    body, status = await bridge.handle_request(
        "modules/info", FakeRequest(token=TOKEN, body={"name": "Ghost"})
    )
    assert status == 404
    assert "未注册" in body["error"]
    assert "Safe" in body["error"], "报错应列出已注册模块，便于 AI 自纠"


async def test_get_module_info_missing_param(bridge):
    body, status = await bridge.handle_request("modules/info", FakeRequest(token=TOKEN, body={}))
    assert status == 400
    assert "name" in body["error"]


# ---------------- call_module 白名单 ----------------


async def test_call_module_without_whitelist_allows_all(bridge, loaded):
    body, status = await bridge.handle_request(
        "modules/call",
        FakeRequest(token=TOKEN, body={"name": "Other", "method": "run", "args": [1], "kwargs": {"k": "v"}}),
    )
    assert status == 200
    assert body["data"]["result"]["echo"] == "run"
    assert loaded.sdk.module.calls[-1] == ("Other", "run", (1,), {"k": "v"})


async def test_call_module_whitelist_denies_with_readable_error(loaded):
    loaded._conf = lambda: Main.ConfigClass(token=TOKEN, module_call_whitelist=["Safe.echo"])
    bridge = loaded._bridge

    body, status = await bridge.handle_request(
        "modules/call", FakeRequest(token=TOKEN, body={"name": "Other", "method": "run"})
    )
    assert status == 403
    assert "白名单" in body["error"]
    assert "Other.run" in body["error"]
    assert loaded.sdk.module.calls == [], "白名单外调用不应触达 SDK"

    # 模块级条目放行该模块全部公开方法
    loaded._conf = lambda: Main.ConfigClass(token=TOKEN, module_call_whitelist=["Safe"])
    body, status = await bridge.handle_request(
        "modules/call", FakeRequest(token=TOKEN, body={"name": "Safe", "method": "anything"})
    )
    assert status == 200

    # 方法级条目精确放行
    loaded._conf = lambda: Main.ConfigClass(token=TOKEN, module_call_whitelist=["Safe.echo", "Other"])
    body, status = await bridge.handle_request(
        "modules/call", FakeRequest(token=TOKEN, body={"name": "Safe", "method": "echo"})
    )
    assert status == 200


async def test_call_module_rejects_private_method(loaded):
    loaded._conf = lambda: Main.ConfigClass(token=TOKEN, module_call_whitelist=["Boom"])
    bridge = loaded._bridge
    body, status = await bridge.handle_request(
        "modules/call", FakeRequest(token=TOKEN, body={"name": "Boom", "method": "_secret"})
    )
    assert status == 403
    assert "私有方法" in body["error"]


async def test_call_module_sdk_error_is_readable(bridge, loaded, fake_sdk):
    """被调模块异常：只回异常类型，全文（可能含路径/SQL/凭据）只进日志"""
    body, status = await bridge.handle_request(
        "modules/call", FakeRequest(token=TOKEN, body={"name": "Boom", "method": "run"})
    )
    assert status == 400
    assert "Boom.run" in body["error"]
    assert "RuntimeError" in body["error"]
    assert "boom from module" not in body["error"], "异常详情不得回给客户端"
    errors = [msg for level, msg in fake_sdk.logger.records if level == "error"]
    assert any("boom from module" in msg for msg in errors), "完整 repr 应进 logger.error"


async def test_call_module_bad_args_shape(bridge):
    body, status = await bridge.handle_request(
        "modules/call", FakeRequest(token=TOKEN, body={"name": "Safe", "method": "echo", "args": "nope"})
    )
    assert status == 400
    assert "args" in body["error"]


# ---------------- rpc_timeout（P2-1）----------------


async def test_call_module_rpc_timeout_passthrough(bridge, loaded):
    """rpc_timeout 请求字段透传给 sdk.module.call 的原生 timeout 形参"""
    body, status = await bridge.handle_request(
        "modules/call",
        FakeRequest(token=TOKEN, body={"name": "Safe", "method": "run", "rpc_timeout": 1.5}),
    )
    assert status == 200
    name, method, args, kwargs = loaded.sdk.module.calls[-1]
    assert kwargs.get("timeout") == 1.5


async def test_call_module_rpc_timeout_accepts_numeric_string(bridge, loaded):
    body, status = await bridge.handle_request(
        "modules/call",
        FakeRequest(token=TOKEN, body={"name": "Safe", "method": "run", "rpc_timeout": "2"}),
    )
    assert status == 200
    assert loaded.sdk.module.calls[-1][3].get("timeout") == 2.0


async def test_call_module_rpc_timeout_validation(bridge):
    for bad in ("abc", 0, -1, True):
        body, status = await bridge.handle_request(
            "modules/call",
            FakeRequest(token=TOKEN, body={"name": "Safe", "method": "run", "rpc_timeout": bad}),
        )
        assert status == 400, f"rpc_timeout={bad!r} 应被拒绝"
        assert "rpc_timeout" in body["error"]


async def test_call_module_strips_kwargs_timeout_with_note(bridge, loaded):
    """kwargs.timeout 被 SDK module.call 形参吞噬：剔除并在响应 note 提示"""
    body, status = await bridge.handle_request(
        "modules/call",
        FakeRequest(token=TOKEN, body={
            "name": "Safe", "method": "run", "kwargs": {"timeout": 5, "k": "v"},
        }),
    )
    assert status == 200
    assert "不支持透传" in body["data"]["note"]
    assert "rpc_timeout" in body["data"]["note"]
    # 剔除后不再出现在触达 SDK 的 kwargs
    name, method, args, kwargs = loaded.sdk.module.calls[-1]
    assert kwargs == {"k": "v"}


async def test_call_module_without_rpc_timeout_omits_timeout(bridge, loaded):
    """未提供 rpc_timeout 时不注入 timeout（保持 SDK 默认语义）"""
    body, status = await bridge.handle_request(
        "modules/call", FakeRequest(token=TOKEN, body={"name": "Other", "method": "run"})
    )
    assert status == 200
    assert "timeout" not in loaded.sdk.module.calls[-1][3]


# ---------------- reload_module 开关 ----------------


async def test_reload_module_allowed_by_default(bridge, loaded):
    body, status = await bridge.handle_request(
        "modules/reload", FakeRequest(token=TOKEN, body={"name": "Safe"})
    )
    assert status == 200
    assert body["data"]["reloaded"] is True
    assert loaded.sdk.module.reload_calls == ["Safe"]


async def test_reload_module_denied_when_disabled(loaded):
    loaded._conf = lambda: Main.ConfigClass(token=TOKEN, allow_module_reload=False)
    bridge = loaded._bridge
    body, status = await bridge.handle_request(
        "modules/reload", FakeRequest(token=TOKEN, body={"name": "Safe"})
    )
    assert status == 403
    assert "allow_module_reload" in body["error"]
    assert loaded.sdk.module.reload_calls == []
