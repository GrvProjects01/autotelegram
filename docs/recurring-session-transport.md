# Recurring Messages — Session Transport (Scenario 2)

The existing recurring scheduler now supports two transports:

- `transport=bot`: Scenario 1, uses `telegram_bot_key`.
- `transport=session`: Scenario 2, uses `worker_session_key` / `telegram_session_key` and the Telethon human session owned by that worker process.

For `transport=session`:

- `telegram_bot_key` is not required.
- `worker_session_key` must identify the connected session (`primary`, `marca_b`, etc.).
- inline buttons are intentionally ignored because they require bot publication.
- sent messages are marked as self-published to avoid loops through normal automation handlers.
- delete-previous uses the same session that sent the recurring message.
- Telegram FloodWait is persisted as a deferred `next_run_at`.
