"""Preserva formatacao Telegram nas mensagens recorrentes.

Funciona como camada final do scheduler existente. Usa ContextVar para que os
wrappers de envio saibam qual recurring_message esta sendo processada, sem
alterar o comportamento das automacoes comuns do worker.
"""

import contextvars
import hashlib
import json

import recurring_messages_addon as recurring
import telegram_rich_text


_current_item = contextvars.ContextVar("recurring_rich_text_item", default=None)
_registered = False
_original_process_one = None
_original_config_signature = None


def _entities_signature(item):
    raw = (
        item.get("message_entities")
        or item.get("entities")
        or item.get("text_entities")
        or []
    ) if isinstance(item, dict) else []
    payload = json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _config_signature(item):
    return f"{_original_config_signature(item)}:entities:{_entities_signature(item)}"


async def _process_one(worker, store, item, session_key):
    token = _current_item.set(item)
    try:
        return await _original_process_one(worker, store, item, session_key)
    finally:
        _current_item.reset(token)


def _entities_for(text):
    item = _current_item.get()
    if not item:
        return None
    return telegram_rich_text.build_entities(item, text)


def _wrap_button_publisher(worker):
    publisher = worker.button_publisher
    original_send_text = publisher.send_text
    original_send_file = publisher.send_file

    async def send_text(destination_chat_id, text, entities, automation):
        rich = _entities_for(text)
        if rich is not None:
            entities = rich
        return await original_send_text(
            destination_chat_id, text, entities, automation
        )

    async def send_file(destination_chat_id, file_path, caption, entities, automation):
        rich = _entities_for(caption or "")
        if rich is not None:
            entities = rich
        return await original_send_file(
            destination_chat_id, file_path, caption, entities, automation
        )

    publisher.send_text = send_text
    publisher.send_file = send_file


def _wrap_session_client(worker):
    client = worker.client
    original_send_message = client.send_message
    original_send_file = client.send_file

    async def send_message(entity, message="", *args, **kwargs):
        rich = _entities_for(message)
        if rich is not None:
            kwargs["formatting_entities"] = rich
            kwargs["parse_mode"] = None
        return await original_send_message(entity, message, *args, **kwargs)

    async def send_file(entity, file, *args, **kwargs):
        caption = kwargs.get("caption") or ""
        rich = _entities_for(caption)
        if rich is not None:
            kwargs["formatting_entities"] = rich
            kwargs["parse_mode"] = None
        return await original_send_file(entity, file, *args, **kwargs)

    client.send_message = send_message
    client.send_file = send_file


def register(worker):
    global _registered, _original_process_one, _original_config_signature
    if _registered:
        return

    _original_process_one = recurring._process_one
    _original_config_signature = recurring._config_signature

    recurring._process_one = _process_one
    recurring._config_signature = _config_signature
    _wrap_button_publisher(worker)
    _wrap_session_client(worker)

    _registered = True
    print("[Recurring RichText] formatacao e custom emoji registrados")
