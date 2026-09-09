"""Hardening global de transporte de mídia para bots e sessões humanas.

Objetivos:
- usar armazenamento temporário em disco (EBS) em vez do /tmp tmpfs por padrão;
- preservar metadata de vídeos quando a sessão humana baixa e reupa mídia Telegram;
- impedir uso de GetDialogsRequest por contas bot ao resolver destinos;
- validar a mensagem realmente persistida no Telegram após upload;
- corrigir caption somente após refetch real, evitando edits falsos;
- registrar diferenças de metadata sem transformar sucesso em duplicação silenciosa.

Esta camada é deliberadamente aditiva e não altera contratos Lovable/Supabase.
"""

import os
import re
import tempfile
import types


_REGISTERED = False


def _temp_root():
    configured = str(os.getenv("TELEGRAM_MEDIA_TEMP_DIR") or "").strip()
    if configured:
        root = os.path.abspath(os.path.expanduser(configured))
    else:
        # session_worker.py vive em .../autotelegram/autotelegram; o diretório
        # irmão ../tmp fica no EBS da instância e não no tmpfs de /tmp.
        root = os.path.abspath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tmp", "media")
        )
    os.makedirs(root, mode=0o700, exist_ok=True)
    return root


def _text(value):
    return str(value or "")


def _document_from_media(value):
    if value is None:
        return None
    document = getattr(value, "document", None)
    if document is not None:
        return document
    media = getattr(value, "media", None)
    return getattr(media, "document", None) if media is not None else None


def _metadata_from_media(value):
    document = _document_from_media(value)
    if document is None:
        return None

    attrs = list(getattr(document, "attributes", None) or [])
    mime = str(getattr(document, "mime_type", "") or "").strip() or None
    result = {
        "attributes": attrs,
        "mime_type": mime,
        "is_video": bool(mime and mime.lower().startswith("video/")),
        "size": int(getattr(document, "size", 0) or 0) or None,
        "width": None,
        "height": None,
        "duration": None,
    }
    for attr in attrs:
        if type(attr).__name__ == "DocumentAttributeVideo":
            result["is_video"] = True
            result["width"] = getattr(attr, "w", None)
            result["height"] = getattr(attr, "h", None)
            result["duration"] = getattr(attr, "duration", None)
            break
    return result


def _metadata_summary(meta):
    if not isinstance(meta, dict):
        return "none"
    return (
        f"mime={meta.get('mime_type') or '-'} "
        f"w={meta.get('width') or '-'} h={meta.get('height') or '-'} "
        f"duration={meta.get('duration') or '-'} size={meta.get('size') or '-'}"
    )


def _metadata_diff(expected, actual):
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return []
    mismatches = []
    for key in ("mime_type", "width", "height"):
        left = expected.get(key)
        right = actual.get(key)
        if left not in (None, "") and right not in (None, "") and left != right:
            mismatches.append(f"{key}:{left}!={right}")
    # Telegram pode representar duração como int/float; tolerância curta evita falso positivo.
    left_duration = expected.get("duration")
    right_duration = actual.get("duration")
    if left_duration not in (None, "") and right_duration not in (None, ""):
        try:
            if abs(float(left_duration) - float(right_duration)) > 1.0:
                mismatches.append(f"duration:{left_duration}!={right_duration}")
        except (TypeError, ValueError):
            if left_duration != right_duration:
                mismatches.append(f"duration:{left_duration}!={right_duration}")
    return mismatches


def _normalize_bot_destination(value):
    text = str(value or "").strip()
    if text.startswith("https://t.me/") or text.startswith("http://t.me/"):
        text = text.split("t.me/", 1)[1].split("?", 1)[0].strip("/")
    if text.startswith("t.me/"):
        text = text[5:].split("?", 1)[0].strip("/")
    if text.startswith("@"):
        text = text[1:]
    return text


async def _fetch_single(client, destination, message_id):
    if not message_id:
        return None
    try:
        return await client.get_messages(destination, ids=int(message_id))
    except Exception:
        return None


