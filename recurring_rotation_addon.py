"""Rotacao duravel de mensagens recorrentes e variantes de voz/audio.

Contrato esperado por tarefa recorrente:
- rotation_enabled: bool
- rotation_items: lista ordenada de variantes
- cada variante pode conter:
  id/key, enabled, sort_order, kind,
  message_text/text, message_entities,
  media_url, media_type, media_filename, send_as_voice_note.

O scheduler continua sendo o mesmo. Esta camada apenas escolhe uma variante por
ciclo e avanca o cursor SOMENTE depois que o envio foi confirmado no estado
SQLite. Assim restart da EC2 nao pula variante nem duplica rotacao.
"""

import hashlib
import json
import sqlite3
import time

import recurring_messages_addon as recurring


_registered = False
_original_is_valid = None
_original_process_one = None
_original_config_signature = None
_original_init_db = None


def _truthy(value, default=False):
    return recurring._truthy(value, default)


def _rotation_items(item):
    if not isinstance(item, dict):
        return []

    raw = (
        item.get("rotation_items")
        or item.get("recurring_variants")
        or item.get("variants")
        or []
    )
    if not isinstance(raw, list):
        return []

    normalized = []
    for index, variant in enumerate(raw):
        if not isinstance(variant, dict):
            continue
        if variant.get("enabled") is False:
            continue

        clone = dict(variant)
        clone.setdefault("sort_order", index)
        clone.setdefault("id", clone.get("key") or f"variant-{index + 1}")
        normalized.append(clone)

    def order_key(value):
        try:
            order = int(value.get("sort_order", 0))
        except (TypeError, ValueError):
            order = 0
        return (order, str(value.get("id") or value.get("key") or ""))

    return sorted(normalized, key=order_key)


def _rotation_enabled(item):
    variants = _rotation_items(item)
    if not variants:
        return False
    if "rotation_enabled" in item:
        return _truthy(item.get("rotation_enabled"), False)
    return len(variants) > 1


def _variant_key(variant, index):
    return str(
        variant.get("id")
        or variant.get("key")
        or variant.get("name")
        or f"variant-{index + 1}"
    ).strip()


def _merge_variant(parent, variant, index):
    merged = dict(parent)

    # Campos de conteudo nao devem vazar da variante anterior/parent quando a
    # variante atual e text-only.
    for field in (
        "message_text", "text", "message_entities", "entities", "text_entities",
        "media_url", "attachment_url", "file_url", "media_type",
        "attachment_type", "media_filename", "send_as_voice_note",
    ):
        merged.pop(field, None)

    for key, value in variant.items():
        if key in {"enabled", "sort_order"}:
            continue
        merged[key] = value

    kind = str(
        variant.get("kind")
        or variant.get("type")
        or variant.get("media_type")
        or "text"
    ).strip().lower()

    if kind in {"audio", "voice", "voice_note", "voice-message", "voice_message"}:
        merged["media_type"] = "voice"
        merged["send_as_voice_note"] = True
    elif kind in {"image", "photo"}:
        merged["media_type"] = "image"
    elif kind in {"video", "mp4"}:
        merged["media_type"] = "video"

    merged["_rotation_selected"] = True
    merged["_rotation_variant_key"] = _variant_key(variant, index)
    merged["_rotation_variant_index"] = int(index)
    # Mantem a colecao inteira para assinatura estavel.
    merged["rotation_items"] = _rotation_items(parent)
    merged["rotation_enabled"] = True

    return merged


def _rotation_hash(item):
    payload = []
    for index, variant in enumerate(_rotation_items(item)):
        payload.append({
            "id": _variant_key(variant, index),
            "sort_order": variant.get("sort_order", index),
            "kind": variant.get("kind") or variant.get("type") or variant.get("media_type"),
            "text": variant.get("message_text") or variant.get("text") or "",
            "entities": variant.get("message_entities") or variant.get("entities") or [],
            "media_url": variant.get("media_url") or variant.get("attachment_url") or variant.get("file_url") or "",
            "media_type": variant.get("media_type") or "",
            "media_filename": variant.get("media_filename") or "",
            "send_as_voice_note": _truthy(variant.get("send_as_voice_note"), False),
        })
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _config_signature(item):
    if not _rotation_enabled(item):
        return _original_config_signature(item)

    invariant = dict(item)
    for field in (
        "message_text", "text", "message_entities", "entities", "text_entities",
        "media_url", "attachment_url", "file_url", "media_type",
        "attachment_type", "media_filename", "send_as_voice_note",
        "_rotation_selected", "_rotation_variant_key", "_rotation_variant_index",
    ):
        invariant.pop(field, None)

    # O hash das variantes participa da assinatura, mas a variante selecionada
    # no ciclo atual nao altera a assinatura.
    base = _original_config_signature(invariant)
    return f"{base}:rotation:{_rotation_hash(item)}"


