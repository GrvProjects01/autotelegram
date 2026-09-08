# Scenario 1 — Recurring Telegram Messages

- Bot-only recurring scheduler.
- Durable SQLite schedule state.
- Exactly one worker owns each recurring task via `worker_session_key`.
- Default owner: `primary`.
- New message is sent before previous one is deleted.
- Failed deletion is retried from persisted pending state.
- Optional inline buttons reuse the existing publisher contract.
- Lovable endpoint absence is tolerated during rollout.
