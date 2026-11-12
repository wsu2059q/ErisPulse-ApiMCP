"""新增能力测试：文档检索（自有引擎）/ get_module_info 服务签名 / send_message 账户与签名预检"""

from conftest import TOKEN, FakeRequest

from ErisPulse_ApiMCP.docs import DocsEngine


# ---------- 文档检索（ApiMCP 自有引擎，独立于其它模块） ----------

DOCS_A = "# 快速开始\n\n安装 ErisPulse 后启动机器人。\n\n## 发送消息\n\n用 SendDSL 发送消息。"
DOCS_B = "# 模块开发\n\n继承 BaseModule，注册 register_http_route 暴露 REST 路由。"


def _docs_fixture(tmp_path, monkeypatch, apimcp):
    """把文档源指向本地目录（零网络）：两篇 markdown。"""
    root = tmp_path / "docs"
    (root / "sdk").mkdir(parents=True)
    (root / "getting-started.md").write_text(DOCS_A, encoding="utf-8")
    (root / "sdk" / "module.md").write_text(DOCS_B, encoding="utf-8")
    apimcp.docs = DocsEngine(apimcp)
    monkeypatch.setattr(apimcp, "_conf", lambda: apimcp.ConfigClass(token=TOKEN, docs_source=str(root)))
    return root


async def test_docs_search_uses_local_engine(tmp_path, monkeypatch, loaded):
    _docs_fixture(tmp_path, monkeypatch, loaded)
    body, status = await loaded._bridge.handle_request(
        "docs/search", FakeRequest(token=TOKEN, body={"query": "发送消息", "top_k": 3})
    )
    assert status == 200
    results = body["data"]["results"]
    assert results
    assert "发送消息" in results[0]["title"] + results[0]["snippet"]
# file/root 属路径类字段，响应净化会丢弃（AI 侧只看 title/snippet 即可）
    assert "file" not in results[0]
    assert body["data"]["status"]["files"] >= 2


async def test_docs_read_and_path_escape(tmp_path, monkeypatch, loaded):
    _docs_fixture(tmp_path, monkeypatch, loaded)
    bridge = loaded._bridge
    body, status = await bridge.handle_request(
        "docs/read", FakeRequest(token=TOKEN, body={"file": "sdk/module.md"})
    )
    assert status == 200
    assert "register_http_route" in body["data"]["content"]
# 越权路径：引擎层直接拒绝（响应净化也会把含绝对路径的字段丢掉，双层防护）
    engine = bridge._docs()
    assert "escapes" in engine.read("../../etc/passwd", 800)


async def test_docs_refresh_reports_sync(tmp_path, monkeypatch, loaded):
    _docs_fixture(tmp_path, monkeypatch, loaded)
    body, status = await loaded._bridge.handle_request(
        "docs/refresh", FakeRequest(token=TOKEN, body={})
    )
    assert status == 200
    assert body["data"]["sync"]["enabled"] is True
    assert body["data"]["status"]["files"] >= 2


def test_docs_engine_is_self_contained(loaded):
    """不依赖 QvQChat：文档端点经自有 DocsEngine，不走 module.call 委托。"""
    engine = loaded._bridge._docs()
    assert isinstance(engine, DocsEngine)
    assert not hasattr(engine, "_DOCS_HOST_MODULE")


# ---------- get_module_info 服务签名 ----------


async def test_get_module_info_lists_service_signatures(bridge):
    def fake_services():
        return {"Safe": [{"name": "do_thing", "signature": "(a, b=1)", "description": "做一件事"}]}

    bridge.sdk.module.services = fake_services
    body, status = await bridge.handle_request(
        "modules/info", FakeRequest(token=TOKEN, body={"name": "Safe"})
    )
    assert status == 200
    services = body["data"]["services"]
    assert services and services[0]["name"] == "do_thing"
    assert services[0]["signature"] == "(a, b=1)"


# ---------- send_message 账户与签名预检 ----------


class _AccountingSendDSL:
    """带 Using 的假 SendDSL：记录账户；Custom 方法带额外必填参数"""

    def __init__(self, log):
        self._log = log
        self.account = None

    def Using(self, account):
        self._log.append(("Using", account))
        wrapper = _AccountingSendDSL(self._log)
        wrapper.account = account
        return wrapper

    def To(self, target_type, target_id):
        self._log.append(("To", target_type, str(target_id)))
        return self

    async def Text(self, content):
        self._log.append(("Text", content))
        return "msg_acc_001"

    async def Custom(self, content, extra):
        self._log.append(("Custom", content, extra))
        return "msg_custom_001"

    def list_sends(self):
        return ["Text", "Custom"]


async def test_send_message_with_account(bridge):
    log = []
    bridge.sdk.adapter.adapters["onebot11"].Send = _AccountingSendDSL(log)
    body, status = await bridge.handle_request(
        "messages/send",
        FakeRequest(token=TOKEN, body={
            "platform": "onebot11", "target_type": "group", "target_id": "123",
            "method": "Text", "content": "在吗", "account": "20002",
        }),
    )
    assert status == 200
    assert ("Using", "20002") in log and ("Text", "在吗") in log


async def test_send_message_rejects_extra_required_params(bridge):
    """方法除 content 外还有必填参数 → 400 提前拒绝并列出缺参（不发送）"""
    log = []
    bridge.sdk.adapter.adapters["onebot11"].Send = _AccountingSendDSL(log)
    body, status = await bridge.handle_request(
        "messages/send",
        FakeRequest(token=TOKEN, body={
            "platform": "onebot11", "target_type": "group", "target_id": "123",
            "method": "Custom", "content": "x",
        }),
    )
    assert status == 400
    assert "extra" in body["error"]
    assert all(entry[0] != "Custom" for entry in log), "签名预检应发生在发送之前"
