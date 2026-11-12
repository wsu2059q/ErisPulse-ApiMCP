<div align="center">

[简体中文](README.md) | [繁體中文](README.zh-TW.md) | [English](README.en.md) | 日本語 | [Русский](README.ru.md)

<img src="https://raw.githubusercontent.com/ErisPulse/ErisPulse/main/.github/assets/ErisPulseLogo.png" width="180" alt="ErisPulse-ApiMCP" />

</div>

# ErisPulse-ApiMCP

ErisPulse ボットの実行時機能を **MCP（Model Context Protocol）** 経由で AI クライアント（Claude Desktop など）に公開します：メッセージの送受信、モジュール API の呼び出し、モジュール/アダプターの管理、イベントストリームの購読、フレームワーク文書の検索。

## アーキテクチャ

- ボットプロセス内：ApiMCP モジュールがフレームワーク HTTP サーバー上に REST ブリッジ（`/apimcp/*`、token 認証）を公開。
- 別プロセス：`erispulse-apimcp` が公式 `mcp` SDK で stdio サーバーを起動し、keep-alive HTTP（10 秒タイムアウト）でボットへ転送。
- ボットがオフライン、またはブリッジ未設定の場合は、スタックではなく読みやすいエラーを返します。

## インストール

```bash
epsdk install ErisPulse-ApiMCP
# または
pip install ErisPulse-ApiMCP
```

インストール後にボットを再起動します。モジュールは起動時に読み込まれ、REST ブリッジはフレームワーク HTTP サーバー（既定 `0.0.0.0:8000`）に接続されます。

## token の設定

**token は自動生成されません。** `[ApiMCP] token`（または環境変数）が無い場合、ブリッジは未設定状態となり、`health` は 200、それ以外のエンドポイントは **503** を返します。

- 設定ファイル：`config.toml` の `[ApiMCP] token = "長いランダム文字列"`
- 環境変数：`ERISPLUSE_APIMCP_TOKEN`（優先）

```bash
curl -H "X-ApiMCP-Token: <token>" http://127.0.0.1:8000/apimcp/health
curl http://127.0.0.1:8000/apimcp/health   # 未設定：200、data.status = "unconfigured"
```

## Claude Desktop

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

`--url` / `--token` も使用できますが、`--token` はプロセス一覧に表示されるため環境変数を推奨します。`--url` には `http://` または `https://` が必要です。

## MCP Tools（17 個）

| 分類 | Tools |
| --- | --- |
| モジュール | `list_modules` · `get_module_info` · `call_module` · `reload_module` |
| アダプター | `list_adapters` · `get_adapter_status` · `reload_adapter` |
| メッセージ | `send_message`（`account` でマルチ bot の送信アカウントを指定）· `recent_messages` |
| イベント | `subscribe_events` · `unsubscribe` · `poll_events` |
| ドキュメント | `search_erispulse_docs` · `read_erispulse_doc` · `refresh_erispulse_docs`（独立した文書エンジン。フレームワーク版ごとにキャッシュし、他モジュールに依存しません） |
| 観測 | `get_topology` · `sdk_health` |

イベントはポーリング方式です。`subscribe_events` の後、定期的に `poll_events` を呼びます。バッファが満杯になると最も古いイベントから破棄され、`dropped` に累計が入ります。

## REST エンドポイント（`/apimcp/*`）

すべて `POST`（`health` は `GET` も可）。ヘッダー `X-ApiMCP-Token`、本文は JSON。パスは tools と一対一です：

`modules/list` · `modules/info` · `modules/call` · `modules/reload` · `adapters/list` · `adapters/status` · `adapters/reload` · `messages/send` · `messages/recent` · `events/subscribe` · `events/unsubscribe` · `events/poll` · `docs/search` · `docs/read` · `docs/refresh` · `topology` · `health`

応答：`{"ok": true, "data": {...}}`、失敗時 `{"ok": false, "error": "..."}` と 400/401/403/404/413/500/503。

- レート制限：クライアント IP あたり毎分 30 回（429 + `Retry-After`）
- 本文 ≤ 256 KB、`messages/send` の content ≤ 32 KB、有効な購読 ≤ 32
- 数値パラメータは数値または数値文字列を受け付けます

## 設定（`[ApiMCP]`）

| キー | 既定値 | 説明 |
| --- | --- | --- |
| `token` | `""` | ブリッジのトークン。必須（未設定時 503）。環境変数で上書き可 |
| `allow_module_reload` | `true` | AI によるモジュール再読み込みを許可 |
| `allow_adapter_reload` | `false` | アダプター再読み込みを許可 |
| `module_call_whitelist` | `[]` | `["Mod"]` または `["Mod.method"]`。空 = 制限なし |
| `send_target_whitelist` | `[]` | `"type:id"` または `"type:*"`。空 = 制限なし |
| `event_types_allowed` | `[]` | 購読可能なイベント種別。空 = 制限なし |
| `event_buffer_size` | `1000` | リングバッファ容量（10–100000） |
| `max_poll_events` | `200` | 1 回のポーリング上限（≤500） |
| `recent_window_seconds` | `3600` | 最近メッセージの遡り期間（秒） |

すべての設定はホットリロードに対応します。

## セキュリティ

- ローカル利用のみの場合は、フレームワーク HTTP サーバーを `127.0.0.1` にバインドしてください。
- token はブリッジの完全な制御権を持ちます。1 token = 1 信頼ドメインです。マルチテナントではインスタンスを分けてください。
- token は環境変数で渡してください。`--token` はプロセス一覧に表示されます。
- `module_call_whitelist` と `send_target_whitelist` を絞り、`allow_adapter_reload = false` を維持してください。
- 出力では秘密鍵風のキー名、パス風のキー名、パス形式または `..` を含む文字列値を破棄します。

## 開発

```bash
uv sync
uv run pytest
uv run erispulse-apimcp --help
```

## 既知の制限

- イベントストリームはポーリング方式で、リアルタイム push ではありません。
- ボット再起動後、`poll_events` のカーソルは 0 に戻ります。
- `rpc_timeout` はコルーチン メソッドにのみ適用されます。
- バッファ内のメッセージは要約形式です。完全なメッセージは `recent_messages` を使用してください。
- mcp SDK 1.x と 2.x に対応します。

## コミュニティ

- Issue：<https://github.com/wsu2059q/ErisPulse-ApiMCP/issues>
- Telegram：<https://t.me/ErisPulse>
- QQ グループ：<https://qm.qq.com/q/TOwnCmypcy>

## ライセンス

[MIT](LICENSE)
