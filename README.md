<div align="center">

简体中文 | [繁體中文](README.zh-TW.md) | [English](README.en.md) | [日本語](README.ja.md) | [Русский](README.ru.md)

<img src="https://raw.githubusercontent.com/ErisPulse/ErisPulse/main/.github/assets/ErisPulseLogo.png" width="180" alt="ErisPulse-ApiMCP" />

</div>

# ErisPulse-ApiMCP

把 ErisPulse 机器人运行时能力经 **MCP（Model Context Protocol）** 开放给 AI 客户端（Claude Desktop 等）：收消息、发消息、调用模块 API、管理模块/适配器、观测事件流、检索框架文档。

<p>
  <a href="https://github.com/ErisPulse/ErisPulse"><img src="https://raw.githubusercontent.com/ErisPulse/.github/main/assets/badges/module.svg" alt="ErisPulse Module" /></a>
  <a href="https://pypi.org/project/ErisPulse-ApiMCP/"><img src="https://img.shields.io/pypi/v/ErisPulse-ApiMCP?style=for-the-badge&logo=pypi&logoColor=white" alt="PyPI"></a>
  <a href="https://pypi.org/project/ErisPulse-ApiMCP/"><img src="https://img.shields.io/badge/Python-3.10+-FFD43B?style=for-the-badge&logo=python&logoColor=blue" alt="Python"></a>
</p>

## 架构

```
┌─────────────────────────┐  MCP (stdio)  ┌──────────────────────────────────┐
│  AI 客户端               │◄─────────────►│  erispulse-apimcp（stdio server） │
│  (Claude Desktop ...)   │   17 个 tools  │  官方 mcp SDK · HTTP 转发         │
└─────────────────────────┘               └──────────────┬───────────────────┘
                                                         │ HTTP POST /apimcp/*
                                                         │ 头: X-ApiMCP-Token
                                                         ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  ErisPulse 机器人进程                                                       │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ ApiMCP 模块（REST 桥，挂框架内置 HTTP server，默认 :8000）             │  │
│  │  · token 鉴权（必须显式配置；未配置则除 health 探活外全部 503）        │  │
│  │  · 事件通配处理器 → 订阅过滤 → 环形缓冲（默认 1000 条，超限淘汰最旧）   │  │
│  └───────────────┬──────────────────────────────────────────────────────┘  │
│                  │ sdk.* 调用                                              │
│        ┌─────────┴─────────┬──────────────┬─────────────┬────────────┐     │
│        ▼                   ▼              ▼             ▼            ▼     │
│   module.call/list/    adapter.list/   SendDSL 发送  transcript    拓扑/   │
│   reload/get_info      reload/status                 最近消息      健康    │
└────────────────────────────────────────────────────────────────────────────┘
```

- 机器人进程内：ApiMCP 模块经框架内置 HTTP server 暴露 REST 桥（`/apimcp/*`，token 鉴权）
- 独立进程：`erispulse-apimcp` 命令以官方 `mcp` SDK 起 stdio server，把 MCP tools 转发到机器人 HTTP（keep-alive 持久连接，超时 10s）
- 机器人不在线 / 桥未配置时，tools 返回可读错误与排查指引（不抛栈）

## 安装

```bash
# 方式一：ErisPulse CLI（推荐，自动装进机器人环境）
epsdk install ErisPulse-ApiMCP

# 方式二：pip
pip install ErisPulse-ApiMCP
```

安装后重启机器人，ApiMCP 模块自动加载（启动即加载，非懒加载），REST 桥挂到框架 HTTP server（默认 `0.0.0.0:8000`）。

## 配置 token（必须显式配置）

**token 不自动生成**：`[ApiMCP] token` 未配置（且无环境变量）时，桥进入「未配置」状态——除 `health` 探活端点（仍返回 200，用于区分「桥未配置」与「机器人不在线」）外，所有端点返回 **503** 与可读指引。配置方式任选其一：

- 配置文件：`config.toml` 中 `[ApiMCP]` 节设置 `token = "你的长随机串"`
- 环境变量：`ERISPULSE_APIMCP_TOKEN=你的长随机串`（优先级高于配置文件）

配置后热更新或重启机器人即生效。快速自检（curl 探活）：

```bash
# 已配置：返回健康摘要
curl -H "X-ApiMCP-Token: <token>" http://127.0.0.1:8000/apimcp/health
# 未配置：仍返回 200，data.status = "unconfigured"（据此区分桥未配置与机器人离线）
curl http://127.0.0.1:8000/apimcp/health
```

