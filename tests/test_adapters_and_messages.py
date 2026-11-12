"""适配器管理 + 消息收发端点测试"""

from conftest import TOKEN, FakeRequest

from ErisPulse_ApiMCP.Core import Main


# ---------------- 适配器 ----------------


async def test_list_adapters(bridge):
    body, status = await bridge.handle_request("adapters/list", FakeRequest(token=TOKEN))
    assert status == 200
    row = body["data"]["adapters"][0]
    assert row["platform"] == "onebot11"
    assert row["status"] == "started"
    assert row["bots_online"] == ["10001"]


async def test_get_adapter_status(bridge):
    body, status = await bridge.handle_request(
        "adapters/status", FakeRequest(token=TOKEN, body={"platform": "onebot11"})
    )
    assert status == 200
    assert body["data"]["status"]["status"] == "started"

    body, status = await bridge.handle_request(
        "adapters/status", FakeRequest(token=TOKEN, body={"platform": "ghost"})
    )
    assert status == 404
    assert "未注册" in body["error"]
    assert "onebot11" in body["error"], "报错应列出已注册平台"


async def test_reload_adapter_denied_by_default(loaded):
    """allow_adapter_reload 默认 false：拒绝且给出开启方法"""
    bridge = loaded._bridge
    body, status = await bridge.handle_request(
        "adapters/reload", FakeRequest(token=TOKEN, body={"platform": "onebot11"})
    )
    assert status == 403
    assert "allow_adapter_reload" in body["error"]
    assert "默认禁用" in body["error"]
    assert loaded.sdk.adapter.reload_calls == []


async def test_reload_adapter_allowed_when_enabled(loaded):
    loaded._conf = lambda: Main.ConfigClass(token=TOKEN, allow_adapter_reload=True)
    bridge = loaded._bridge
    body, status = await bridge.handle_request(
        "adapters/reload", FakeRequest(token=TOKEN, body={"platform": "onebot11"})
    )
    assert status == 200
    assert body["data"]["reloaded"] is True
    assert loaded.sdk.adapter.reload_calls == ["onebot11"]


# ---------------- send_message ----------------


async def test_send_message_text(bridge, loaded):
    body, status = await bridge.handle_request(
        "messages/send",
        FakeRequest(
            token=TOKEN,
            body={
                "platform": "onebot11",
                "target_type": "group",
                "target_id": 123,
                "method": "Text",
                "content": "你好，世界",
            },
        ),
    )
    assert status == 200
    assert body["data"]["message_id"] == "msg_fake_001"
    sent = loaded.sdk.adapter.adapters["onebot11"].sent
    assert ("To", "group", "123") in sent
    assert ("Text", "你好，世界") in sent


async def test_send_message_unknown_platform(bridge):
    body, status = await bridge.handle_request(
        "messages/send",
        FakeRequest(token=TOKEN, body={
            "platform": "ghost", "target_type": "user", "target_id": "1",
            "method": "Text", "content": "hi",
        }),
    )
    assert status == 404
    assert "未注册" in body["error"]
    assert "onebot11" in body["error"]


async def test_send_message_unknown_method(bridge):
    body, status = await bridge.handle_request(
        "messages/send",
        FakeRequest(token=TOKEN, body={
            "platform": "onebot11", "target_type": "user", "target_id": "1",
            "method": "Nope", "content": "hi",
        }),
    )
    assert status == 400
    assert "不支持发送方法" in body["error"]
    assert "Text" in body["error"], "报错应列出可用发送方法"


async def test_send_message_non_awaitable_method_rejected(bridge):
    """链式/同步方法（返回非 Awaitable）必须被拒绝，避免静默无发送"""
    body, status = await bridge.handle_request(
        "messages/send",
        FakeRequest(token=TOKEN, body={
            "platform": "onebot11", "target_type": "user", "target_id": "1",
            "method": "Sticker", "content": "hi",
        }),
    )
    assert status == 400
    assert "未产生发送动作" in body["error"]


async def test_send_message_missing_params(bridge):
    body, status = await bridge.handle_request(
        "messages/send", FakeRequest(token=TOKEN, body={"platform": "onebot11"})
    )
    assert status == 400
    assert "target_type" in body["error"]


# ---------------- send_message 目标校验（P0-2）----------------


