"""token 鉴权测试：401（缺失/错误）、200（正确）、未配置态（503/health 探活 200）"""

from conftest import TOKEN, FakeRequest

from ErisPulse_ApiMCP.Core import Main


async def test_missing_token_gets_401(bridge):
    body, status = await bridge.handle_request("health", FakeRequest(token=None))
    assert status == 401
    assert body["ok"] is False
    assert "X-ApiMCP-Token" in body["error"]


async def test_wrong_token_gets_401(bridge):
    body, status = await bridge.handle_request("health", FakeRequest(token="wrong-token"))
    assert status == 401
    assert body["ok"] is False


async def test_non_ascii_token_gets_401_not_500(bridge):
    """P1-2 回归：非 ASCII 请求头 token —— bytes 比较 + try 兜底，401 而非异常穿透"""
    for weird in ("令牌-中文", "tokéñ", "令牌\x00"):
        body, status = await bridge.handle_request("health", FakeRequest(token=weird))
        assert status == 401, f"非 ASCII token {weird!r} 应 401"
        assert body["ok"] is False
        assert "X-ApiMCP-Token" in body["error"]


async def test_non_ascii_configured_token_roundtrip(fake_sdk):
    """非 ASCII 配置 token：携带正确值仍可 200（bytes 比较不破坏合法令牌）"""
    mod = Main()
    mod.sdk = fake_sdk
    mod.logger = fake_sdk.logger.get_child("ApiMCP")
    mod._conf = lambda: Main.ConfigClass(token="中文令牌-安全")
    await mod.on_load({"module_name": "ApiMCP"})
    try:
        body, status = await mod._bridge.handle_request("health", FakeRequest(token="中文令牌-安全"))
        assert status == 200
        assert body["ok"] is True
    finally:
        await mod.on_unload({"module_name": "ApiMCP"})


async def test_correct_token_gets_200(bridge):
    body, status = await bridge.handle_request("health", FakeRequest(token=TOKEN))
    assert status == 200
    assert body["ok"] is True
    data = body["data"]
    assert data["sdk_version"] == "2.11.0-test"
    assert data["auth_configured"] is True
    assert data["uptime_seconds"] is not None
    assert data["modules"]["registered"] >= 1
    assert data["adapters"]["onebot11"] == "started"


async def test_unknown_action_404(bridge):
    body, status = await bridge.handle_request("nope/none", FakeRequest(token=TOKEN))
    assert status == 404
    assert "未知端点" in body["error"]


async def test_non_object_body_400(bridge):
    body, status = await bridge.handle_request("modules/list", FakeRequest(token=TOKEN, body=[1, 2, 3]))
    assert status == 400
    assert "JSON 对象" in body["error"]


# ---------------- 未配置态：不生成临时 token，桥 503 ----------------


async def _unconfigured_module(fake_sdk):
    mod = Main()
    mod.sdk = fake_sdk
    mod.logger = fake_sdk.logger.get_child("ApiMCP")
    mod._conf = lambda: Main.ConfigClass(token="")
    await mod.on_load({"module_name": "ApiMCP"})
    return mod


async def test_unconfigured_token_returns_503_with_guidance(fake_sdk):
    """token 未配置：不生成临时 token；端点 503 + 可读指引"""
    mod = await _unconfigured_module(fake_sdk)
    try:
        assert mod.token == "", "未配置时不得生成临时 token"
        bridge = mod._bridge
        body, status = await bridge.handle_request("modules/list", FakeRequest(token=None))
        assert status == 503
        assert body["ok"] is False
        assert "[ApiMCP] token" in body["error"]
        assert "ERISPULSE_APIMCP_TOKEN" in body["error"]

        # 带任意 token 请求同样 503（未配置门禁先于鉴权）
        body, status = await bridge.handle_request("modules/list", FakeRequest(token="whatever"))
        assert status == 503
    finally:
        await mod.on_unload({"module_name": "ApiMCP"})