## Claude Desktop 接入

编辑 `claude_desktop_config.json`（macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`；Windows: `%APPDATA%\Claude\claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "erispulse": {
      "command": "erispulse-apimcp",
      "env": {
        "ERISPULSE_APIMCP_URL": "http://127.0.0.1:8000",
        "ERISPULSE_APIMCP_TOKEN": "你的token"
      }
    }
  }
}
```

- 若 `erispulse-apimcp` 不在 PATH，把 `command` 换为解释器内的绝对路径（`python -c "import ErisPulse_ApiMCP.server as s; print(s.__file__)"` 定位安装位置，或直接用 `command: python` + `args: ["-m", "ErisPulse_ApiMCP.server"]`）
- 地址/Token 也可用命令行参数 `--url` / `--token` 传入（优先于环境变量），但 `--token` 会暴露在进程列表中——推荐只用环境变量；`--url` 必须带 `http://` 或 `https://`
- 保存后完全退出并重启 Claude Desktop，工具列表出现 `erispulse` 服务器即接入成功

## MCP Tools（17 个）

| Tool | 说明 |
| --- | --- |
| `list_modules` | 列出已注册模块及状态（loaded/registered/disabled、enabled、failed、stale） |
| `get_module_info(name)` | 模块注册信息 + 元信息（版本/描述/服务契约/最近错误） |
| `call_module(name, method, args?, kwargs?, rpc_timeout?)` | 跨模块 RPC；私有方法拒绝；受 `module_call_whitelist` 白名单约束；RPC 超时用 `rpc_timeout`（秒，仅协程方法生效；kwargs 里的 `timeout` 无法透传会被剔除并在响应 `note` 提示）；被调方异常只回类型名，详情进机器人日志 |
| `reload_module(name)` | 模块热重载（`allow_module_reload`，默认允许） |
| `list_adapters` | 列出平台适配器及状态、Bot 在线概况 |
| `get_adapter_status(platform)` | 单平台运行状态与 Bot 详情 |
| `reload_adapter(platform)` | 适配器重载（`allow_adapter_reload`，**默认禁用**） |
| `send_message(platform, target_type, target_id, method, content, account?)` | 发消息：`target_type` 限 `user`/`group`/`channel`/`guild`/`thread`（`private` 自动转 `user`）；`Text` 文本 / `Image` `Voice` `Video` `File` 传 URL 或 base64（content ≤ 32KB）；受 `send_target_whitelist` 白名单约束；`account` 指定多 bot 部署下的发送账户；方法需要额外参数时提前拒绝并列出缺参 |
| `recent_messages(platform?, session?, limit=50)` | 最近消息回看（经 transcript；会话键 `平台:类型:ID`，类型限 `private`/`group`/`user`/`channel`/`guild`/`thread`，格式非法 400；limit 接受字符串数字） |
| `subscribe_events(types)` | 订阅事件（`message`/`notice`/`request`/`meta` 或 `*`），返回 `subscription_id` 与全部活跃订阅；活跃订阅总数上限 32 |
| `unsubscribe(subscription_id)` | 取消订阅，返回剩余全部活跃订阅 |
| `poll_events(since_ts, since_seq, limit?, subscription_id?)` | 增量轮询缓冲事件；首次传 0，之后传上次返回的 `next_since_ts`/`next_since_seq`；可选 `subscription_id` 只返回该订阅声明类型的事件；响应含 `dropped`（累计淘汰数），静默期仍在淘汰时附 `warning` |
| `get_topology` | 运行时拓扑（模块/适配器/作用域归属），已脱敏 |
| `sdk_health` | SDK 版本 / 运行时长 / 模块数 / 适配器状态 / 缓冲统计摘要 |
| `search_erispulse_docs(query, top_k=5)` | 检索 ErisPulse 框架文档（独立文档引擎：按框架版本下载并缓存，不依赖其它模块） |
| `read_erispulse_doc(file, limit=8000)` | 读取文档单文件（路径来自检索结果的 `file` 字段） |
| `refresh_erispulse_docs()` | 强制同步文档缓存到当前框架版本 |

> 事件观测模式：先 `subscribe_events(["message"])`，之后周期性 `poll_events`。事件在机器人侧环形缓冲（默认 1000 条，超限淘汰最旧，响应 `dropped` 字段可观测累计淘汰量）。入缓冲的消息段为摘要形态：文本截断 500 字符，file/base64/data/url 大块字段替换为 `<omitted:len=N>`，超 50 段截断。

