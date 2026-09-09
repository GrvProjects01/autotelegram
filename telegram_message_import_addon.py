"""Importa texto e entidades de uma mensagem Telegram para o Lovable.

Arquitetura:
- o Lovable cria um pedido pendente;
- cada worker consulta apenas pedidos da propria session_key;
- a sessao Telethon resolve o link e le a mensagem original;
- texto + entidades (incluindo custom emoji) sao serializados no mesmo contrato
  usado pelas mensagens recorrentes;
- o resultado volta ao Lovable por endpoint protegido por x-worker-secret.

A midia original nao e copiada para Supabase por este addon. Apenas sinalizamos
has_media/media_kind para o painel pedir upload pelo fluxo de Storage existente.
"""

import asyncio
import re
import time
from urllib.parse import urlparse

from telethon import types


IMPORTS_ENDPOINT = "/api/public/worker/recurring-message-imports"
RESULT_ENDPOINT = "/api/public/worker/recurring-message-imports/result"
DEFAULT_POLL_SECONDS = 5
MAX_RESULTS_PER_POLL = 10


def _clean_session_key(value):
    return str(value or "primary").strip().lower()


def _extract_jobs(result):
    if isinstance(result, list):
        return result
    if not isinstance(result, dict):
        return []
    for key in ("imports", "recurring_message_imports", "items", "jobs"):
        value = result.get(key)
        if isinstance(value, list):
            return value
    return []


def _belongs_to_session(job, session_key):
    configured = _clean_session_key(
        job.get("worker_session_key")
        or job.get("telegram_session_key")
        or "primary"
    )
    return configured == _clean_session_key(session_key)


def _job_id(job):
    return str(job.get("id") or job.get("import_id") or "").strip()


def _message_url(job):
    return str(
        job.get("source_message_url")
        or job.get("message_url")
        or job.get("telegram_message_url")
        or ""
    ).strip()


def parse_message_link(url):
    """Retorna (peer_hint, message_id, link_kind).

    Aceita:
      https://t.me/canal/123
      https://t.me/c/1234567890/123
      https://t.me/c/1234567890/77/123   (topico/forum; usa ultimo id)
      https://telegram.me/canal/123

    Para links /c/, peer_hint e o id numerico completo -100... usado pelo MTProto.
    Para links publicos, peer_hint e o username sem @.
    """
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("Link da mensagem Telegram vazio")

    if not re.match(r"^https?://", raw, flags=re.I):
        raw = "https://" + raw

    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        raise ValueError("Use um link de mensagem t.me/telegram.me")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ValueError("Link Telegram incompleto; copie o link da mensagem")

    last = parts[-1]
    if not last.isdigit():
        raise ValueError("Link Telegram nao contem message_id numerico")
    message_id = int(last)
    if message_id <= 0:
        raise ValueError("message_id invalido")

    if parts[0].lower() == "c":
        if len(parts) < 3 or not parts[1].isdigit():
            raise ValueError("Link privado /c/ invalido")
        internal_id = parts[1]
        peer_hint = int(f"-100{internal_id}")
        return peer_hint, message_id, "private"

    username = parts[0].lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{4,64}", username):
        raise ValueError("Username/canal invalido no link Telegram")
    return username, message_id, "public"


async def _resolve_peer(client, peer_hint):
    try:
        return await client.get_input_entity(peer_hint)
    except Exception:
        pass

    # Fallback para IDs privados que ja estejam nos dialogs da sessao.
    target = str(peer_hint)
    async for dialog in client.iter_dialogs():
        if str(dialog.id) == target:
            return dialog.input_entity
        username = str(getattr(dialog.entity, "username", "") or "")
        if isinstance(peer_hint, str) and username.lower() == peer_hint.lower():
            return dialog.input_entity

    raise ValueError(
        "A sessao selecionada nao consegue acessar o chat da mensagem. "
        "Confirme que a conta participa do canal/grupo e abra a mensagem nela."
    )


def _entity_payload(entity):
    base = {
        "offset": int(getattr(entity, "offset", 0)),
        "length": int(getattr(entity, "length", 0)),
    }

    if isinstance(entity, types.MessageEntityBold):
        return {"type": "bold", **base}
    if isinstance(entity, types.MessageEntityItalic):
        return {"type": "italic", **base}
    if isinstance(entity, types.MessageEntityUnderline):
        return {"type": "underline", **base}
    if isinstance(entity, types.MessageEntityStrike):
        return {"type": "strikethrough", **base}
    if isinstance(entity, types.MessageEntitySpoiler):
        return {"type": "spoiler", **base}
    if isinstance(entity, types.MessageEntityCode):
        return {"type": "code", **base}
    if isinstance(entity, types.MessageEntityPre):
        return {
            "type": "pre",
            **base,
            "language": str(getattr(entity, "language", "") or ""),
        }
    if isinstance(entity, types.MessageEntityTextUrl):
        return {
            "type": "text_url",
            **base,
            "url": str(getattr(entity, "url", "") or ""),
        }
    if isinstance(entity, types.MessageEntityBlockquote):
        return {"type": "blockquote", **base}
    if isinstance(entity, types.MessageEntityCustomEmoji):
        # String de proposito: IDs Telegram excedem a precisao segura de Number JS.
        return {
            "type": "custom_emoji",
            **base,
            "custom_emoji_id": str(getattr(entity, "document_id", "") or ""),
        }

    # Entidades que ja estao explicitamente no texto (URL, @mention, #hashtag,
    # bot_command, email etc.) nao precisam de metadata para sobreviver ao copy.
    return None