async def test_unconfigured_health_probe_still_200(fake_sdk):
    """未配置时 health 探活仍 200：区分「桥未配置」与「机器人不在线」"""
    mod = await _unconfigured_module(fake_sdk)
    try:
        body, status = await mod._bridge.handle_request("health", FakeRequest(token=None))
        assert status == 200
        assert body["ok"] is True
        assert body["data"]["auth_configured"] is False
        assert body["data"]["status"] == "unconfigured"
        assert "ERISPULSE_APIMCP_TOKEN" in body["data"]["message"]
    finally:
        await mod.on_unload({"module_name": "ApiMCP"})


async def test_unconfigured_logs_warning_without_any_token_value(fake_sdk):
    """未配置提示走 log.warning，且不含任何 token 值（本就不生成）"""
    mod = await _unconfigured_module(fake_sdk)
    try:
        warnings = [msg for level, msg in fake_sdk.logger.records if level == "warning"]
        assert any("ERISPULSE_APIMCP_TOKEN" in msg for msg in warnings), "应有一条未配置 warning 指引"
        # 无临时 token 存在，自然无从泄漏；日志不应出现「生成」字样
        assert not any("生成" in msg for _, msg in fake_sdk.logger.records)
    finally:
        await mod.on_unload({"module_name": "ApiMCP"})


async def test_token_set_by_hot_update_activates_bridge(fake_sdk):
    """未配置 → 热更新补 token：桥即刻启用"""
    mod = await _unconfigured_module(fake_sdk)
    try:
        bridge = mod._bridge
        body, status = await bridge.handle_request("modules/list", FakeRequest(token=TOKEN))
        assert status == 503

        mod._conf = lambda: Main.ConfigClass(token=TOKEN)
        mod.on_config_update(None, None)
        assert mod.token == TOKEN
        body, status = await bridge.handle_request("modules/list", FakeRequest(token=TOKEN))
        assert status == 200
    finally:
        await mod.on_unload({"module_name": "ApiMCP"})


async def test_token_cleared_by_hot_update_deactivates_bridge(loaded, fake_sdk):
    """已配置 → 热更新清空 token：桥回到未配置态（503），health 探活 200"""
    bridge = loaded._bridge
    body, status = await bridge.handle_request("modules/list", FakeRequest(token=TOKEN))
    assert status == 200

    loaded._conf = lambda: Main.ConfigClass(token="")
    loaded.on_config_update(None, None)
    assert loaded.token == ""
    body, status = await bridge.handle_request("modules/list", FakeRequest(token=TOKEN))
    assert status == 503
    body, status = await bridge.handle_request("health", FakeRequest(token=None))
    assert status == 200
    assert body["data"]["auth_configured"] is False
    warnings = [msg for level, msg in fake_sdk.logger.records if level == "warning"]
    assert any("清空" in msg or "未配置" in msg for msg in warnings)


async def test_unconfigured_update_does_not_repeat_warning(fake_sdk):
    """未配置状态下无关配置热更新：不重复未配置告警"""
    mod = await _unconfigured_module(fake_sdk)
    try:
        mod._conf = lambda: Main.ConfigClass(token="", event_buffer_size=500)
        mod.on_config_update(None, None)
        assert mod._buffer.maxlen == 500
        warnings = [msg for level, msg in fake_sdk.logger.records if level == "warning"]
        assert len([m for m in warnings if "ERISPULSE_APIMCP_TOKEN" in m]) == 1
    finally:
        await mod.on_unload({"module_name": "ApiMCP"})


# ---------------- 环境变量覆盖路径（frame_config 内存合并语义）----------------


def test_token_env_override_applies_at_config_read(monkeypatch):
    """ERISPULSE_APIMCP_TOKEN 覆盖在配置读取（dict_to_dataclass）时已生效，
    _resolve_token 拿到的 conf.token 即最终值（P2-4 注释的运行时验证）"""
    from ErisPulse.Core.Bases.config_schema import dict_to_dataclass

    monkeypatch.setenv("ERISPULSE_APIMCP_TOKEN", "env-token-abc123")
    conf = dict_to_dataclass(Main.ConfigClass, {"token": ""})
    assert conf.token == "env-token-abc123", "env 应覆盖空配置值"

    conf2 = dict_to_dataclass(Main.ConfigClass, {"token": "from-toml"})
    assert conf2.token == "env-token-abc123", "env 应覆盖 config.toml 值"