## REST 桥端点（`/apimcp/*`）

所有端点默认 `POST`（`health` 额外支持 `GET`），请求头携带 `X-ApiMCP-Token`，请求体 JSON：

| 端点 | 对应 tool | 请求体要点 |
| --- | --- | --- |
| `/apimcp/modules/list` | list_modules | `{}` |
| `/apimcp/modules/info` | get_module_info | `{"name": "..."}` |
| `/apimcp/modules/call` | call_module | `{"name","method","args":[],"kwargs":{}}` |
| `/apimcp/modules/reload` | reload_module | `{"name": "..."}` |
| `/apimcp/adapters/list` | list_adapters | `{}` |
| `/apimcp/adapters/status` | get_adapter_status | `{"platform": "..."}` |
| `/apimcp/adapters/reload` | reload_adapter | `{"platform": "..."}` |
| `/apimcp/messages/send` | send_message | `{"platform","target_type","target_id","method","content"}` |
| `/apimcp/messages/recent` | recent_messages | `{"platform"?,"session"?,"limit"?}` |
| `/apimcp/events/subscribe` | subscribe_events | `{"types": ["message"]}` |
| `/apimcp/events/unsubscribe` | unsubscribe | `{"subscription_id": "..."}` |
| `/apimcp/events/poll` | poll_events | `{"since_ts":0,"since_seq":0,"limit"?}` |
| `/apimcp/docs/search` | search_erispulse_docs | `{"query", "top_k"?}` |
| `/apimcp/docs/read` | read_erispulse_doc | `{"file", "limit"?}` |
| `/apimcp/docs/refresh` | refresh_erispulse_docs | `{}` |
| `/apimcp/topology` | get_topology | `{}` |
| `/apimcp/health` | sdk_health | `{}` |

统一响应：`{"ok": true, "data": {...}}`；失败 `{"ok": false, "error": "可读错误"}` + 对应状态码（400 参数 / 401 鉴权 / 403 开关或白名单拒绝 / 404 未注册 / 413 请求体或 content 超限 / 500 内部（仅 `internal: <异常类型>`，详情只进机器人日志）/ 503 桥未配置或 handler 不完整）。

> - token 未配置时除 `health`（200，`data.status = "unconfigured"`）外全部 503
> - 全部端点默认限流 **30 请求/分钟/客户端 IP**（SDK RouterManager 内置限流，超限 429 + `Retry-After`），AI 客户端异常循环重试会被截断
> - 请求体 ≤ 256KB、`messages/send` 的 content ≤ 32KB（超限 413；大文件请用平台 URL 引用）；活跃事件订阅总数 ≤ 32（超出 403）
> - 数值参数（`limit` / `since_ts` / `since_seq` / `rpc_timeout`）接受数字或字符串数字（如 `"50"`），非法值 400「须为数字」

## 配置项（`config.toml` `[ApiMCP]` 节）

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `token` | `""` | REST 桥访问令牌；**必须显式配置**，留空则桥不启用（health 探活仍 200，其余 503）；支持环境变量 `ERISPULSE_APIMCP_TOKEN` 覆盖 |
| `allow_module_reload` | `true` | 允许 AI 触发模块热重载 |
| `allow_adapter_reload` | `false` | 允许 AI 触发适配器重载（断连风险高，默认关闭） |
| `module_call_whitelist` | `[]` | MCP 可调用模块白名单；`["Mod"]` 放行整模块公开方法，`["Mod.method"]` 精确放行；空 = 不限制（仍受目标模块自身 services 契约与私有方法禁令约束） |
| `send_target_whitelist` | `[]` | 允许发送消息的目标白名单；条目 `"类型:ID"`（如 `"user:123"`）精确放行，`"类型:*"`（如 `"group:*"`）放行该类型全部 ID；空 = 不限制（任何目标均可发送，**有越权发送风险**，不信任的 AI 环境务必配置） |
| `event_types_allowed` | `[]` | 允许订阅的事件类型；空 = 不限制 |
| `event_buffer_size` | `1000` | 事件环形缓冲容量（10–100000） |
| `max_poll_events` | `200` | 单次轮询最多返回事件数（≤500） |
| `recent_window_seconds` | `3600` | 最近消息回溯窗口（秒） |

所有配置支持热更新：token 设置/切换/清空、缓冲容量调整即时生效。

## 安全注意事项