def serialize_entities(message):
    result = []
    for entity in (getattr(message, "entities", None) or []):
        payload = _entity_payload(entity)
        if payload and payload.get("length", 0) > 0:
            result.append(payload)
    result.sort(key=lambda item: (item["offset"], -item["length"]))
    return result


def _media_kind(message):
    if getattr(message, "photo", None) is not None:
        return "image"
    if getattr(message, "video", None) is not None:
        return "video"

    document = getattr(message, "document", None)
    if document is not None:
        mime = str(getattr(document, "mime_type", "") or "").lower()
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("image/"):
            return "image"
        return "document"

    if getattr(message, "media", None) is not None:
        return "other"
    return None


async def import_message(worker, job, session_key):
    job_id = _job_id(job)
    url = _message_url(job)
    if not job_id:
        raise ValueError("Pedido de importacao sem id")
    if not url:
        raise ValueError("Pedido de importacao sem source_message_url")

    peer_hint, message_id, link_kind = parse_message_link(url)
    peer = await _resolve_peer(worker.client, peer_hint)
    message = await worker.client.get_messages(peer, ids=message_id)

    if message is None:
        raise ValueError("Mensagem nao encontrada ou sem acesso pela sessao selecionada")

    text = str(getattr(message, "message", "") or "")
    entities = serialize_entities(message)
    media_kind = _media_kind(message)
    custom_count = sum(1 for item in entities if item.get("type") == "custom_emoji")

    chat_id = str(getattr(message, "chat_id", "") or "")

    return {
        "id": job_id,
        "status": "ok",
        "worker_session_key": _clean_session_key(session_key),
        "source_message_url": url,
        "source_chat_id": chat_id or None,
        "source_message_id": str(message_id),
        "source_link_kind": link_kind,
        "message_text": text,
        "message_entities": entities,
        "custom_emoji_count": custom_count,
        "has_media": media_kind is not None,
        "media_kind": media_kind,
        "media_imported": False,
        "media_notice": (
            "A mensagem original possui midia. Texto/formatacao foram importados; "
            "anexe a midia pelo upload do painel."
            if media_kind is not None else None
        ),
        "imported_at": time.time(),
        "error": None,
    }


async def _post_result(worker, payload):
    await worker.lovable_request(RESULT_ENDPOINT, method="POST", data=payload)


async def run(worker, session_key, poll_seconds=DEFAULT_POLL_SECONDS):
    """Loop de fila. Falha/404 do endpoint nao derruba o worker principal."""
    recent = {}
    warned_endpoint = False

    print(f"[Telegram Import:{session_key}] fila ativa endpoint={IMPORTS_ENDPOINT}")

    while True:
        try:
            response = await worker.lovable_request(IMPORTS_ENDPOINT)
            warned_endpoint = False
            now = time.monotonic()

            # Evita reprocessar imediatamente se o backend ainda estiver
            # propagando o resultado. Expira rapido para permitir recovery.
            recent = {key: expiry for key, expiry in recent.items() if expiry > now}

            jobs = [
                job for job in _extract_jobs(response)
                if isinstance(job, dict)
                and _belongs_to_session(job, session_key)
                and _job_id(job)
            ][:MAX_RESULTS_PER_POLL]

            for job in jobs:
                job_id = _job_id(job)
                if job_id in recent:
                    continue

                recent[job_id] = time.monotonic() + 30
                try:
                    payload = await import_message(worker, job, session_key)
                    await _post_result(worker, payload)
                    print(
                        f"[Telegram Import:{session_key}] OK id={job_id} "
                        f"entities={len(payload['message_entities'])} "
                        f"premium={payload['custom_emoji_count']}"
                    )
                except Exception as error:
                    payload = {
                        "id": job_id,
                        "status": "error",
                        "worker_session_key": _clean_session_key(session_key),
                        "source_message_url": _message_url(job) or None,
                        "message_text": None,
                        "message_entities": [],
                        "error": f"{type(error).__name__}: {error}"[:1000],
                    }
                    try:
                        await _post_result(worker, payload)
                    except Exception:
                        pass
                    print(
                        f"[Telegram Import:{session_key}] FAIL id={job_id}: "
                        f"{type(error).__name__}: {error}"
                    )

        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not warned_endpoint:
                print(
                    f"[Telegram Import:{session_key}] endpoint indisponivel; fila ociosa: "
                    f"{type(error).__name__}: {error}"
                )
                warned_endpoint = True

        await asyncio.sleep(max(3, int(poll_seconds)))