def _is_valid(item):
    if not _rotation_enabled(item):
        return _original_is_valid(item)

    variants = _rotation_items(item)
    if not variants:
        return False

    # A tarefa e valida se pelo menos uma variante passa no validador completo
    # da cadeia (session + media).
    for index, variant in enumerate(variants):
        if _original_is_valid(_merge_variant(item, variant, index)):
            return True
    return False


def _ensure_rotation_columns(path):
    with sqlite3.connect(path, timeout=10) as db:
        columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(recurring_message_state)").fetchall()
        }
        if "rotation_cursor" not in columns:
            db.execute(
                "ALTER TABLE recurring_message_state "
                "ADD COLUMN rotation_cursor INTEGER NOT NULL DEFAULT 0"
            )
        if "last_variant_key" not in columns:
            db.execute(
                "ALTER TABLE recurring_message_state "
                "ADD COLUMN last_variant_key TEXT"
            )
        db.commit()


def _read_rotation_state(store, recurring_id):
    _ensure_rotation_columns(store.path)
    with sqlite3.connect(store.path, timeout=10) as db:
        row = db.execute(
            """
            SELECT rotation_cursor, last_variant_key, last_sent_at
            FROM recurring_message_state
            WHERE recurring_id = ?
            """,
            (str(recurring_id),),
        ).fetchone()
    if not row:
        return 0, None, None
    return int(row[0] or 0), row[1], row[2]


def _commit_rotation_cursor(store, recurring_id, cursor, variant_key):
    _ensure_rotation_columns(store.path)
    now = time.time()
    with sqlite3.connect(store.path, timeout=10) as db:
        db.execute(
            """
            UPDATE recurring_message_state
            SET rotation_cursor = ?, last_variant_key = ?, updated_at = ?
            WHERE recurring_id = ?
            """,
            (int(cursor), str(variant_key or ""), float(now), str(recurring_id)),
        )
        db.commit()


async def _process_one(worker, store, item, session_key):
    if not _rotation_enabled(item):
        return await _original_process_one(worker, store, item, session_key)

    recurring_id = str(item.get("id") or "").strip()
    variants = _rotation_items(item)
    if not recurring_id or not variants:
        return

    cursor, _, before_sent_at = _read_rotation_state(store, recurring_id)
    index = cursor % len(variants)
    variant = variants[index]
    variant_key = _variant_key(variant, index)
    selected = _merge_variant(item, variant, index)

    # Se a variante atual ficou invalida (ex.: midia removida), procura a
    # proxima valida sem travar a rotacao inteira.
    attempts = 0
    while attempts < len(variants) and not _original_is_valid(selected):
        index = (index + 1) % len(variants)
        variant = variants[index]
        variant_key = _variant_key(variant, index)
        selected = _merge_variant(item, variant, index)
        attempts += 1

    if attempts >= len(variants) and not _original_is_valid(selected):
        print(
            f"[Recurring Rotation:{session_key}] nenhuma variante valida "
            f"id={recurring_id}"
        )
        return

    print(
        f"[Recurring Rotation:{session_key}] id={recurring_id} "
        f"cursor={cursor} variante={variant_key}"
    )

    await _original_process_one(worker, store, selected, session_key)

    # Avanca SOMENTE se o scheduler realmente registrou um novo envio.
    _, _, after_sent_at = _read_rotation_state(store, recurring_id)
    if after_sent_at is not None and after_sent_at != before_sent_at:
        next_cursor = (index + 1) % len(variants)
        _commit_rotation_cursor(store, recurring_id, next_cursor, variant_key)
        print(
            f"[Recurring Rotation:{session_key}] ADVANCE id={recurring_id} "
            f"{index}->{next_cursor} enviada={variant_key}"
        )


def register():
    global _registered
    global _original_is_valid, _original_process_one, _original_config_signature
    if _registered:
        return

    _original_is_valid = recurring._is_valid
    _original_process_one = recurring._process_one
    _original_config_signature = recurring._config_signature

    recurring._is_valid = _is_valid
    recurring._process_one = _process_one
    recurring._config_signature = _config_signature

    _registered = True
    print("[Recurring Rotation] texto/audio sequencial registrado")