async def test_send_message_invalid_target_type_400(bridge):
    body, status = await bridge.handle_request(
        "messages/send",
        FakeRequest(token=TOKEN, body={
            "platform": "onebot11", "target_type": "private_chat", "target_id": "1",
            "method": "Text", "content": "hi",
        }),
    )
    assert status == 400
    assert "target_type" in body["error"]
    assert "user" in body["error"] and "group" in body["error"], "报错应列出允许值"
    # 未触达适配器
    assert bridge.sdk.adapter.adapters["onebot11"].sent == []


async def test_send_message_private_target_type_accepted(bridge, loaded):
    """private 属允许集（SDK SendDSL.To 自动转换为 user）"""
    body, status = await bridge.handle_request(
        "messages/send",
        FakeRequest(token=TOKEN, body={
            "platform": "onebot11", "target_type": "private", "target_id": "42",
            "method": "Text", "content": "hi",
        }),
    )
    assert status == 200
    assert ("To", "private", "42") in loaded.sdk.adapter.adapters["onebot11"].sent


async def test_send_message_bad_target_id_400(bridge):
    for bad_id in ("", "   ", "a" * 129, "bad\x00id", "bad\nid"):
        body, status = await bridge.handle_request(
            "messages/send",
            FakeRequest(token=TOKEN, body={
                "platform": "onebot11", "target_type": "user", "target_id": bad_id,
                "method": "Text", "content": "hi",
            }),
        )
        assert status == 400, f"target_id={bad_id!r} 应被拒绝"
        assert "target_id" in body["error"]


async def test_send_target_whitelist_exact_hit(loaded):
    loaded._conf = lambda: Main.ConfigClass(token=TOKEN, send_target_whitelist=["user:123"])
    bridge = loaded._bridge
    body, status = await bridge.handle_request(
        "messages/send",
        FakeRequest(token=TOKEN, body={
            "platform": "onebot11", "target_type": "user", "target_id": "123",
            "method": "Text", "content": "hi",
        }),
    )
    assert status == 200
    # 精确条目不放行其它 ID
    body, status = await bridge.handle_request(
        "messages/send",
        FakeRequest(token=TOKEN, body={
            "platform": "onebot11", "target_type": "user", "target_id": "456",
            "method": "Text", "content": "hi",
        }),
    )
    assert status == 403
    assert "send_target_whitelist" in body["error"]


async def test_send_target_whitelist_wildcard(loaded):
    loaded._conf = lambda: Main.ConfigClass(token=TOKEN, send_target_whitelist=["group:*"])
    bridge = loaded._bridge
    for gid in ("1", "999", "123456"):
        body, status = await bridge.handle_request(
            "messages/send",
            FakeRequest(token=TOKEN, body={
                "platform": "onebot11", "target_type": "group", "target_id": gid,
                "method": "Text", "content": "hi",
            }),
        )
        assert status == 200, f"group:{gid} 应被通配放行"
    # 通配仅限同类型：user 不被 group:* 放行
    body, status = await bridge.handle_request(
        "messages/send",
        FakeRequest(token=TOKEN, body={
            "platform": "onebot11", "target_type": "user", "target_id": "1",
            "method": "Text", "content": "hi",
        }),
    )
    assert status == 403


async def test_send_target_whitelist_empty_means_unrestricted(loaded):
    """空白名单 = 不限（保持现能力）"""
    loaded._conf = lambda: Main.ConfigClass(token=TOKEN, send_target_whitelist=[])
    bridge = loaded._bridge
    body, status = await bridge.handle_request(
        "messages/send",
        FakeRequest(token=TOKEN, body={
            "platform": "onebot11", "target_type": "user", "target_id": "anyone",
            "method": "Text", "content": "hi",
        }),
    )
    assert status == 200
    # 白名单外拒绝不应触达适配器（上面 403 用例已验证，这里补触达确认）
    assert ("To", "user", "anyone") in loaded.sdk.adapter.adapters["onebot11"].sent


async def test_send_target_whitelist_denied_does_not_touch_adapter(loaded):
    loaded._conf = lambda: Main.ConfigClass(token=TOKEN, send_target_whitelist=["user:1"])
    bridge = loaded._bridge
    before = list(loaded.sdk.adapter.adapters["onebot11"].sent)
    body, status = await bridge.handle_request(
        "messages/send",
        FakeRequest(token=TOKEN, body={
            "platform": "onebot11", "target_type": "group", "target_id": "9",
            "method": "Text", "content": "hi",
        }),
    )
    assert status == 403
    assert loaded.sdk.adapter.adapters["onebot11"].sent == before


# ---------------- 体积上限（P2-2）----------------