- **只监听本机（强烈建议置顶执行）**：框架 HTTP server 默认 `0.0.0.0:8000`，公网/局域网可达面即 REST 桥可达面。仅本机使用时在框架配置设置 `server.host = "127.0.0.1"`；跨机器必须走内网 + 反向代理加 TLS
- **token 必须显式配置**：未配置时桥不启用（503），不生成临时 token；token 热更新（设置/切换/清空）即时生效
- **token 即完全控制权，且单 token = 单信任域**：持有 token 的客户端可读消息、发消息、调用任意（白名单内）模块方法、查看全部订阅事件（事件缓冲与 `dropped` 淘汰基线为全桥共享，不按订阅隔离）。多租户/多方接入请拆多个机器人实例。切勿提交到仓库、截图或粘贴到公开频道
- **stdio 命令行的 token 优先走环境变量**：`--token` 参数会暴露在进程列表（`ps`/任务管理器）中；Claude Desktop 配置的 `env` 字段即环境变量方式
- `--url` 必须带 `http://` 或 `https://` scheme（无 scheme 的 `localhost:8000` 会被拒绝并明确报错，避免静默错连 127.0.0.1:80）
- **最小授权**：不信任的 AI 环境请配置 `module_call_whitelist` 收窄可调用面、配置 `send_target_whitelist` 收窄发送目标、保持 `allow_adapter_reload = false`；模块启动时对「白名单为空」「允许模块重载」的宽默认姿态会输出 warning 提示
- 输出净化声明：拓扑 / `modules/call` 结果为**键名级**（密钥类 / 路径类键名整键丢弃）+ **字符串叶子路径形态**（盘符 / 前导 `/` / UNC / `file://`）+ 含 `..` 的值丢弃——注意业务文本字段若以 `/` 开头（如斜杠命令消息）也会被该规则丢弃；事件入缓冲前消息段已净化为摘要形态。深度净化是目标模块自身的责任，敏感会话仍勿开放订阅

## 开发

```bash
cd ErisPulse-ApiMCP
uv sync          # 安装（含测试依赖）
uv run pytest    # 运行测试（假 SDK 对象，无需真实机器人）
uv run erispulse-apimcp --help
```

## 已知限制

- 事件流为**轮询模型**（订阅 → 环形缓冲 → AI 轮询），非实时推送；缓冲超限淘汰最旧事件（响应 `dropped` 字段为累计淘汰量；无新事件但淘汰仍在增加时响应附 `warning` 提示），高频场景请缩短轮询间隔或调大 `event_buffer_size`；缓冲与淘汰计数为全桥共享，`subscription_id` 过滤只作用于返回视图
- `poll_events` 游标基于 `(ts, seq)`：机器人重启后缓冲清空，游标从 0 重新开始即可
- `call_module` 的 `rpc_timeout` 透传 SDK `module.call` 原生超时，**仅对协程方法生效**（同步方法无超时约束）
- 事件缓冲中的 `message` 为摘要形态（文本截断 500 字符、大块字段 `<omitted:len=N>` 占位、超 50 段截断），不适合作为完整消息回放源；完整消息请用 `recent_messages`
- `sdk_health` 的运行时长以 ApiMCP 模块本次加载时间为基准（模块随框架启动加载，与进程启动时长的误差通常在秒级）
- `recent_messages` 跨会话查询依赖 transcript（框架消息记录），历史深度受其保留策略限制
- `recent_messages` 的 `session` 会话键中段类型限 SDK 会话类型集（private/group/user/channel/guild/thread）；适配器经 `register_custom_type` 注册的自定义会话类型默认被 400 拒绝，如需支持须扩展桥层允许集
- 发送媒体类消息（`Image` 等）的 `content` 为 URL 或 base64 字符串（≤ 32KB），文件句柄/本地路径语义取决于各平台适配器实现
- mcp SDK 兼容 1.x（FastMCP）与 2.x（MCPServer）；服务器为同步 HTTP 转发（keep-alive 持久连接，多线程 tool 调用经锁串行化复用），单请求超时默认 10 秒（`ERISPULSE_APIMCP_TIMEOUT` 可调），仅幂等 GET 类请求在连接失败时重试 1 次

## 交流

问题与建议请提 [Issue](https://github.com/wsu2059q/ErisPulse-ApiMCP/issues)。

- Telegram：<https://t.me/ErisPulse>
- QQ 群：<https://qm.qq.com/q/TOwnCmypcy>

## 许可证

[MIT](LICENSE)
