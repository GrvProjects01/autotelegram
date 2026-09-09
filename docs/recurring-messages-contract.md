# Recurring Messages — Worker Contract

Endpoint de leitura:

`GET /api/public/worker/recurring-messages`

Resposta aceita:

```json
{
  "recurring_messages": [
    {
      "id": "uuid",
      "name": "Oferta 15 min",
      "enabled": true,
      "transport": "bot",
      "worker_session_key": "primary",
      "destination_chat_id": "-1001234567890",
      "telegram_bot_key": "closeflix",
      "message_text": "Texto da mensagem",
      "interval_minutes": 15,
      "delete_previous": true,
      "send_immediately": false,
      "buttons_enabled": false,
      "buttons": []
    }
  ]
}
```

Endpoint de telemetria/estado:

`POST /api/public/worker/recurring-messages/state`

Payload:

```json
{
  "id": "uuid",
  "status": "ok",
  "last_message_id": "123",
  "last_sent_at": "2026-09-08T12:00:00+00:00",
  "next_run_at": "2026-09-08T12:15:00+00:00",
  "error": null
}
```

## Regras

- `transport` deve ser `bot` no Cenario 1.
- `worker_session_key` default: `primary`.
- somente um worker processa cada recorrencia.
- `interval_minutes` aceito pelo worker: 1..10080.
- `delete_previous=true` envia a nova mensagem primeiro e depois apaga a anterior.
- estado critico e persistido localmente em SQLite para sobreviver a restart.
- falha no endpoint/telemetria do Lovable nao derruba o worker principal.
