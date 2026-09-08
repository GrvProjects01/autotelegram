"""Transportador por sessao humana para o scheduler de mensagens recorrentes.

Estende recurring_messages_addon sem criar um segundo scheduler:
- transport=bot continua no fluxo original;
- transport=session usa a sessao Telethon do worker dono da tarefa;
- marca mensagens como self-published para evitar loops/republicacao;
- delete da mensagem anterior usa a mesma sessao que publicou;
- FloodWait adia o proximo ciclo em vez de martelar a API.
"""

import asyncio
import sqlite3
import time

from telethon.errors import FloodWaitError

import recurring_messages_addon as recurring


_registered = False
_original_is_valid = recurring._is_valid
_original_process_one = recurring._process_one
_original_config_signature = recurring._config_signature


def _transport(item):
    value = str(item.get("transport") or "bot").strip().lower()
    if value in {"publisher_bot", "publisher", "bot"}:
        return "bot"
    if value in {"session", "telegram_session", "human", "user"}:
        return "session"
    return value


def _session_key(item):
    return str(
        item.get("worker_session_key")
        or item.get("telegram_session_key")
        or recurring.DEFAULT_SESSION_KEY
    ).strip().lower()


def _config_signature(item):
    clone = dict(item)
    # Faz transport/sessao participarem da assinatura duravel do agendamento.
    clone["telegram_bot_key"] = (
        str(item.get("telegram_bot_key") or "")
        if _transport(item) == "bot"
        else f"session:{_session_key(item)}"
    )
    base_signature = _original_config_signature(clone)
    return f"{_transport(item)}:{_session_key(item)}:{base_signature}"


def _is_valid(item):
    if not isinstance(item, dict):
        return False

    transport = _transport(item)
    if transport == "bot":
        return _original_is_valid(item)

    if transport != "session":
        return False
    if not recurring._truthy(item.get("enabled"), False):
        return False
    if not str(item.get("id") or "").strip():
        return False
    if not str(item.get("destination_chat_id") or "").strip():
        return False
    if not str(item.get("message_text") or item.get("text") or "").strip():
        return False
    return True


async def _resolve_session_destination(worker, destination_chat_id):
    destination = str(destination_chat_id).strip()

    # IDs sincronizados pelo painel normalmente chegam como inteiros (-100...).
    try:
        return await worker.client.get_input_entity(int(destination))
    except Exception:
        pass

    try:
        return await worker.client.get_input_entity(destination)
    except Exception:
        pass

    async for dialog in worker.client.iter_dialogs():
        if str(dialog.id) == destination:
            return dialog.input_entity

    raise ValueError(
        f"Sessao nao consegue resolver o destino {destination}. "
        "Confirme que a conta participa do grupo/canal e possui permissao para publicar."
    )


def _defer_state(store, recurring_id, next_run_at):
    """Adia uma tarefa duravelmente apos FloodWait."""
    with sqlite3.connect(store.path, timeout=10) as db:
        db.execute(
            """
            UPDATE recurring_message_state
            SET next_run_at = ?, updated_at = ?
            WHERE recurring_id = ?
            """,
            (float(next_run_at), float(time.time()), str(recurring_id)),
        )
        db.commit()


async def _delete_session_message(worker, destination_chat_id, message_id, session_key):
    if not message_id:
        return True
    try:
        destination = await _resolve_session_destination(worker, destination_chat_id)
        await worker.client.delete_messages(
            destination,
            [int(message_id)],
            revoke=True,
        )
        print(
            f"[Recurring Session:{session_key}] DELETE OK "
            f"dest={destination_chat_id} msg={message_id}"
        )
        return True
    except Exception as error:
        print(
            f"[Recurring Session:{session_key}] DELETE FAIL "
            f"dest={destination_chat_id} msg={message_id}: "
            f"{type(error).__name__}: {error}"
        )
        return False


