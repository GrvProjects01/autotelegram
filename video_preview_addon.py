"""Preserva thumbnails/previews de videos ao reupar midia Telegram.

Problema resolvido:
- o arquivo de video chegava completo e com metadata correta, mas sem thumbnail;
- Telegram aceitava o upload, porem o card no grupo/canal podia aparecer preto
  ate o usuario tocar no video;
- isso afetava publicacoes por bot e por sessao humana.

Estrategia:
- captura o thumbnail JPEG original do documento Telegram quando existe;
- associa esse thumbnail ao arquivo baixado para o fluxo BOT;
- injeta `thumb=` no send_file dos bots;
- injeta `thumb=` diretamente no fluxo de reupload da sessao;
- tudo e best-effort: ausencia de thumb nunca derruba a automacao.
"""

import contextvars
import os
import shutil
import tempfile
from collections import OrderedDict


_BOT_THUMB = contextvars.ContextVar("telegram_video_preview_thumb", default=None)
_THUMBS_BY_MEDIA = OrderedDict()
_THUMB_CACHE_MAX = 500
_REGISTERED = False


def _document(value):
    if value is None:
        return None
    document = getattr(value, "document", None)
    if document is not None:
        return document
    media = getattr(value, "media", None)
    return getattr(media, "document", None) if media is not None else None


def _is_video(value):
    document = _document(value)
    if document is None:
        return False
    mime = str(getattr(document, "mime_type", "") or "").lower()
    if mime.startswith("video/"):
        return True
    for attr in list(getattr(document, "attributes", None) or []):
        if type(attr).__name__ == "DocumentAttributeVideo":
            return True
    return False