async def test_send_message_oversized_content_413(bridge, loaded):
    """content 超 32KB：413 拒绝且不触达适配器"""
    big = "x" * (32 * 1024 + 1)
    before = list(loaded.sdk.adapter.adapters["onebot11"].sent)
    body, status = await bridge.handle_request(
        "messages/send",
        FakeRequest(token=TOKEN, body={
            "platform": "onebot11", "target_type": "user", "target_id": "1",
            "method": "Text", "content": big,
        }),
    )
    assert status == 413
    assert "content" in body["error"] or "消息内容过大" in body["error"]
    assert loaded.sdk.adapter.adapters["onebot11"].sent == before


async def test_oversized_body_413(bridge):
    """请求体超 256KB：413 可读错误"""
    huge = {"name": "Safe", "args": ["x" * (256 * 1024)]}
    body, status = await bridge.handle_request("modules/call", FakeRequest(token=TOKEN, body=huge))
    assert status == 413
    assert "请求体过大" in body["error"]


# ---------------- recent_messages ----------------


async def test_recent_messages_by_platform(bridge):
    body, status = await bridge.handle_request(
        "messages/recent", FakeRequest(token=TOKEN, body={"platform": "onebot11", "limit": 10})
    )
    assert status == 200
    texts = [m["text"] for m in body["data"]["messages"]]
    assert texts == ["你好"], "只应返回 onebot11 会话的消息"


async def test_recent_messages_numeric_string_limit_accepted(bridge):
    """P1-1：LLM 常传字符串数字（"50"）——先转换再使用"""
    body, status = await bridge.handle_request(
        "messages/recent", FakeRequest(token=TOKEN, body={"platform": "onebot11", "limit": "50"})
    )
    assert status == 200
    assert body["data"]["count"] == 1


async def test_recent_messages_non_numeric_limit_400(bridge):
    body, status = await bridge.handle_request(
        "messages/recent", FakeRequest(token=TOKEN, body={"limit": "fifty"})
    )
    assert status == 400
    assert "limit" in body["error"]
    assert "须为数字" in body["error"]


async def test_recent_messages_by_session(bridge):
    body, status = await bridge.handle_request(
        "messages/recent",
        FakeRequest(token=TOKEN, body={"session": "telegram:user:200"}),
    )
    assert status == 200
    assert [m["text"] for m in body["data"]["messages"]] == ["hello"]


async def test_recent_messages_session_platform_mismatch_400(bridge):
    """合法格式但前缀与 platform 不匹配：400（防止跨平台越权查询）"""
    body, status = await bridge.handle_request(
        "messages/recent",
        FakeRequest(token=TOKEN, body={"session": "telegram:user:200", "platform": "onebot11"}),
    )
    assert status == 400
    assert "不属于平台" in body["error"]


async def test_recent_messages_invalid_session_format_400(bridge):
    """session 格式非法（非 平台:类型:ID 三段）：400 可读错误"""
    for bad in ("not-a-session", "onebot11:group", "onebot11:group:123:extra:segments?", "onebot11::123"):
        body, status = await bridge.handle_request(
            "messages/recent", FakeRequest(token=TOKEN, body={"session": bad})
        )
        assert status == 400, f"session={bad!r} 应被判格式非法"
        assert "格式非法" in body["error"]
        assert "平台:类型:ID" in body["error"]


async def test_recent_messages_session_unknown_detail_type_400(bridge):
    """中段不在 SDK 会话类型集（notice 语义子类型等）：400"""
    body, status = await bridge.handle_request(
        "messages/recent", FakeRequest(token=TOKEN, body={"session": "onebot11:group_member_increase:1"})
    )
    assert status == 400
    assert "格式非法" in body["error"]


async def test_recent_messages_session_all_known_detail_types_ok(bridge):
    """SDK 会话类型集（private/group/user/channel/guild/thread）全部放行"""
    for detail in ("private", "group", "user", "channel", "guild", "thread"):
        body, status = await bridge.handle_request(
            "messages/recent", FakeRequest(token=TOKEN, body={"session": f"onebot11:{detail}:1"})
        )
        assert status == 200, f"onebot11:{detail}:1 应为合法格式"


# ---------------- topology 脱敏 ----------------


async def test_topology_sanitized(bridge):
    body, status = await bridge.handle_request("topology", FakeRequest(token=TOKEN))
    assert status == 200
    text = str(body)
    assert "secret-should-drop" not in text, "密钥类字段不得外泄"
    assert "/home/user/modules/Safe" not in text, "绝对路径不得外泄"
    assert body["data"]["topology"]["adapters"]["onebot11"]["status"] == "started"