async def _process_session(worker, store, item, session_key):
    now = time.time()
    recurring_id = str(item.get("id")).strip()
    destination_id = str(item.get("destination_chat_id")).strip()
    interval_seconds = recurring._safe_interval(item.get("interval_minutes")) * 60
    delete_previous = recurring._truthy(item.get("delete_previous"), True)

    state = store.get_or_create(item, now)
    if state is None:
        return

    # Limpeza pendente sempre ocorre com a mesma sessao dona da recorrencia.
    pending_id = state.get("pending_delete_message_id")
    if pending_id and delete_previous:
        deleted = await _delete_session_message(
            worker,
            destination_id,
            pending_id,
            session_key,
        )
        if deleted:
            store.clear_pending_delete(recurring_id, pending_id, time.time())
            state["pending_delete_message_id"] = None
            state["pending_delete_bot_key"] = None

    if now < float(state.get("next_run_at") or 0):
        return

    text = str(item.get("message_text") or item.get("text") or "").strip()

    if recurring._truthy(item.get("buttons_enabled"), False):
        # Contas humanas nao podem publicar inline keyboard como bot.
        print(
            f"[Recurring Session:{session_key}] botoes ignorados id={recurring_id}; "
            "inline buttons exigem transport=bot"
        )

    try:
        destination = await _resolve_session_destination(worker, destination_id)
        sent = await worker.client.send_message(
            destination,
            text,
            formatting_entities=[],
        )

        sent_id = getattr(sent, "id", None)
        if sent_id is None:
            raise RuntimeError("Telegram nao retornou message_id para recorrencia por sessao")

        # Impede que handlers do proprio worker tratem a recorrencia como origem nova.
        remember = getattr(worker, "remember_self_published", None)
        if callable(remember):
            await remember(destination_id, sent_id)

        sent_at = time.time()
        next_run = sent_at + interval_seconds
        previous_id = state.get("last_message_id") if delete_previous else None
        transport_key = f"session:{session_key}"

        # Telegram ja aceitou o post: persistir antes de qualquer outro await.
        store.commit_send(
            recurring_id,
            sent_id,
            transport_key,
            previous_id,
            transport_key if previous_id else None,
            next_run,
            sent_at,
        )

        state.update({
            "last_message_id": str(sent_id),
            "last_bot_key": transport_key,
            "pending_delete_message_id": str(previous_id) if previous_id else None,
            "pending_delete_bot_key": transport_key if previous_id else None,
            "next_run_at": next_run,
            "last_sent_at": sent_at,
        })

        print(
            f"[Recurring Session:{session_key}] SEND OK id={recurring_id} "
            f"dest={destination_id} msg={sent_id} next={recurring._utc_iso(next_run)}"
        )

        if previous_id:
            deleted = await _delete_session_message(
                worker,
                destination_id,
                previous_id,
                session_key,
            )
            if deleted:
                store.clear_pending_delete(recurring_id, previous_id, time.time())
                state["pending_delete_message_id"] = None
                state["pending_delete_bot_key"] = None

        await recurring._report_state(worker, item, state, "ok")

    except FloodWaitError as error:
        wait_seconds = max(1, int(getattr(error, "seconds", 60) or 60))
        next_run = time.time() + wait_seconds + 5
        _defer_state(store, recurring_id, next_run)
        state["next_run_at"] = next_run
        message = f"Telegram FloodWait: aguardar {wait_seconds}s"
        print(
            f"[Recurring Session:{session_key}] FLOOD WAIT id={recurring_id} "
            f"wait={wait_seconds}s next={recurring._utc_iso(next_run)}"
        )
        await recurring._report_state(worker, item, state, "error", error=message)

    except Exception as error:
        print(
            f"[Recurring Session:{session_key}] SEND FAIL id={recurring_id} "
            f"dest={destination_id}: {type(error).__name__}: {error}"
        )
        await recurring._report_state(worker, item, state, "error", error=error)


async def _process_one(worker, store, item, session_key):
    if _transport(item) == "session":
        return await _process_session(worker, store, item, session_key)
    return await _original_process_one(worker, store, item, session_key)


def register():
    global _registered
    if _registered:
        return

    recurring._config_signature = _config_signature
    recurring._is_valid = _is_valid
    recurring._process_one = _process_one
    _registered = True
    print("[Recurring Session] transport=session registrado")