def _thumb_area(thumb):
    try:
        return int(getattr(thumb, "w", 0) or 0) * int(getattr(thumb, "h", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _select_photo_thumb(value):
    document = _document(value)
    if document is None:
        return None

    candidates = []
    for thumb in list(getattr(document, "thumbs", None) or []):
        name = type(thumb).__name__
        # VideoSize e animacoes nao servem como thumb= JPEG no upload.
        if name.startswith("Video") or name == "PhotoStrippedSize":
            continue
        width = int(getattr(thumb, "w", 0) or 0)
        height = int(getattr(thumb, "h", 0) or 0)
        if width <= 0 or height <= 0:
            continue
        candidates.append(thumb)

    if not candidates:
        return None

    # Telethon/Telegram aceitam melhor thumbs pequenas. Prefere a maior ate 320px.
    within = [
        item for item in candidates
        if int(getattr(item, "w", 0) or 0) <= 320
        and int(getattr(item, "h", 0) or 0) <= 320
    ]
    pool = within or candidates
    return sorted(pool, key=_thumb_area)[-1]


def _remember(media_path, thumb_path):
    if not media_path or not thumb_path:
        return
    key = os.path.abspath(os.fspath(media_path))
    if key in _THUMBS_BY_MEDIA:
        _THUMBS_BY_MEDIA.pop(key, None)
    _THUMBS_BY_MEDIA[key] = thumb_path
    while len(_THUMBS_BY_MEDIA) > _THUMB_CACHE_MAX:
        _THUMBS_BY_MEDIA.popitem(last=False)


def _lookup(media_path):
    if not media_path:
        return None
    try:
        key = os.path.abspath(os.fspath(media_path))
    except TypeError:
        return None
    value = _THUMBS_BY_MEDIA.get(key)
    if value and os.path.exists(value):
        return value
    return None


async def _download_preview(raw_download_media, source, directory, session_key):
    if not _is_video(source):
        return None
    thumb = _select_photo_thumb(source)
    if thumb is None:
        print(f"[Video Preview:{session_key}] origem sem thumbnail fotografico")
        return None

    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, "telegram_preview.jpg")
    try:
        result = await raw_download_media(
            source,
            file=target,
            thumb=thumb,
        )
        path = os.fspath(result or target)
        if not os.path.exists(path) or os.path.getsize(path) <= 0:
            return None
        print(
            f"[Video Preview:{session_key}] thumbnail preservado "
            f"bytes={os.path.getsize(path)}"
        )
        return path
    except Exception as error:
        print(
            f"[Video Preview:{session_key}] thumbnail indisponivel: "
            f"{type(error).__name__}: {error}"
        )
        return None


def register(worker, session_key="primary"):
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    # Captura o downloader cru ANTES do media_transport_hardening envolver
    # download_media com validacao de tamanho do arquivo principal.
    raw_download_media = worker.client.download_media
    previous_download_media = worker.client.download_media

    async def preview_aware_download_media(message, *args, **kwargs):
        # Chamadas explicitas de thumbnail passam direto; nunca comparar thumb
        # com o tamanho total do documento.
        if kwargs.get("thumb") is not None:
            return await raw_download_media(message, *args, **kwargs)

        path = await previous_download_media(message, *args, **kwargs)
        if path and _is_video(message):
            try:
                directory = os.path.dirname(os.path.abspath(os.fspath(path)))
                thumb_path = await _download_preview(
                    raw_download_media,
                    message,
                    directory,
                    session_key,
                )
                if thumb_path:
                    _remember(path, thumb_path)
            except Exception as error:
                print(
                    f"[Video Preview:{session_key}] cache de thumb falhou: "
                    f"{type(error).__name__}: {error}"
                )
        return path

    worker.client.download_media = preview_aware_download_media

    # Sessao humana: o wrapper session_worker recebe a midia Telegram crua.
    # Baixamos a thumb antes do reupload e passamos thumb= ate o send_file real.
    previous_session_send_file = worker.client.send_file

    async def preview_session_send_file(entity, file, *args, **kwargs):
        if isinstance(file, (list, tuple)) or not _is_video(file) or kwargs.get("thumb"):
            return await previous_session_send_file(entity, file, *args, **kwargs)

        temp_dir = tempfile.mkdtemp(prefix=f"video_preview_{session_key}_")
        try:
            thumb_path = await _download_preview(
                raw_download_media,
                file,
                temp_dir,
                session_key,
            )
            if thumb_path:
                kwargs["thumb"] = thumb_path
                kwargs.setdefault("force_document", False)
                kwargs["supports_streaming"] = True
            result = await previous_session_send_file(entity, file, *args, **kwargs)
            if thumb_path:
                print(
                    f"[Video Preview:{session_key}] SESSION preview enviado "
                    f"msg={getattr(result, 'id', None)}"
                )
            return result
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    worker.client.send_file = preview_session_send_file

    publisher = worker.button_publisher
    previous_publisher_send_file = publisher.send_file

    async def preview_publisher_send_file(
        destination_chat_id,
        file_path,
        caption,
        entities,
        automation,
    ):
        thumb_path = _lookup(file_path)
        token = _BOT_THUMB.set(thumb_path)
        try:
            result = await previous_publisher_send_file(
                destination_chat_id,
                file_path,
                caption,
                entities,
                automation,
            )
            if thumb_path:
                print(
                    f"[Video Preview:{session_key}] BOT preview enviado "
                    f"msg={getattr(result, 'id', None)}"
                )
            return result
        finally:
            _BOT_THUMB.reset(token)
            try:
                _THUMBS_BY_MEDIA.pop(os.path.abspath(os.fspath(file_path)), None)
            except Exception:
                pass

    publisher.send_file = preview_publisher_send_file

    # O publisher pode ter varios clientes bot. Injeta thumb no upload real.
    for bot_key, bot in publisher.bots.items():
        client = bot.get("client")
        if client is None:
            continue
        previous_bot_send_file = client.send_file

        async def preview_bot_send_file(
            entity,
            file,
            *args,
            __previous=previous_bot_send_file,
            __bot_key=bot_key,
            **kwargs,
        ):
            thumb_path = _BOT_THUMB.get()
            if thumb_path and os.path.exists(thumb_path) and not kwargs.get("thumb"):
                kwargs["thumb"] = thumb_path
                kwargs.setdefault("force_document", False)
                kwargs["supports_streaming"] = True
                print(
                    f"[Video Preview:{session_key}] injetando thumbnail bot={__bot_key}"
                )
            return await __previous(entity, file, *args, **kwargs)

        client.send_file = preview_bot_send_file

    print(
        f"[Video Preview:{session_key}] ativo: thumbnail original preservado em BOT e SESSION"
    )