def register(worker, session_key="primary"):
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    media_root = _temp_root()
    # Todos os tempfile.mkdtemp/mkstemp sem dir explícito deste processo passam
    # a usar o EBS. Isso cobre histórico, sessão e mídia recorrente.
    tempfile.tempdir = media_root
    print(f"[Media Hardening:{session_key}] temp_dir={media_root}")

    publisher = worker.button_publisher

    # ------------------------------------------------------------------
    # 1) Resolução de destino para bots SEM iter_dialogs/GetDialogsRequest.
    # Bots não podem executar GetDialogsRequest. O cache local da sessão do bot
    # e get_input_entity continuam sendo usados; username público também funciona.
    # ------------------------------------------------------------------
    async def safe_bot_resolve(self, bot, destination_chat_id):
        destination = _normalize_bot_destination(destination_chat_id)
        cache = bot.get("entity_cache") or {}
        cached = cache.get(str(destination))
        if cached is not None:
            try:
                cache.move_to_end(str(destination))
            except Exception:
                pass
            return cached

        candidates = []
        if re.fullmatch(r"-?\d+", destination or ""):
            candidates.append(int(destination))
        candidates.append(destination)

        errors = []
        for candidate in candidates:
            try:
                entity = await bot["client"].get_input_entity(candidate)
                try:
                    self._cache_entity(bot, str(destination), entity)
                except Exception:
                    pass
                return entity
            except Exception as error:
                errors.append(type(error).__name__)

        raise ValueError(
            f"Bot '{bot['key']}' nao consegue resolver o destino {destination_chat_id} "
            f"sem GetDialogsRequest (tentativas={','.join(errors)}). "
            "Confirme que o bot participa do destino e que a sessao do bot ja recebeu "
            "uma atualizacao desse chat; para destino publico, prefira username/link t.me."
        )

    publisher._resolve_destination = types.MethodType(safe_bot_resolve, publisher)

    async def no_bot_dialog_warmup(self, bot):
        # Não chama iter_dialogs porque Telegram proíbe GetDialogsRequest para bots.
        print(
            f"[Media Hardening:{session_key}] bot={bot.get('key')} "
            "dialog warmup ignorado (Bot API restriction)"
        )

    publisher._warm_entity_cache = types.MethodType(no_bot_dialog_warmup, publisher)

    # ------------------------------------------------------------------
    # 2) Edit seguro do bot. Refetch antes de editar e usa o MESMO bot autor.
    # ------------------------------------------------------------------
    async def safe_bot_edit(
        destination_chat_id,
        destination_message_id,
        text,
        entities,
        automation,
    ):
        bot = publisher._get_bot(automation)
        destination = await publisher._resolve_destination(bot, destination_chat_id)
        current = await _fetch_single(bot["client"], destination, destination_message_id)
        if current is not None and _text(getattr(current, "message", "")) == _text(text):
            print(
                f"[Media Verify:{session_key}] caption ja correta; edit ignorado "
                f"bot={bot['key']} msg={destination_message_id}"
            )
            return current
        return await bot["client"].edit_message(
            destination,
            int(destination_message_id),
            text,
            formatting_entities=entities or [],
        )

    publisher.edit_message = safe_bot_edit

    # ------------------------------------------------------------------
    # 3) Bot uploads: forçar tratamento como mídia e verificar post real.
    # ------------------------------------------------------------------
    for bot_key, bot in publisher.bots.items():
        client = bot.get("client")
        if client is None:
            continue
        previous_client_send_file = client.send_file

        async def bot_client_send_file(
            entity,
            file,
            *args,
            __previous=previous_client_send_file,
            __bot_key=bot_key,
            **kwargs,
        ):
            kwargs.setdefault("force_document", False)
            # Se metadata original informou vídeo, streaming deve ficar ativo.
            attrs = kwargs.get("attributes") or []
            if any(type(attr).__name__ == "DocumentAttributeVideo" for attr in attrs):
                kwargs["supports_streaming"] = True
            return await __previous(entity, file, *args, **kwargs)

        client.send_file = bot_client_send_file

    previous_publisher_send_file = publisher.send_file

    async def verified_bot_send_file(
        destination_chat_id,
        file_path,
        caption,
        entities,
        automation,
    ):
        sent = await previous_publisher_send_file(
            destination_chat_id,
            file_path,
            caption,
            entities,
            automation,
        )
        bot = publisher._get_bot(automation)
        destination = await publisher._resolve_destination(bot, destination_chat_id)
        sent_id = getattr(sent, "id", None)
        persisted = await _fetch_single(bot["client"], destination, sent_id)
        actual = persisted or sent

        expected_caption = _text(caption)
        actual_caption = _text(getattr(actual, "message", ""))
        if expected_caption and actual_caption != expected_caption:
            if not actual_caption:
                actual = await safe_bot_edit(
                    destination_chat_id,
                    sent_id,
                    caption,
                    entities,
                    automation,
                )
                actual_caption = _text(getattr(actual, "message", ""))
            if actual_caption != expected_caption:
                print(
                    f"[MEDIA_INTEGRITY_MISMATCH:{session_key}] bot={bot['key']} "
                    f"msg={sent_id} caption expected_len={len(expected_caption)} "
                    f"actual_len={len(actual_caption)}"
                )

        expected_meta = automation.get("_source_media_metadata") if isinstance(automation, dict) else None
        actual_meta = _metadata_from_media(actual)
        mismatches = _metadata_diff(expected_meta, actual_meta)
        if mismatches:
            print(
                f"[MEDIA_INTEGRITY_MISMATCH:{session_key}] bot={bot['key']} msg={sent_id} "
                f"diff={';'.join(mismatches)} source=({_metadata_summary(expected_meta)}) "
                f"dest=({_metadata_summary(actual_meta)})"
            )
        else:
            print(
                f"[Media Verify:{session_key}] BOT OK bot={bot['key']} msg={sent_id} "
                f"meta=({_metadata_summary(actual_meta)})"
            )
        return actual

    publisher.send_file = verified_bot_send_file

    # ------------------------------------------------------------------
    # 4) Sessão humana: injeta metadata original ANTES do wrapper de reupload
    # capturado em session_worker.py e valida o resultado depois do envio.
    # ------------------------------------------------------------------
    previous_session_send_file = worker.client.send_file

    async def verified_session_send_file(entity, file, *args, **kwargs):
        is_album = isinstance(file, (list, tuple))
        source_meta = None
        if not is_album:
            source_meta = _metadata_from_media(file)
            if source_meta:
                if source_meta.get("attributes"):
                    kwargs.setdefault("attributes", source_meta["attributes"])
                if source_meta.get("mime_type"):
                    kwargs.setdefault("mime_type", source_meta["mime_type"])
                if source_meta.get("is_video"):
                    kwargs["supports_streaming"] = True
                kwargs.setdefault("force_document", False)

        sent = await previous_session_send_file(entity, file, *args, **kwargs)
        if isinstance(sent, (list, tuple)):
            print(
                f"[Media Verify:{session_key}] SESSION album OK itens={len(sent)}"
            )
            return sent

        sent_id = getattr(sent, "id", None)
        persisted = await _fetch_single(worker.client, entity, sent_id)
        actual = persisted or sent

        expected_caption = _text(kwargs.get("caption") or "")
        actual_caption = _text(getattr(actual, "message", ""))
        if expected_caption and actual_caption != expected_caption:
            if not actual_caption:
                try:
                    actual = await worker.client.edit_message(
                        entity,
                        int(sent_id),
                        expected_caption,
                        formatting_entities=kwargs.get("formatting_entities") or [],
                    )
                    actual_caption = _text(getattr(actual, "message", ""))
                except Exception as error:
                    print(
                        f"[MEDIA_INTEGRITY_MISMATCH:{session_key}] SESSION caption edit falhou "
                        f"msg={sent_id} {type(error).__name__}: {error}"
                    )
            if actual_caption != expected_caption:
                print(
                    f"[MEDIA_INTEGRITY_MISMATCH:{session_key}] SESSION msg={sent_id} "
                    f"caption expected_len={len(expected_caption)} actual_len={len(actual_caption)}"
                )

        actual_meta = _metadata_from_media(actual)
        mismatches = _metadata_diff(source_meta, actual_meta)
        if mismatches:
            print(
                f"[MEDIA_INTEGRITY_MISMATCH:{session_key}] SESSION msg={sent_id} "
                f"diff={';'.join(mismatches)} source=({_metadata_summary(source_meta)}) "
                f"dest=({_metadata_summary(actual_meta)})"
            )
        else:
            print(
                f"[Media Verify:{session_key}] SESSION OK msg={sent_id} "
                f"meta=({_metadata_summary(actual_meta)})"
            )
        return actual

    worker.client.send_file = verified_session_send_file

    print(
        f"[Media Hardening:{session_key}] ativo: EBS temp + bot resolver seguro + "
        "metadata/caption verification em bot e sessao"
    )
