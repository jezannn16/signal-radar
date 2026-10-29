# Demo

1. Add a synthetic or public RSS endpoint and a GitHub repository with `add-source`.
2. Add an include keyword and run `collect`; inspect `items` and `why <id>`.
3. Run `collect` again. The existing canonical URL should update in place; a changed title/body adds a row to `item_versions`.
4. Run `digest` to preview the first unsent entries. Configure a private test chat and explicitly pass `--send` only when you intend to send a real Telegram message.
5. Restart the process and inspect the same SQLite database. Source checkpoints, normalized items and delivery state persist.

Expected result depends on upstream public feeds. Tests use local synthetic XML and mocked GitHub responses; no real Telegram/API calls are made by tests.
