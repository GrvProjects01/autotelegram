"""Suporte de imagem/video para mensagens recorrentes (bot e sessao).

Extende o scheduler existente sem criar outro relogio:
- media_url opcional (HTTPS);
- media_type: image|video;
- baixa arquivo temporario com limite de tamanho;
- transport=bot usa TelegramButtonPublisher.send_file;
- transport=session usa a sessao Telethon dona da tarefa;
- texto vira caption quando existe midia;
- arquivo temporario e sempre removido.
"""

import asyncio
import ipaddress
import os
import tempfile
from urllib.parse import urlparse

import httpx
from telethon.errors import FloodWaitError

import recurring_messages_addon as recurring
import recurring_session_transport_addon as session_transport


_registered = False
_original_is_valid = recurring._is_valid
_original_process_one = recurring._process_one
_original_config_signature = recurring._config_signature

DEFAULT_MAX_MEDIA_MB = 80
ALLOWED_MEDIA_TYPES = {"image", "video"}


def _media_url(item):
    return str(
        item.get("media_url")
        or item.get("attachment_url")
        or item.get("file_url")
        or ""
    ).strip()


def _media_type(item):
    value = str(item.get("media_type") or item.get("attachment_type") or "").strip().lower()
    aliases = {
        "photo": "image",
        "image": "image",
        "img": "image",
        "video": "video",
        "mp4": "video",
    }
    return aliases.get(value, value)


def _has_text(item):
    return bool(str(item.get("message_text") or item.get("text") or "").strip())


def _has_media(item):
    return bool(_media_url(item))


def _safe_media_url(url):
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False

    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return False

    host = parsed.hostname.strip().lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return False

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True

    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
    )


def _is_valid(item):
    if not isinstance(item, dict):
        return False

    clone = dict(item)
    # Os validadores anteriores exigiam texto. Para post com midia, usamos uma
    # sentinela apenas durante a validacao, sem alterar o payload real.
    if not _has_text(item) and _has_media(item):
        clone["message_text"] = "__media_only__"

    if not _original_is_valid(clone):
        return False

    if not (_has_text(item) or _has_media(item)):
        return False

    if _has_media(item):
        if not _safe_media_url(_media_url(item)):
            return False
        if _media_type(item) not in ALLOWED_MEDIA_TYPES:
            return False

    return True


def _config_signature(item):
    base = _original_config_signature(item)
    media_bits = "|".join([
        _media_url(item),
        _media_type(item),
        str(item.get("media_filename") or ""),
    ])
    return f"{base}:media:{media_bits}"


def _max_media_bytes():
    try:
        mb = int(os.getenv("TELEGRAM_RECURRING_MAX_MEDIA_MB", str(DEFAULT_MAX_MEDIA_MB)))
    except (TypeError, ValueError):
        mb = DEFAULT_MAX_MEDIA_MB
    mb = max(1, min(mb, 200))
    return mb * 1024 * 1024


def _suffix_for(item, response):
    filename = str(item.get("media_filename") or "").strip()
    if filename:
        _, ext = os.path.splitext(filename)
        if ext and len(ext) <= 10:
            return ext

    path = urlparse(_media_url(item)).path
    _, ext = os.path.splitext(path)
    if ext and len(ext) <= 10:
        return ext

    content_type = str(response.headers.get("content-type") or "").lower()
    if "jpeg" in content_type:
        return ".jpg"
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "mp4" in content_type:
        return ".mp4"
    if "quicktime" in content_type:
        return ".mov"
    return ".bin"


async def _download_media(item):
    url = _media_url(item)
    if not _safe_media_url(url):
        raise ValueError("media_url invalida: somente HTTPS publico e permitido")

    limit = _max_media_bytes()
    timeout = httpx.Timeout(connect=15, read=120, write=30, pool=15)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
        async with http.stream("GET", url) as response:
            response.raise_for_status()

            declared = response.headers.get("content-length")
            if declared:
                try:
                    if int(declared) > limit:
                        raise ValueError(
                            f"midia excede limite de {limit // (1024 * 1024)} MB"
                        )
                except ValueError as error:
                    if "excede limite" in str(error):
                        raise

            suffix = _suffix_for(item, response)
            fd, path = tempfile.mkstemp(prefix="recurring_media_", suffix=suffix)
            total = 0
            try:
                with os.fdopen(fd, "wb") as handle:
                    async for chunk in response.aiter_bytes(1024 * 256):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > limit:
                            raise ValueError(
                                f"midia excede limite de {limit // (1024 * 1024)} MB"
                            )
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())

                if total <= 0:
                    raise ValueError("download de midia retornou arquivo vazio")
                return path, total
            except Exception:
                try:
                    os.remove(path)
                except OSError:
                    pass
                raise


