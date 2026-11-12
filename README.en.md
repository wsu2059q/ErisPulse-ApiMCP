<div align="center">

[简体中文](README.md) | [繁體中文](README.zh-TW.md) | English | [日本語](README.ja.md) | [Русский](README.ru.md)

<img src="https://raw.githubusercontent.com/ErisPulse/ErisPulse/main/.github/assets/ErisPulseLogo.png" width="180" alt="ErisPulse-ApiMCP" />

</div>

# ErisPulse-ApiMCP

Expose an ErisPulse bot runtime over **MCP (Model Context Protocol)** to AI clients such as Claude Desktop: receive and send messages, call module APIs, manage modules/adapters, observe event streams, and search framework documentation.

<p>
  <a href="https://github.com/ErisPulse/ErisPulse"><img src="https://raw.githubusercontent.com/ErisPulse/.github/main/assets/badges/module.svg" alt="ErisPulse Module" /></a>
  <a href="https://pypi.org/project/ErisPulse-ApiMCP/"><img src="https://img.shields.io/pypi/v/ErisPulse-ApiMCP?style=for-the-badge&logo=pypi&logoColor=white" alt="PyPI"></a>
  <a href="https://pypi.org/project/ErisPulse-ApiMCP/"><img src="https://img.shields.io/badge/Python-3.10+-FFD43B?style=for-the-badge&logo=python&logoColor=blue" alt="Python"></a>
</p>

## Architecture

```
┌─────────────────────────┐  MCP (stdio)  ┌──────────────────────────────────┐
│  AI client              │◄─────────────►│  erispulse-apimcp (stdio server) │
│  (Claude Desktop ...)   │   17 tools    │  official mcp SDK · HTTP forward │
└─────────────────────────┘               └──────────────┬───────────────────┘
                                                         │ HTTP POST /apimcp/*
                                                         │ header: X-ApiMCP-Token
                                                         ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  ErisPulse bot process                                                     │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ ApiMCP module (REST bridge on the framework HTTP server, :8000)      │  │
│  │  · token auth (explicit config required; 503 otherwise)              │  │
│  │  · event handler → subscription filter → ring buffer (1000 default)  │  │
│  └───────────────┬──────────────────────────────────────────────────────┘  │
│                  │ sdk.* calls                                              │
│        ┌─────────┴─────────┬──────────────┬─────────────┬────────────┐     │
│        ▼                   ▼              ▼             ▼            ▼     │
│   module.call/list/    adapter.list/   SendDSL send  transcript    topology│
│   reload/get_info      reload/status                 recent msgs   /health │
└────────────────────────────────────────────────────────────────────────────┘
```

- In the bot process, the ApiMCP module exposes a REST bridge (`/apimcp/*`, token auth) on the framework HTTP server.
- As a separate process, `erispulse-apimcp` runs a stdio MCP server (official `mcp` SDK) and forwards tools to the bot over keep-alive HTTP (10 s timeout).
- When the bot is offline or the bridge is unconfigured, tools return readable errors instead of stack traces.

## Install

```bash
# Option 1: ErisPulse CLI (recommended)
epsdk install ErisPulse-ApiMCP

# Option 2: pip
pip install ErisPulse-ApiMCP
```

Restart the bot after installation. The module loads at startup and mounts the bridge on the framework HTTP server (default `0.0.0.0:8000`).

## Configure the token

**The token is never generated automatically.** Without `[ApiMCP] token` (or the environment variable), the bridge stays unconfigured: `health` still returns 200 so you can distinguish "bridge unconfigured" from "bot offline", while every other endpoint returns **503** with instructions.

- Config file: `[ApiMCP] token = "your-long-random-string"` in `config.toml`
- Environment: `ERISPLUSE_APIMCP_TOKEN=...` (takes precedence)

```bash
# configured: returns a health summary
curl -H "X-ApiMCP-Token: <token>" http://127.0.0.1:8000/apimcp/health
# unconfigured: 200 with data.status = "unconfigured"
curl http://127.0.0.1:8000/apimcp/health
```

## Claude Desktop

Edit `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`; Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "erispulse": {
      "command": "erispulse-apimcp",
      "env": {
        "ERISPLUSE_APIMCP_URL": "http://127.0.0.1:8000",
        "ERISPLUSE_APIMCP_TOKEN": "your-token"
      }
    }
  }
}
```

If `erispulse-apimcp` is not on PATH, use the interpreter path or `python -m ErisPulse_ApiMCP.server`. `--url` / `--token` are also accepted and take precedence, but `--token` is visible in the process list; prefer the environment variable. `--url` must include `http://` or `https://`.

## MCP Tools (17)

