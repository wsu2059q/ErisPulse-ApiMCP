"""事件环形缓冲与订阅流测试：环形淘汰、增量游标、订阅过滤"""

import pytest

from conftest import TOKEN, FakeRequest

from ErisPulse_ApiMCP.Core import EventBuffer, Main
from ErisPulse_ApiMCP.bridge import BridgeError


def _event(etype="message", **extra):
    return {"type": etype, "platform": "onebot11", **extra}


# ---------------- EventBuffer 单元测试 ----------------


def test_buffer_evicts_oldest_when_full():
    buf = EventBuffer(5)
    for i in range(10):
        buf.append({"type": "message", "n": i})
    assert len(buf) == 5
    assert buf.dropped == 5
    remaining = buf.after(0.0, 0, 100)
    assert [e["n"] for e in remaining] == [5, 6, 7, 8, 9], "最旧的 5 条应被环形淘汰"


def test_buffer_cursor_incremental():
    buf = EventBuffer(100)
    first = buf.append(_event())
    second = buf.append(_event())
    # 游标取第一条之后：只剩第二条
    rest = buf.after(first["ts"], first["seq"], 100)
    assert len(rest) == 1
    assert rest[0]["seq"] == second["seq"]


def test_buffer_resize_keeps_recent():
    buf = EventBuffer(100)
    for i in range(10):
        buf.append({"type": "message", "n": i})
    buf.resize(3)
    assert len(buf) == 3
    assert buf.dropped == 7
    assert [e["n"] for e in buf.after(0.0, 0, 10)] == [7, 8, 9]


def test_buffer_poll_limit():
    buf = EventBuffer(100)
    for _ in range(10):
        buf.append(_event())
    assert len(buf.after(0.0, 0, 3)) == 3


# ---------------- 订阅流（经模块处理器）----------------


async def test_no_subscription_means_no_buffering(loaded):
    await loaded._on_any_event(_event())
    assert len(loaded._buffer) == 0


async def test_subscribe_buffer_poll_flow(loaded):
    result = loaded.subscribe_events(["message"])
    sid = result["subscription_id"]
    assert result["types"] == ["message"]
    assert result["active_subscriptions"] == [{"id": sid, "types": ["message"]}]

    # 未订阅类型不入缓冲
    await loaded._on_any_event(_event("notice"))
    assert len(loaded._buffer) == 0

    # 订阅类型入缓冲
    await loaded._on_any_event(_event("message", text="hi"))
    assert len(loaded._buffer) == 1

    out = loaded.poll_events()
    assert out["count"] == 1
    assert out["events"][0]["type"] == "message"
    assert out["events"][0]["text"] == "hi"
    assert out["active_subscriptions"] == [{"id": sid, "types": ["message"]}]

    # 增量游标：上次游标之后无新事件
    out2 = loaded.poll_events(since_ts=out["next_since_ts"], since_seq=out["next_since_seq"])
    assert out2["count"] == 0

    # 取消订阅后停止缓冲
    assert loaded.unsubscribe(sid) is True
    assert loaded.unsubscribe(sid) is False
    await loaded._on_any_event(_event("message"))
    assert len(loaded._buffer) == 1


async def test_subscribe_unsubscribe_return_all_active_subscriptions(loaded):
    """订阅/取消都返回当前全部活跃订阅（id + types），与 poll_events 对齐"""
    first = loaded.subscribe_events(["message"])
    # 首次订阅时只有自己
    assert first["active_subscriptions"] == [
        {"id": first["subscription_id"], "types": ["message"]},
    ]
    second = loaded.subscribe_events(["notice", "message"])
    # 第二次订阅时能看到全部两个
    assert second["active_subscriptions"] == [
        {"id": first["subscription_id"], "types": ["message"]},
        {"id": second["subscription_id"], "types": ["notice", "message"]},
    ]

    loaded.unsubscribe(first["subscription_id"])
    remaining = [
        {"id": second["subscription_id"], "types": ["notice", "message"]},
    ]
    assert loaded.active_subscriptions() == remaining
    out = loaded.poll_events()
    assert out["active_subscriptions"] == remaining, "poll_events 与订阅响应同形态"


async def test_wildcard_subscription(loaded):
    loaded.subscribe_events(["*"])
    await loaded._on_any_event(_event("notice"))
    await loaded._on_any_event(_event("message"))
    out = loaded.poll_events()
    assert out["count"] == 2


async def test_subscribe_rejects_empty_and_restricted_types(loaded):
    with pytest.raises(BridgeError) as exc_info:
        loaded.subscribe_events(["  "])
    assert exc_info.value.status == 400

    loaded._conf = lambda: Main.ConfigClass(token=TOKEN, event_types_allowed=["message"])
    with pytest.raises(BridgeError) as exc_info:
        loaded.subscribe_events(["notice"])
    assert exc_info.value.status == 403
    assert "event_types_allowed" in exc_info.value.message


async def test_event_text_truncated(loaded):
    loaded.subscribe_events(["message"])
    await loaded._on_any_event(_event("message", text="x" * 5000))
    out = loaded.poll_events()
    assert len(out["events"][0]["text"]) < 5000
    assert "截断" in out["events"][0]["text"]


