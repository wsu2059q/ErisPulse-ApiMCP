<div align="center">

[简体中文](README.md) | 繁體中文 | [English](README.en.md) | [日本語](README.ja.md) | [Русский](README.ru.md)

<img src="https://raw.githubusercontent.com/ErisPulse/ErisPulse/main/.github/assets/ErisPulseLogo.png" width="180" alt="ErisPulse-ApiMCP" />

</div>

# ErisPulse-ApiMCP

把 ErisPulse 機器人執行時能力經 **MCP（Model Context Protocol）** 開放給 AI 用戶端（Claude Desktop 等）：收發訊息、呼叫模組 API、管理模組/適配器、觀測事件流、檢索框架文件。

## 架構

- 機器人程序內：ApiMCP 模組在框架 HTTP server 上掛載 REST 橋（`/apimcp/*`，token 鑑權）。
- 獨立程序：`erispulse-apimcp` 以官方 `mcp` SDK 啟動 stdio server，將 tools 經 keep-alive HTTP（10 秒逾時）轉發到機器人。
- 機器人離線或橋未設定時，tools 回傳可讀錯誤，不拋出堆疊。

## 安裝

```bash
epsdk install ErisPulse-ApiMCP
# 或
pip install ErisPulse-ApiMCP
```

安裝後重新啟動機器人，模組於啟動時載入，REST 橋掛在框架 HTTP server（預設 `0.0.0.0:8000`）。

## 設定 token

**token 不會自動產生。** 未設定 `[ApiMCP] token`（且無環境變數）時，橋處於未設定狀態：`health` 仍回 200，其餘端點回 **503**。

- 設定檔：`config.toml` 的 `[ApiMCP] token = "你的長隨機字串"`
- 環境變數：`ERISPLUSE_APIMCP_TOKEN`（優先）

```bash
curl -H "X-ApiMCP-Token: <token>" http://127.0.0.1:8000/apimcp/health
curl http://127.0.0.1:8000/apimcp/health   # 未設定：200，data.status = "unconfigured"
```

## Claude Desktop

```json
{
  "mcpServers": {
    "erispulse": {
      "command": "erispulse-apimcp",
      "env": {
        "ERISPLUSE_APIMCP_URL": "http://127.0.0.1:8000",
        "ERISPLUSE_APIMCP_TOKEN": "你的token"
      }
    }
  }
}
```

`--url` / `--token` 也可用且優先，但 `--token` 會出現在程序列表；建議只用環境變數。`--url` 必須帶 `http://` 或 `https://`。

## MCP Tools（17 個）

| 分類 | Tools |
| --- | --- |
| 模組 | `list_modules` · `get_module_info` · `call_module` · `reload_module` |
| 適配器 | `list_adapters` · `get_adapter_status` · `reload_adapter` |
| 訊息 | `send_message`（`account` 指定多 bot 發送帳戶）· `recent_messages` |
| 事件 | `subscribe_events` · `unsubscribe` · `poll_events` |
| 文件 | `search_erispulse_docs` · `read_erispulse_doc` · `refresh_erispulse_docs`（獨立文件引擎，按框架版本快取，不依賴其它模組） |
| 觀測 | `get_topology` · `sdk_health` |

事件採輪詢模型：先 `subscribe_events`，再週期性 `poll_events`；緩衝區滿時淘汰最舊事件（`dropped` 為累計淘汰數）。

## REST 端點（`/apimcp/*`）

所有端點為 `POST`（`health` 另支援 `GET`），請求標頭 `X-ApiMCP-Token`，請求體 JSON。路徑與 tools 一一對應：

`modules/list` · `modules/info` · `modules/call` · `modules/reload` · `adapters/list` · `adapters/status` · `adapters/reload` · `messages/send` · `messages/recent` · `events/subscribe` · `events/unsubscribe` · `events/poll` · `docs/search` · `docs/read` · `docs/refresh` · `topology` · `health`

統一回應：`{"ok": true, "data": {...}}`；失敗 `{"ok": false, "error": "..."}` 搭配 400/401/403/404/413/500/503。

- 限流：每用戶端 IP 每分鐘 30 次（429 + `Retry-After`）
- 請求體 ≤ 256 KB；`messages/send` 的 content ≤ 32 KB；活躍訂閱 ≤ 32
- 數值參數接受數字或數字字串

## 設定項（`[ApiMCP]`）

| 設定項 | 預設 | 說明 |
| --- | --- | --- |
| `token` | `""` | 橋存取權杖；必填（未設定時 503）；環境變數可覆蓋 |
| `allow_module_reload` | `true` | 允許 AI 觸發模組熱重載 |
| `allow_adapter_reload` | `false` | 允許適配器重載 |
| `module_call_whitelist` | `[]` | `["Mod"]` 或 `["Mod.method"]`；空 = 不限制 |
| `send_target_whitelist` | `[]` | `"類型:ID"` 或 `"類型:*"`；空 = 不限制 |
| `event_types_allowed` | `[]` | 允許訂閱的事件類型；空 = 不限制 |
| `event_buffer_size` | `1000` | 事件環形緩衝容量（10–100000） |
| `max_poll_events` | `200` | 單次輪詢上限（≤500） |
| `recent_window_seconds` | `3600` | 最近訊息回溯視窗（秒） |

全部設定支援熱更新。

## 安全注意事項

- 僅本機使用時，框架 HTTP server 綁定 `127.0.0.1`。
- token 等同完整控制權；單一 token 即單一信任域，多租戶請拆多個實例。
- 優先用環境變數傳 token，`--token` 會出現在程序列表。
- 收窄 `module_call_whitelist` 與 `send_target_whitelist`，保持 `allow_adapter_reload = false`。
- 輸出會丟棄密鑰類鍵名、路徑類鍵名，以及像路徑或含 `..` 的字串值。

## 開發

```bash
uv sync
uv run pytest
uv run erispulse-apimcp --help
```

## 已知限制

- 事件流為輪詢模型，非即時推送；緩衝滿時淘汰最舊事件。
- 機器人重啟後 `poll_events` 游標歸零。
- `rpc_timeout` 僅對協程方法生效。
- 緩衝中的訊息為摘要形態；完整訊息請用 `recent_messages`。
- 支援 mcp SDK 1.x 與 2.x。

## 交流

- Issue：<https://github.com/wsu2059q/ErisPulse-ApiMCP/issues>
- Telegram：<https://t.me/ErisPulse>
- QQ 群：<https://qm.qq.com/q/TOwnCmypcy>

## 許可證

[MIT](LICENSE)
