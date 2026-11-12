"""测试夹具：假 SDK 对象（不依赖 ErisPulse-Testing / 不触盘配置）"""

import pytest

from ErisPulse_ApiMCP.Core import Main


class FakeLogger:
    """记录式日志器（get_child 共享记录列表）"""

    def __init__(self, records=None):
        self.records = records if records is not None else []

    def get_child(self, name):
        return self

    def _record(self, level, msg):
        self.records.append((level, str(msg)))

    def info(self, msg, *args, **kwargs):
        self._record("info", msg)

    def warning(self, msg, *args, **kwargs):
        self._record("warning", msg)

    def error(self, msg, *args, **kwargs):
        self._record("error", msg)

    def debug(self, msg, *args, **kwargs):
        self._record("debug", msg)

    def trace(self, msg, *args, **kwargs):
        self._record("trace", msg)

    def critical(self, msg, *args, **kwargs):
        self._record("critical", msg)


class FakeSendDSL:
    """假 SendDSL：记录 To/Text/Image 调用；Sticker 故意返回非可等待对象"""

    def __init__(self, log):
        self._log = log

    def To(self, target_type, target_id):
        self._log.append(("To", target_type, str(target_id)))
        return self

    async def Text(self, content):
        self._log.append(("Text", content))
        return "msg_fake_001"

    async def Image(self, content):
        self._log.append(("Image", content))
        return "msg_fake_002"

    def Sticker(self, content):  # 非法样例：链式/同步方法，返回自身而非 Awaitable
        self._log.append(("Sticker", content))
        return self

    def list_sends(self):
        return ["Text", "Image", "Sticker"]


class FakeAdapterInstance:
    def __init__(self):
        self.sent = []
        self.Send = FakeSendDSL(self.sent)


class FakeAdapterManager:
    def __init__(self, adapters=None):
        self.adapters = adapters if adapters is not None else {"onebot11": FakeAdapterInstance()}
        self.handlers = {}
        self.reload_calls = []

    def list_registered(self):
        return list(self.adapters)

    def get(self, platform):
        return self.adapters.get(platform)

    def get_status_summary(self):
        return {
            "adapters": {
                platform: {"status": "started", "enabled": True, "bots": {"10001": {"status": "online"}}}
                for platform in self.adapters
            }
        }

    async def reload(self, platform, **kwargs):
        self.reload_calls.append(platform)
        return True

    def on(self, event_type="*", **kwargs):
        def decorator(func):
            self.handlers.setdefault(event_type, []).append(func)
            return func

        return decorator


class FakeModuleManager:
    def __init__(self):
        self.calls = []
        self.reload_calls = []
        self.modules = {
            "Safe": {"status": "loaded", "enabled": True},
            "Other": {"status": "loaded", "enabled": True},
        }

    def list_registered(self):
        return list(self.modules)

    def get_status_summary(self):
        return {"modules": dict(self.modules)}

    def get_info(self, name):
        if name not in self.modules:
            return None
        return {"name": name, "meta": {"version": "1.0.0"}}

    def get_meta(self, name):
        if name not in self.modules:
            return None
        return {"name": name, "description": f"fake module {name}", "version": "1.0.0"}

    async def call(self, name, method, *args, **kwargs):
        self.calls.append((name, method, args, kwargs))
        if name == "Boom":
            raise RuntimeError("boom from module")
        return {"echo": method, "args": args, "kwargs": kwargs}

    async def reload(self, name, **kwargs):
        self.reload_calls.append(name)
        return True


class FakeTranscript:
    def __init__(self, rows=None):
        self.rows = rows or []

    def get(self, session, n=20):
        matched = [r for r in self.rows if r.get("session_key") == session]
        return matched[-max(1, int(n)):]

    def recent(self, seconds, limit=200):
        return self.rows[-max(1, int(limit)):]


class FakeRouter:
    """假 RouterManager：对齐真框架的重复注册（方法冲突抛 ValueError）语义"""

    def __init__(self):
        self.routes = {}

    def register_http_route(self, module_name, path, handler, methods=None, rate_limit=None):
        methods = list(methods or ["POST"])
        key = (module_name, path)
        existing = self.routes.get(key)
        if existing is not None:
            conflict = [m for m in methods if m in existing["methods"]]
            if conflict:
                raise ValueError(f"路由重复注册: /{module_name}{path} methods={conflict}")
            existing["methods"] = existing["methods"] + methods
            existing["handler"] = handler
        else:
            self.routes[key] = {
                "handler": handler,
                "methods": methods,
                "rate_limit": rate_limit,
            }

    def unregister_http_route(self, module_name, path):
        return self.routes.pop((module_name, path), None) is not None


class FakeSdk:
    def __init__(self):
        self.version = "2.11.0-test"
        self.logger = FakeLogger()
        self.module = FakeModuleManager()
        self.adapter = FakeAdapterManager()
        self.router = FakeRouter()
        self.transcript = FakeTranscript(
            rows=[
                {
                    "role": "user",
                    "text": "你好",
                    "ts": 1000.0,
                    "sender": "u1",
                    "session_key": "onebot11:group:100",
                },
                {
                    "role": "user",
                    "text": "hello",
                    "ts": 1001.0,
                    "sender": "u2",
                    "session_key": "telegram:user:200",
                },
            ]
        )

    def get_topology(self):
        return {
            "modules": {
                "Safe": {
                    "loaded": True,
                    "info": {"meta": {"version": "1.0.0"}, "module_path": "/home/user/modules/Safe"},
                    "commands": [],
                }
            },
            "adapters": {"onebot11": {"status": "started", "token": "secret-should-drop"}},
            "scope": {},
        }


class FakeRequest:
    """最小请求对象：headers + async json()"""

    def __init__(self, token=None, body=None, method="POST"):
        self.headers = {}
        if token is not None:
            self.headers["X-ApiMCP-Token"] = token
        self.method = method
        self._body = body

    async def json(self):
        return {} if self._body is None else self._body


TOKEN = "test-token-123"


@pytest.fixture
def fake_sdk():
    return FakeSdk()


@pytest.fixture
def apimcp(fake_sdk):
    """未加载的 Main 实例（注入假 SDK 与内存配置）"""
    m = Main()
    m.sdk = fake_sdk
    m.logger = fake_sdk.logger.get_child("ApiMCP")
    m._conf = lambda: Main.ConfigClass(token=TOKEN)
    return m


@pytest.fixture
async def loaded(apimcp):
    """已完成 on_load 的 Main 实例（测试后卸载）"""
    await apimcp.on_load({"module_name": "ApiMCP"})
    yield apimcp
    await apimcp.on_unload({"module_name": "ApiMCP"})


@pytest.fixture
def bridge(loaded):
    return loaded._bridge