async def test_unsubscribe_via_bridge_lists_active(bridge, loaded):
    body, _ = await bridge.handle_request(
        "events/subscribe", FakeRequest(token=TOKEN, body={"types": ["message"]})
    )
    sid = body["data"]["subscription_id"]
    body, status = await bridge.handle_request(
        "events/unsubscribe", FakeRequest(token=TOKEN, body={"subscription_id": sid})
    )
    assert status == 200
    assert body["data"]["unsubscribed"] is True
    assert body["data"]["active_subscriptions"] == [], "取消后无剩余订阅"


# ---------------- poll_events 数值参数容错（P1-1）----------------


async def test_poll_events_accepts_numeric_string_cursors(bridge, loaded):
    """字符串数字游标（"0"/"0"）先转换再使用"""
    loaded.subscribe_events(["message"])
    await loaded._on_any_event(_event("message", text="hi"))
    body, status = await bridge.handle_request(
        "events/poll",
        FakeRequest(token=TOKEN, body={"since_ts": "0", "since_seq": "0", "limit": "10"}),
    )
    assert status == 200
    assert body["data"]["count"] == 1


async def test_poll_events_non_numeric_cursor_400(bridge):
    for bad in ({"since_ts": "abc"}, {"since_seq": "abc"}, {"limit": "many"}):
        body, status = await bridge.handle_request("events/poll", FakeRequest(token=TOKEN, body=bad))
        assert status == 400, f"{bad} 应 400"
        assert "须为数字" in body["error"]


# ---------------- 订阅数上限（P2-2）----------------


async def test_subscription_count_cap_403(loaded):
    """活跃订阅超 32 个：403 可读错误"""
    for i in range(32):
        loaded.subscribe_events([f"type_{i}"])
    with pytest.raises(BridgeError) as exc_info:
        loaded.subscribe_events(["message"])
    assert exc_info.value.status == 403
    assert "32" in exc_info.value.message
    # 取消一个后可再订阅
    first_sid = loaded.subscription_ids()[0]
    loaded.unsubscribe(first_sid)
    result = loaded.subscribe_events(["message"])
    assert result["subscription_id"]


# ---------------- poll_events 按订阅过滤（P2-4）----------------


async def test_poll_events_filters_by_subscription_id(loaded, bridge):
    """subscription_id 过滤：只返回该订阅声明类型的事件"""
    sub_message = loaded.subscribe_events(["message"])
    sub_notice = loaded.subscribe_events(["notice"])
    await loaded._on_any_event(_event("message", text="m1"))
    await loaded._on_any_event(_event("notice"))
    await loaded._on_any_event(_event("message", text="m2"))

    out = loaded.poll_events(subscription_id=sub_message["subscription_id"])
    assert [e.get("text") for e in out["events"]] == ["m1", "m2"]
    assert all(e["type"] == "message" for e in out["events"])

    out = loaded.poll_events(subscription_id=sub_notice["subscription_id"])
    assert [e["type"] for e in out["events"]] == ["notice"]

    # 通配订阅不过滤
    sub_all = loaded.subscribe_events(["*"])
    out = loaded.poll_events(subscription_id=sub_all["subscription_id"])
    assert out["count"] == 3

    # 经 bridge 端点同样生效
    body, status = await bridge.handle_request(
        "events/poll",
        FakeRequest(token=TOKEN, body={"subscription_id": sub_message["subscription_id"]}),
    )
    assert status == 200
    assert body["data"]["count"] == 2


async def test_poll_events_unknown_subscription_id_404(loaded, bridge):
    with pytest.raises(BridgeError) as exc_info:
        loaded.poll_events(subscription_id="sub_0000")
    assert exc_info.value.status == 404

    body, status = await bridge.handle_request(
        "events/poll", FakeRequest(token=TOKEN, body={"subscription_id": "sub_0000"})
    )
    assert status == 404
    assert "不存在" in body["error"]


# ---------------- poll_events 淘汰可见性（dropped / warning）----------------