async def _send_bot_media(worker, item, destination, text, bot_key):
    path, size = await _download_media(item)
    try:
        send_config = dict(item)
        send_config["telegram_bot_key"] = bot_key
        send_config.setdefault("buttons", item.get("buttons") or [])
        send_config.setdefault("buttons_enabled", item.get("buttons_enabled", True))
        sent = await worker.button_publisher.send_file(
            destination,
            path,
            text,
            [],
            send_config,
        )
        return sent, size
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def _send_session_media(worker, item, destination_id, text):
    path, size = await _download_media(item)
    try:
        destination = await session_transport._resolve_session_destination(
            worker, destination_id
        )
        sent = await worker.client.send_file(
            destination,
            path,
            caption=text or "",
            supports_streaming=_media_type(item) == "video",
        )
        return sent, size
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def _process_media(worker, store, item, session_key):
    now = __import__("time").time()
    recurring_id = str(item.get("id") or "").strip()
    destination = str(item.get("destination_chat_id") or "").strip()
    transport = session_transport._transport(item)
    text = str(item.get("message_text") or item.get("text") or "").strip()
    interval_seconds = recurring._safe_interval(item.get("interval_minutes")) * 60
    delete_previous = recurring._truthy(item.get("delete_previous"), True)

    state = store.get_or_create(item, now)
    if state is None:
        return

    pending_id = state.get("pending_delete_message_id")
    if pending_id and delete_previous:
        if transport == "session":
            deleted = await session_transport._delete_session_message(
                worker, destination, pending_id, session_key
            )
        else:
            bot_key = str(item.get("telegram_bot_key") or "").strip()
            deleted = await recurring._delete_message(
                worker,
                destination,
                pending_id,
                state.get("pending_delete_bot_key") or state.get("last_bot_key") or bot_key,
                session_key,
            )
        if deleted:
            store.clear_pending_delete(recurring_id, pending_id, __import__("time").time())
            state["pending_delete_message_id"] = None
            state["pending_delete_bot_key"] = None

    if now < float(state.get("next_run_at") or 0):
        return

    try:
        if transport == "session":
            if recurring._truthy(item.get("buttons_enabled"), False):
                print(
                    f"[Recurring Media:{session_key}] botoes ignorados id={recurring_id}; "
                    "transport=session"
                )
            sent, size = await _send_session_media(
                worker, item, destination, text
            )
            transport_key = f"session:{session_key}"
        else:
            bot_key = str(item.get("telegram_bot_key") or "").strip()
            sent, size = await _send_bot_media(
                worker, item, destination, text, bot_key
            )
            transport_key = bot_key

        sent_id = getattr(sent, "id", None)
        if sent_id is None:
            raise RuntimeError("Telegram nao retornou message_id para recorrencia com midia")

        if transport == "session":
            remember = getattr(worker, "remember_self_published", None)
            if callable(remember):
                await remember(destination, sent_id)

        sent_at = __import__("time").time()
        next_run = sent_at + interval_seconds
        previous_id = state.get("last_message_id") if delete_previous else None
        previous_transport = state.get("last_bot_key") if previous_id else None

        store.commit_send(
            recurring_id,
            sent_id,
            transport_key,
            previous_id,
            previous_transport or (transport_key if previous_id else None),
            next_run,
            sent_at,
        )

        state.update({
            "last_message_id": str(sent_id),
            "last_bot_key": transport_key,
            "pending_delete_message_id": str(previous_id) if previous_id else None,
            "pending_delete_bot_key": previous_transport or (transport_key if previous_id else None),
            "next_run_at": next_run,
            "last_sent_at": sent_at,
        })

        print(
            f"[Recurring Media:{session_key}] SEND OK id={recurring_id} "
            f"transport={transport} type={_media_type(item)} bytes={size} "
            f"dest={destination} msg={sent_id} next={recurring._utc_iso(next_run)}"
        )

        if previous_id:
            if transport == "session":
                deleted = await session_transport._delete_session_message(
                    worker, destination, previous_id, session_key
                )
            else:
                deleted = await recurring._delete_message(
                    worker,
                    destination,
                    previous_id,
                    previous_transport or transport_key,
                    session_key,
                )
            if deleted:
                store.clear_pending_delete(recurring_id, previous_id, __import__("time").time())
                state["pending_delete_message_id"] = None
                state["pending_delete_bot_key"] = None

        await recurring._report_state(worker, item, state, "ok")

    except FloodWaitError as error:
        wait_seconds = max(1, int(getattr(error, "seconds", 60) or 60))
        next_run = __import__("time").time() + wait_seconds + 5
        session_transport._defer_state(store, recurring_id, next_run)
        state["next_run_at"] = next_run
        await recurring._report_state(
            worker,
            item,
            state,
            "error",
            error=f"Telegram FloodWait: aguardar {wait_seconds}s",
        )
    except Exception as error:
        print(
            f"[Recurring Media:{session_key}] SEND FAIL id={recurring_id} "
            f"transport={transport} dest={destination}: {type(error).__name__}: {error}"
        )
        await recurring._report_state(worker, item, state, "error", error=error)


async def _process_one(worker, store, item, session_key):
    if _has_media(item):
        return await _process_media(worker, store, item, session_key)
    return await _original_process_one(worker, store, item, session_key)


def register():
    global _registered
    if _registered:
        return

    recurring._is_valid = _is_valid
    recurring._config_signature = _config_signature
    recurring._process_one = _process_one
    _registered = True
    print("[Recurring Media] suporte image/video registrado")