| Tool | Description |
| --- | --- |
| `list_modules` | List registered modules and states (loaded/registered/disabled, enabled, failed, stale) |
| `get_module_info(name)` | Registration info and metadata (version/description/services/last error) |
| `call_module(name, method, args?, kwargs?, rpc_timeout?)` | Cross-module RPC; private methods rejected; subject to `module_call_whitelist`; `rpc_timeout` applies to coroutine methods only |
| `reload_module(name)` | Hot reload a module (`allow_module_reload`, enabled by default) |
| `list_adapters` | List adapters, states and bot presence |
| `get_adapter_status(platform)` | Runtime status and bots for one platform |
| `reload_adapter(platform)` | Reload an adapter (`allow_adapter_reload`, disabled by default) |
| `send_message(platform, target_type, target_id, method, content, account?)` | Send text/image/voice/video/file; `target_type` is user/group/channel/guild/thread (`private` maps to `user`); content ≤ 32 KB; `account` selects the bot account in multi-account deployments |
| `recent_messages(platform?, session?, limit=50)` | Recent messages via transcript; session key `platform:type:id` |
| `subscribe_events(types)` | Subscribe to message/notice/request/meta or `*`; max 32 active subscriptions |
| `unsubscribe(subscription_id)` | Remove a subscription |
| `poll_events(since_ts, since_seq, limit?, subscription_id?)` | Incremental polling; pass `next_since_ts`/`next_since_seq` from the previous response; `dropped` reports evictions |
| `get_topology` | Runtime topology (sanitized) |
| `sdk_health` | SDK version, uptime, module/adapter counts, buffer stats |
| `search_erispulse_docs(query, top_k=5)` | Search framework docs (independent docs engine with versioned cache; no other module required) |
| `read_erispulse_doc(file, limit=8000)` | Read one documentation file |
| `refresh_erispulse_docs()` | Force-sync the docs cache to the current framework version |

## REST endpoints (`/apimcp/*`)

`POST` with `X-ApiMCP-Token`; `health` also accepts `GET`.

| Endpoint | Tool | Body |
| --- | --- | --- |
| `/apimcp/modules/list` | list_modules | `{}` |
| `/apimcp/modules/info` | get_module_info | `{"name"}` |
| `/apimcp/modules/call` | call_module | `{"name","method","args":[],"kwargs":{}}` |
| `/apimcp/modules/reload` | reload_module | `{"name"}` |
| `/apimcp/adapters/list` | list_adapters | `{}` |
| `/apimcp/adapters/status` | get_adapter_status | `{"platform"}` |
| `/apimcp/adapters/reload` | reload_adapter | `{"platform"}` |
| `/apimcp/messages/send` | send_message | `{"platform","target_type","target_id","method","content"}` |
| `/apimcp/messages/recent` | recent_messages | `{"platform"?,"session"?,"limit"?}` |
| `/apimcp/events/subscribe` | subscribe_events | `{"types":["message"]}` |
| `/apimcp/events/unsubscribe` | unsubscribe | `{"subscription_id"}` |
| `/apimcp/events/poll` | poll_events | `{"since_ts":0,"since_seq":0,"limit"?}` |
| `/apimcp/docs/search` | search_erispulse_docs | `{"query","top_k"?}` |
| `/apimcp/docs/read` | read_erispulse_doc | `{"file","limit"?}` |
| `/apimcp/docs/refresh` | refresh_erispulse_docs | `{}` |
| `/apimcp/topology` | get_topology | `{}` |
| `/apimcp/health` | sdk_health | `{}` |

Responses: `{"ok": true, "data": {...}}` or `{"ok": false, "error": "..."}` with 400/401/403/404/413/500/503.

- Rate limit: 30 requests/minute per client IP (429 + `Retry-After`).
- Body ≤ 256 KB; `messages/send` content ≤ 32 KB; active subscriptions ≤ 32.
- Numeric parameters accept numbers or numeric strings.

## Configuration (`[ApiMCP]`)

| Key | Default | Description |
| --- | --- | --- |
| `token` | `""` | Bridge token; required (503 otherwise); env `ERISPLUSE_APIMCP_TOKEN` overrides |
| `allow_module_reload` | `true` | Allow AI-triggered module reload |
| `allow_adapter_reload` | `false` | Allow adapter reload |
| `module_call_whitelist` | `[]` | `["Mod"]` or `["Mod.method"]`; empty = unrestricted |
| `send_target_whitelist` | `[]` | `"type:id"` or `"type:*"`; empty = unrestricted |
| `event_types_allowed` | `[]` | Allowed event types; empty = unrestricted |
| `event_buffer_size` | `1000` | Ring buffer size (10–100000) |
| `max_poll_events` | `200` | Max events per poll (≤500) |
| `recent_window_seconds` | `3600` | Recent-message lookback window |

All keys are hot-reloadable.

## Security notes

- Bind the framework HTTP server to `127.0.0.1` when only local clients are used.
- A token grants full bridge access: read/send messages, call whitelisted methods, read all subscription events. Use one instance per trust domain.
- Prefer environment variables over `--token`, which is visible in the process list.
- Whitelists: narrow `module_call_whitelist` and `send_target_whitelist`; keep `allow_adapter_reload = false`.
- Output sanitization removes secret-like keys, path-like keys, and string values that look like paths or contain `..`.

## Development

```bash
uv sync
uv run pytest
uv run erispulse-apimcp --help
```

## Known limitations

- Event stream is poll-based; the ring buffer drops the oldest events when full (`dropped`).
- `poll_events` cursors reset after a bot restart.
- `rpc_timeout` only applies to coroutine methods.
- Buffered `message` events are summaries (500-char text limit, large fields replaced, 50-segment limit); use `recent_messages` for full messages.
- mcp SDK 1.x (FastMCP) and 2.x (MCPServer) are supported.

## Community

- Issues: <https://github.com/wsu2059q/ErisPulse-ApiMCP/issues>
- Telegram: <https://t.me/ErisPulse>
- QQ group: <https://qm.qq.com/q/TOwnCmypcy>

## License

[MIT](LICENSE)