async def test_poll_events_reports_dropped_and_overflow_warning(loaded):
    loaded._conf = lambda: Main.ConfigClass(token=TOKEN, event_buffer_size=10)
    loaded.on_config_update(None, None)  # 缩容生效
    loaded.subscribe_events(["message"])
    for i in range(12):
        await loaded._on_any_event(_event("message", text=f"m{i}"))

    # 第一次轮询：有新事件（10 条），dropped=2 可见，但无 warning
    body, status = await loaded._bridge.handle_request("events/poll", FakeRequest(token=TOKEN, body={}))
    assert status == 200
    data = body["data"]
    assert data["count"] == 10
    assert data["dropped"] == 2
    assert "warning" not in data

    # 再压入 2 条触发新淘汰，游标推进后轮询：仍有事件，无 warning
    for i in range(2):
        await loaded._on_any_event(_event("message", text=f"late{i}"))
    body, _ = await loaded._bridge.handle_request(
        "events/poll",
        FakeRequest(token=TOKEN, body={"since_ts": data["next_since_ts"], "since_seq": data["next_since_seq"]}),
    )
    assert body["data"]["count"] == 2
    assert body["data"]["dropped"] == 4
    assert "warning" not in body["data"]
    cursor = (body["data"]["next_since_ts"], body["data"]["next_since_seq"])

    # 无新事件 + 淘汰未增加：无 warning；随后缩容再淘汰 → 无新事件但 dropped 增加 → warning
    body, _ = await loaded._bridge.handle_request(
        "events/poll", FakeRequest(token=TOKEN, body={"since_ts": cursor[0], "since_seq": cursor[1]})
    )
    assert body["data"]["count"] == 0
    assert "warning" not in body["data"]

    # 直接缩容触发淘汰（绕过配置收敛下限 10，模拟持续溢出后的静默窗口）
    loaded._buffer.resize(6)
    assert loaded._buffer.dropped == 8

    body, _ = await loaded._bridge.handle_request(
        "events/poll", FakeRequest(token=TOKEN, body={"since_ts": cursor[0], "since_seq": cursor[1]})
    )
    assert body["data"]["count"] == 0
    assert body["data"]["dropped"] == 8
    assert "warning" in body["data"]
    assert "event_buffer_size" in body["data"]["warning"]

    # 淘汰数不再变化后重复轮询：warning 不重复
    body, _ = await loaded._bridge.handle_request(
        "events/poll", FakeRequest(token=TOKEN, body={"since_ts": cursor[0], "since_seq": cursor[1]})
    )
    assert "warning" not in body["data"]


# ---------------- 事件 message 段净化（P1-4）----------------


async def test_event_message_segments_sanitized_to_summary(loaded):
    """含 base64 / 大文本段的事件入缓冲后读取为摘要形态"""
    loaded.subscribe_events(["message"])
    big = "QUFB" * 500  # 2000 字符 base64 样貌
    event = _event(
        "message",
        message=[
            {"type": "text", "data": {"text": "好" * 600}},
            {"type": "image", "data": {"file": big, "url": "https://cdn.example.com/" + big[:100]}},
            {"type": "mention", "data": {"user_id": "u1", "qq": "123"}},
        ],
    )
    await loaded._on_any_event(event)

    out = loaded.poll_events()
    msg = out["events"][0]["message"]
    assert isinstance(msg, list) and len(msg) == 3

    # 文本段：保留 type 与截断后的摘要文本
    assert msg[0]["type"] == "text"
    text = msg[0]["data"]["text"]
    assert len(text) < 600 and text.startswith("好" * 100)
    assert "截断" in text

    # 图片段：file / url 大块字段替换为占位（保留体量信息），不泄漏内容
    url_value = "https://cdn.example.com/" + big[:100]
    assert msg[1]["data"]["file"] == f"<omitted:len={len(big)}>"
    assert msg[1]["data"]["url"] == f"<omitted:len={len(url_value)}>"
    assert "QUFB" not in str(msg)

    # 普通小字段原样保留
    assert msg[2]["data"]["user_id"] == "u1"


async def test_event_message_segment_count_truncated(loaded):
    loaded.subscribe_events(["message"])
    segments = [{"type": "text", "data": {"text": f"seg{i}"}} for i in range(60)]
    await loaded._on_any_event(_event("message", message=segments))
    out = loaded.poll_events()
    msg = out["events"][0]["message"]
    assert len(msg) == 51, "50 段 + 1 个省略标记"
    assert msg[-1]["type"] == "omitted"
    assert msg[-1]["count"] == 60
    assert "截断" in msg[-1]["note"]


async def test_event_message_string_form_truncated(loaded):
    loaded.subscribe_events(["message"])
    await loaded._on_any_event(_event("message", message="字" * 3000))
    out = loaded.poll_events()
    msg = out["events"][0]["message"]
    assert len(msg) < 3000
    assert "截断" in msg


# ---------------- REST 端点（事件）----------------


async def test_events_endpoints_via_bridge(bridge):
    # 无 types 参数 → 可读 400
    body, status = await bridge.handle_request("events/subscribe", FakeRequest(token=TOKEN, body={}))
    assert status == 400
    assert "types" in body["error"]

    # 正常订阅
    body, status = await bridge.handle_request(
        "events/subscribe", FakeRequest(token=TOKEN, body={"types": ["message"]})
    )
    assert status == 200
    sid = body["data"]["subscription_id"]

    # 轮询（空）
    body, status = await bridge.handle_request("events/poll", FakeRequest(token=TOKEN, body={}))
    assert status == 200
    assert body["data"]["count"] == 0

    # 取消订阅：不存在 → 404 可读
    body, status = await bridge.handle_request(
        "events/unsubscribe", FakeRequest(token=TOKEN, body={"subscription_id": "sub_0000"})
    )
    assert status == 404
    assert "不存在" in body["error"]

    body, status = await bridge.handle_request(
        "events/unsubscribe", FakeRequest(token=TOKEN, body={"subscription_id": sid})
    )
    assert status == 200
