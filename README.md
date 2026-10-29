# Signal Radar

Локальный монитор публичных RSS/Atom публикаций и GitHub Releases с нормализацией ссылок, объяснимым приоритетом и журналом доставки.

## Реализовано

- RSS/Atom reader на XML stdlib; официальный GitHub REST API `repos/{owner}/{repo}/releases`, необязательный token и pagination через Link header.
- Изолированный сбор источников: сбой одного источника записывается в source status и не мешает остальным.
- Checkpoint/last fetched timestamp, ETag/Last-Modified checkpoint, fetched/published UTC timestamps, canonical URL и SHA-256 content hash.
- Повторный элемент обновляется, а предыдущая версия сохраняется в `item_versions`; одинаковые URL с tracking params дедуплицируются.
- Include/exclude keyword rules, rule score и причина включения; `/why` CLI команда показывает объяснение и первичный URL.
- CLI preview/dry run, ручная Telegram доставка `digest --send`, daily/weekly scheduler в `run`, timezone, quiet hours, global minimum interval и delivery log. Внешняя доставка at-most-once; только явный HTTP 429 повторяется до двух раз с Retry-After.

## Запуск

Нужен Python 3.11+.

```sh
uv venv --python 3.12
uv pip install -e '.[dev]'
uv run signal-radar add-source rss Python https://www.python.org/blogs/rss/
uv run signal-radar add-source github Python-Releases python/cpython
uv run signal-radar rule include security
uv run signal-radar collect
uv run signal-radar items
uv run signal-radar why 1
```

Для Telegram доставки добавьте optional dependency (`uv sync --extra telegram --extra dev`). Для ежедневного (или `DIGEST_CADENCE=weekly`, по понедельникам) дайджеста скопируйте `.env.example` в `.env`, задайте `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, timezone/quiet hours и запустите:

```sh
uv run signal-radar run --interval 900
```

Scheduler собирает источники не чаще указанного интервала и проверяет окно доставки каждую минуту. Сначала используйте `uv run signal-radar digest` для preview. Ручная отправка требует `uv run signal-radar digest --send`.

## Проверки

```sh
uv run pytest
uv run ruff check src tests
```

## Ограничения

GitHub адаптер сейчас читает Releases, а не issues/commits. Пагинация ограничена 10 страницами за один запуск. Для RSS хранится checkpoint заголовка, но условный запрос If-None-Match пока не включён. Include/exclude фильтры общие для пользователя и источников; нет UI для настроек темы. Содержимое RSS считается недоверенным и показывается только как URL/title в дайджесте. Успешная и неуспешная доставка не повторяется автоматически, кроме явного 429; неопределённый сетевой результат может означать, что Telegram принял сообщение, поэтому повтор запрещён. OAuth/LLM сводки не включены. Используйте умеренную частоту сбора и соблюдайте правила поставщика API/RSS.
