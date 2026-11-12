<div align="center">

[简体中文](README.md) | [繁體中文](README.zh-TW.md) | [English](README.en.md) | [日本語](README.ja.md) | Русский

<img src="https://raw.githubusercontent.com/ErisPulse/ErisPulse/main/.github/assets/ErisPulseLogo.png" width="180" alt="ErisPulse-ApiMCP" />

</div>

# ErisPulse-ApiMCP

Открывает возможности рантайма бота ErisPulse через **MCP (Model Context Protocol)** для AI-клиентов (Claude Desktop и др.): приём и отправка сообщений, вызов API модулей, управление модулями/адаптерами, наблюдение за событиями, поиск по документации фреймворка.

## Архитектура

- Внутри процесса бота модуль ApiMCP публикует REST-мост (`/apimcp/*`, авторизация по token) на HTTP-сервере фреймворка.
- Отдельный процесс `erispulse-apimcp` поднимает stdio-сервер на официальном `mcp` SDK и пересылает tools в бота по keep-alive HTTP (таймаут 10 с).
- Если бот офлайн или мост не настроен, tools возвращают читаемую ошибку без стектрейса.

## Установка

```bash
epsdk install ErisPulse-ApiMCP
# или
pip install ErisPulse-ApiMCP
```

После установки перезапустите бота: модуль загружается при старте, мост подключается к HTTP-серверу фреймворка (по умолчанию `0.0.0.0:8000`).

## Настройка token

**Token не генерируется автоматически.** Без `[ApiMCP] token` (или переменной окружения) мост неактивен: `health` возвращает 200, остальные эндпоинты — **503**.

- Файл конфигурации: `[ApiMCP] token = "длинная случайная строка"`
- Переменная окружения: `ERISPLUSE_APIMCP_TOKEN` (приоритетнее)

```bash
curl -H "X-ApiMCP-Token: <token>" http://127.0.0.1:8000/apimcp/health
curl http://127.0.0.1:8000/apimcp/health   # не настроен: 200, data.status = "unconfigured"
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

`--url` / `--token` также поддерживаются, но `--token` виден в списке процессов; предпочтительна переменная окружения. `--url` требует `http://` или `https://`.

## MCP Tools (17)

| Категория | Tools |
| --- | --- |
| Модули | `list_modules` · `get_module_info` · `call_module` · `reload_module` |
| Адаптеры | `list_adapters` · `get_adapter_status` · `reload_adapter` |
| Сообщения | `send_message` (`account` выбирает аккаунт бота) · `recent_messages` |
| События | `subscribe_events` · `unsubscribe` · `poll_events` |
| Документация | `search_erispulse_docs` · `read_erispulse_doc` · `refresh_erispulse_docs` (собственный движок документации с кэшем по версии фреймворка, без зависимости от других модулей) |
| Наблюдение | `get_topology` · `sdk_health` |

События читаются опросом: `subscribe_events`, затем периодический `poll_events`. При переполнении буфера удаляются самые старые события (`dropped` — счётчик).

## REST-эндпоинты (`/apimcp/*`)

Все — `POST` (`health` также `GET`), заголовок `X-ApiMCP-Token`, тело JSON:

`modules/list` · `modules/info` · `modules/call` · `modules/reload` · `adapters/list` · `adapters/status` · `adapters/reload` · `messages/send` · `messages/recent` · `events/subscribe` · `events/unsubscribe` · `events/poll` · `docs/search` · `docs/read` · `docs/refresh` · `topology` · `health`

Ответ: `{"ok": true, "data": {...}}`; ошибка: `{"ok": false, "error": "..."}` + 400/401/403/404/413/500/503.

- Лимит: 30 запросов/минуту на IP клиента (429 + `Retry-After`)
- Тело ≤ 256 KB; content в `messages/send` ≤ 32 KB; активных подписок ≤ 32
- Числовые параметры принимают числа и числовые строки

## Конфигурация (`[ApiMCP]`)

| Ключ | По умолчанию | Описание |
| --- | --- | --- |
| `token` | `""` | Token моста; обязателен (иначе 503); переопределяется `ERISPLUSE_APIMCP_TOKEN` |
| `allow_module_reload` | `true` | Разрешить перезагрузку модулей |
| `allow_adapter_reload` | `false` | Разрешить перезагрузку адаптеров |
| `module_call_whitelist` | `[]` | `["Mod"]` или `["Mod.method"]`; пусто = без ограничений |
| `send_target_whitelist` | `[]` | `"type:id"` или `"type:*"`; пусто = без ограничений |
| `event_types_allowed` | `[]` | Разрешённые типы событий; пусто = без ограничений |
| `event_buffer_size` | `1000` | Размер кольцевого буфера (10–100000) |
| `max_poll_events` | `200` | Максимум событий за опрос (≤500) |
| `recent_window_seconds` | `3600` | Окно истории сообщений (с) |

Все ключи поддерживают горячую перезагрузку.

## Безопасность

- Для локального использования привяжите HTTP-сервер фреймворка к `127.0.0.1`.
- Token даёт полный доступ к мосту; один token = один домен доверия.
- Передавайте token через переменную окружения: `--token` виден в списке процессов.
- Сужайте `module_call_whitelist` и `send_target_whitelist`, оставляйте `allow_adapter_reload = false`.
- Из вывода удаляются ключи, похожие на секреты и пути, а также строковые значения вида путей или с `..`.

## Разработка

```bash
uv sync
uv run pytest
uv run erispulse-apimcp --help
```

## Ограничения

- Поток событий основан на опросе, без push в реальном времени.
- После перезапуска бота курсоры `poll_events` сбрасываются.
- `rpc_timeout` действует только для корутин.
- Буферизованные сообщения — краткие формы; полные сообщения через `recent_messages`.
- Поддерживаются mcp SDK 1.x и 2.x.

## Сообщество

- Issues: <https://github.com/wsu2059q/ErisPulse-ApiMCP/issues>
- Telegram: <https://t.me/ErisPulse>
- QQ: <https://qm.qq.com/q/TOwnCmypcy>

## Лицензия

[MIT](LICENSE)
