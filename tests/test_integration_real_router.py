"""真实路由栈集成测试：ApiMCP 路由挂到真 RouterManager/FastAPI 的端到端行为

与其它测试（假 router 直调 handler）不同，本文件走真实 HTTP 栈
（starlette TestClient），验证：
- ``apimcp`` 命名空间路由挂载与路径形态（/apimcp/<action>）
- 401/200/404 经真实 HTTP 层的表现（元组返回约定）
- on_unload 显式注销
"""

import pytest

from conftest import TOKEN

pytest.importorskip("fastapi.testclient")

from ErisPulse.Core.router import router as real_router  # noqa: E402
from ErisPulse.runtime.context import current_owner  # noqa: E402

from ErisPulse_ApiMCP.Core import Main  # noqa: E402


@pytest.fixture
async def real_router_bot(fake_sdk):
    """混合 SDK：router 用真 RouterManager，其余假；模拟框架加载 owner 上下文"""
    fake_sdk.router = real_router
    mod = Main()
    mod.sdk = fake_sdk
    mod.logger = fake_sdk.logger.get_child("ApiMCP")
    mod._conf = lambda: Main.ConfigClass(token=TOKEN)

    token = current_owner.set("ApiMCP")
    try:
        await mod.on_load({"module_name": "ApiMCP"})
    finally:
        current_owner.reset(token)
    yield mod
    await mod.on_unload({"module_name": "ApiMCP"})
    for key in [k for k in real_router._http_routes if k == "apimcp"]:
        real_router._http_routes.pop(key, None)


def _client():
    from fastapi.testclient import TestClient

    return TestClient(real_router.app)


def test_routes_mounted_under_apimcp(real_router_bot):
    paths = [getattr(r, "path", "?") for r in real_router.app.router.routes]
    apimcp = [p for p in paths if p.startswith("/apimcp/")]
    assert len(apimcp) == 17
    assert "/apimcp/health" in apimcp


def test_auth_over_real_http(real_router_bot):
    client = _client()
    resp = client.post("/apimcp/modules/list")
    assert resp.status_code == 401
    assert resp.json()["ok"] is False

    resp = client.post("/apimcp/modules/list", headers={"X-ApiMCP-Token": "bad"})
    assert resp.status_code == 401

    resp = client.post("/apimcp/modules/list", headers={"X-ApiMCP-Token": TOKEN})
    assert resp.status_code == 200
    names = [row["name"] for row in resp.json()["data"]["modules"]]
    assert "Safe" in names


def test_slash_action_mounted_as_literal_multi_segment_path(real_router_bot):
    """P1-1 契约固化：RouterManager 把含斜杠 path（如 /modules/list）作为
    多级字面路径挂载（非单段路径参数）——POST /apimcp/modules/list 命中
    bridge 的 modules/list handler（200，非 404）；未注册的兄弟路径 404"""
    client = _client()
    resp = client.post("/apimcp/modules/list", headers={"X-ApiMCP-Token": TOKEN})
    assert resp.status_code == 200, "含斜杠 action 必须按多级路径命中，而非 404"
    assert resp.json()["ok"] is True

    # 未知多级路径不命中任何 apimcp 路由（证明是字面路径而非前缀捕获）
    resp = client.post("/apimcp/modules/nonexistent", headers={"X-ApiMCP-Token": TOKEN})
    assert resp.status_code == 404
    resp = client.post("/apimcp/deeper/nested/path", headers={"X-ApiMCP-Token": TOKEN})
    assert resp.status_code == 404


def test_health_get_probe_over_real_http(real_router_bot):
    client = _client()
    resp = client.get("/apimcp/health", headers={"X-ApiMCP-Token": TOKEN})
    assert resp.status_code == 200
    assert resp.json()["data"]["sdk_version"] == "2.11.0-test"


def test_subscribe_over_real_http(real_router_bot):
    client = _client()
    resp = client.post(
        "/apimcp/events/subscribe",
        headers={"X-ApiMCP-Token": TOKEN},
        json={"types": ["message"]},
    )
    assert resp.status_code == 200
    sid = resp.json()["data"]["subscription_id"]
    assert sid.startswith("sub_")

    resp = client.post(
        "/apimcp/events/poll",
        headers={"X-ApiMCP-Token": TOKEN},
        json={"since_ts": 0},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["count"] == 0


async def test_unload_unmounts_routes(real_router_bot):
    await real_router_bot.on_unload({"module_name": "ApiMCP"})
    paths = [getattr(r, "path", "?") for r in real_router.app.router.routes]
    assert not [p for p in paths if p.startswith("/apimcp/")], "on_unload 后 /apimcp/* 应全部下线"
    # fixture teardown 会再次 on_unload，顺带覆盖幂等性
