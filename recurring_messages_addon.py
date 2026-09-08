"""Scheduler duravel de mensagens recorrentes publicadas por bots Telegram.

Cenario 1:
- busca configuracoes no Lovable;
- executa apenas as tarefas pertencentes a esta sessao worker;
- envia por telegram_bot_key;
- persiste last_message_id/next_run_at em SQLite;
- apaga a mensagem anterior sem transformar falha de delete em duplicacao;
- sobrevive a restart do processo/EC2.

O endpoint pode ainda nao existir durante rollout do frontend. Nesse caso o addon
fica ocioso e tenta novamente sem derrubar o worker principal.
"""

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone


DEFAULT_ENDPOINT = "/api/public/worker/recurring-messages"
STATE_ENDPOINT = "/api/public/worker/recurring-messages/state"
DEFAULT_POLL_SECONDS = 15
DEFAULT_SESSION_KEY = "primary"
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 7 * 24 * 60


def _truthy(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {
        "1", "true", "yes", "on", "sim", "enabled", "active"
    }


def _utc_iso(timestamp=None):
    value = time.time() if timestamp is None else float(timestamp)
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _safe_interval(value):
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = 15
    return max(MIN_INTERVAL_MINUTES, min(minutes, MAX_INTERVAL_MINUTES))


def _config_signature(item):
    important = {
        "destination_chat_id": str(item.get("destination_chat_id") or ""),
        "telegram_bot_key": str(item.get("telegram_bot_key") or ""),
        "interval_minutes": _safe_interval(item.get("interval_minutes")),
        "delete_previous": _truthy(item.get("delete_previous"), True),
        "send_immediately": _truthy(item.get("send_immediately"), False),
    }
    raw = json.dumps(important, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RecurringStateStore:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._init_db()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _init_db(self):
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS recurring_message_state (
                    recurring_id TEXT PRIMARY KEY,
                    config_signature TEXT NOT NULL DEFAULT '',
                    last_message_id TEXT,
                    last_bot_key TEXT,
                    pending_delete_message_id TEXT,
                    pending_delete_bot_key TEXT,
                    next_run_at REAL NOT NULL,
                    last_sent_at REAL,
                    updated_at REAL NOT NULL
                )
                """
            )
            db.commit()

    def get_or_create(self, item, now):
        recurring_id = str(item.get("id") or "").strip()
        if not recurring_id:
            return None

        signature = _config_signature(item)
        interval_seconds = _safe_interval(item.get("interval_minutes")) * 60

        with self._connect() as db:
            row = db.execute(
                """
                SELECT config_signature, last_message_id, last_bot_key,
                       pending_delete_message_id, pending_delete_bot_key,
                       next_run_at, last_sent_at
                FROM recurring_message_state
                WHERE recurring_id = ?
                """,
                (recurring_id,),
            ).fetchone()

            if row is None:
                next_run = now if _truthy(item.get("send_immediately"), False) else now + interval_seconds
                db.execute(
                    """
                    INSERT INTO recurring_message_state (
                        recurring_id, config_signature, next_run_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (recurring_id, signature, next_run, now),
                )
                db.commit()
                return {
                    "config_signature": signature,
                    "last_message_id": None,
                    "last_bot_key": None,
                    "pending_delete_message_id": None,
                    "pending_delete_bot_key": None,
                    "next_run_at": next_run,
                    "last_sent_at": None,
                }

            state = {
                "config_signature": row[0],
                "last_message_id": row[1],
                "last_bot_key": row[2],
                "pending_delete_message_id": row[3],
                "pending_delete_bot_key": row[4],
                "next_run_at": float(row[5]),
                "last_sent_at": row[6],
            }

            # Mudanca de bot/destino/intervalo nao apaga historico. Apenas adota
            # a nova assinatura; o proximo envio segue o relogio ja persistido.
            if state["config_signature"] != signature:
                db.execute(
                    """
                    UPDATE recurring_message_state
                    SET config_signature = ?, updated_at = ?
                    WHERE recurring_id = ?
                    """,
                    (signature, now, recurring_id),
                )
                db.commit()
                state["config_signature"] = signature

            return state

    def commit_send(self, recurring_id, new_message_id, bot_key, previous_message_id, previous_bot_key, next_run_at, now):
        """Commit sincrono IMEDIATO apos Telegram aceitar a nova mensagem."""
        with self._connect() as db:
            db.execute(
                """
                UPDATE recurring_message_state
                SET last_message_id = ?,
                    last_bot_key = ?,
                    pending_delete_message_id = ?,
                    pending_delete_bot_key = ?,
                    next_run_at = ?,
                    last_sent_at = ?,
                    updated_at = ?
                WHERE recurring_id = ?
                """,
                (
                    str(new_message_id),
                    str(bot_key or ""),
                    str(previous_message_id) if previous_message_id else None,
                    str(previous_bot_key or bot_key or "") if previous_message_id else None,
                    float(next_run_at),
                    float(now),
                    float(now),
                    str(recurring_id),
                ),
            )
            db.commit()

    def clear_pending_delete(self, recurring_id, message_id, now):
        with self._connect() as db:
            db.execute(
                """
                UPDATE recurring_message_state
                SET pending_delete_message_id = NULL,
                    pending_delete_bot_key = NULL,
                    updated_at = ?
                WHERE recurring_id = ? AND pending_delete_message_id = ?
                """,
                (float(now), str(recurring_id), str(message_id)),
            )
            db.commit()


async def _delete_message(worker, destination_chat_id, message_id, bot_key, session_key):
    if not message_id:
        return True

    try:
        bot = worker.button_publisher._get_bot(explicit_key=bot_key)
        destination = await worker.button_publisher._resolve_destination(
            bot, destination_chat_id
        )
        await bot["client"].delete_messages(destination, [int(message_id)])
        print(
            f"[Recurring:{session_key}] DELETE OK dest={destination_chat_id} msg={message_id} bot={bot['key']}"
        )
        return True
    except Exception as error:
        print(
            f"[Recurring:{session_key}] DELETE FAIL dest={destination_chat_id} msg={message_id} "
            f"bot={bot_key}: {type(error).__name__}: {error}"
        )
        return False


async def _report_state(worker, item, state, status, error=None):
    payload = {
        "id": str(item.get("id") or ""),
        "status": status,
        "last_message_id": state.get("last_message_id"),
        "last_sent_at": _utc_iso(state["last_sent_at"]) if state.get("last_sent_at") else None,
        "next_run_at": _utc_iso(state["next_run_at"]) if state.get("next_run_at") else None,
        "error": str(error)[:1000] if error else None,
    }
    try:
        await worker.lovable_request(STATE_ENDPOINT, method="POST", data=payload)
    except Exception:
        # Estado local e a fonte de seguranca. Falha de telemetria nao pode
        # transformar um envio bem sucedido em retry/duplicacao.
        pass


def _extract_items(result):
    if isinstance(result, list):
        return result
    if not isinstance(result, dict):
        return []
    for key in ("recurring_messages", "messages", "items"):
        value = result.get(key)
        if isinstance(value, list):
            return value
    return []


def _belongs_to_session(item, session_key):
    configured = str(
        item.get("worker_session_key")
        or item.get("telegram_session_key")
        or DEFAULT_SESSION_KEY
    ).strip().lower()
    return configured == str(session_key).strip().lower()


def _is_valid(item):
    if not isinstance(item, dict):
        return False
    if not _truthy(item.get("enabled"), False):
        return False
    if str(item.get("transport") or "bot").strip().lower() not in {"bot", "publisher_bot"}:
        return False
    if not str(item.get("id") or "").strip():
        return False
    if not str(item.get("destination_chat_id") or "").strip():
        return False
    if not str(item.get("telegram_bot_key") or "").strip():
        return False
    if not str(item.get("message_text") or item.get("text") or "").strip():
        return False
    return True


async def _process_one(worker, store, item, session_key):
    now = time.time()
    recurring_id = str(item.get("id")).strip()
    destination = str(item.get("destination_chat_id")).strip()
    bot_key = str(item.get("telegram_bot_key")).strip()
    interval_seconds = _safe_interval(item.get("interval_minutes")) * 60
    delete_previous = _truthy(item.get("delete_previous"), True)

    state = store.get_or_create(item, now)
    if state is None:
        return

    # Retry de limpeza pendente antes do proximo envio.
    pending_id = state.get("pending_delete_message_id")
    if pending_id and delete_previous:
        deleted = await _delete_message(
            worker,
            destination,
            pending_id,
            state.get("pending_delete_bot_key") or state.get("last_bot_key") or bot_key,
            session_key,
        )
        if deleted:
            store.clear_pending_delete(recurring_id, pending_id, time.time())
            state["pending_delete_message_id"] = None
            state["pending_delete_bot_key"] = None

    if now < float(state.get("next_run_at") or 0):
        return

    text = str(item.get("message_text") or item.get("text") or "").strip()

    # Reaproveita o contrato existente de botoes. Se o backend futuramente
    # entregar buttons/buttons_enabled, o scheduler ja suporta CTA inline.
    send_config = dict(item)
    send_config["telegram_bot_key"] = bot_key
    send_config.setdefault("buttons", item.get("buttons") or [])
    send_config.setdefault("buttons_enabled", item.get("buttons_enabled", True))

    try:
        sent = await worker.button_publisher.send_text(
            destination,
            text,
            [],
            send_config,
        )

        sent_id = getattr(sent, "id", None)
        if sent_id is None:
            raise RuntimeError("Telegram nao retornou message_id para mensagem recorrente")

        sent_at = time.time()
        next_run = sent_at + interval_seconds
        previous_id = state.get("last_message_id") if delete_previous else None
        previous_bot = state.get("last_bot_key") if previous_id else None

        # NAO colocar await antes deste commit: Telegram ja aceitou o post.
        store.commit_send(
            recurring_id,
            sent_id,
            bot_key,
            previous_id,
            previous_bot,
            next_run,
            sent_at,
        )

        state.update({
            "last_message_id": str(sent_id),
            "last_bot_key": bot_key,
            "pending_delete_message_id": str(previous_id) if previous_id else None,
            "pending_delete_bot_key": previous_bot,
            "next_run_at": next_run,
            "last_sent_at": sent_at,
        })

        print(
            f"[Recurring:{session_key}] SEND OK id={recurring_id} bot={bot_key} "
            f"dest={destination} msg={sent_id} next={_utc_iso(next_run)}"
        )

        if previous_id:
            deleted = await _delete_message(
                worker,
                destination,
                previous_id,
                previous_bot or bot_key,
                session_key,
            )
            if deleted:
                store.clear_pending_delete(recurring_id, previous_id, time.time())
                state["pending_delete_message_id"] = None
                state["pending_delete_bot_key"] = None

        await _report_state(worker, item, state, "ok")

    except Exception as error:
        print(
            f"[Recurring:{session_key}] SEND FAIL id={recurring_id} bot={bot_key} dest={destination}: "
            f"{type(error).__name__}: {error}"
        )
        await _report_state(worker, item, state, "error", error=error)


async def run(worker, session_key, endpoint=DEFAULT_ENDPOINT, poll_seconds=DEFAULT_POLL_SECONDS):
    state_path = os.getenv(
        "TELEGRAM_RECURRING_STATE_FILE",
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "recurring_messages_state.sqlite3",
        ),
    )
    store = RecurringStateStore(state_path)
    warned_endpoint = False

    print(
        f"[Recurring:{session_key}] scheduler ativo endpoint={endpoint} state={state_path}"
    )

    while True:
        try:
            result = await worker.lovable_request(endpoint)
            warned_endpoint = False
            items = [
                item for item in _extract_items(result)
                if _is_valid(item) and _belongs_to_session(item, session_key)
            ]

            for item in items:
                await _process_one(worker, store, item, session_key)

        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Durante rollout, endpoint 404 nao pode derrubar o worker.
            if not warned_endpoint:
                print(
                    f"[Recurring:{session_key}] endpoint indisponivel; scheduler ocioso: "
                    f"{type(error).__name__}: {error}"
                )
                warned_endpoint = True

        await asyncio.sleep(max(5, int(poll_seconds)))
