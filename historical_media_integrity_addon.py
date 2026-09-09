"""Integridade de mídia para publicações via bot.

O histórico programado baixa a mídia usando a sessão humana e republica pelo bot.
Ao reupar um vídeo a partir de um arquivo temporário, o Telegram/Telethon pode
recalcular atributos do documento. Esta camada permite que o roteador injete os
atributos originais do documento (dimensões/duração/mime) no upload do bot.

Também valida a legenda retornada pelo Telegram. Se uma publicação com mídia
for criada sem a legenda esperada, a camada corrige imediatamente via edit,
em vez de aceitar um falso "published" sem texto.
"""

import contextvars


_MEDIA_METADATA = contextvars.ContextVar(
    "historical_bot_media_metadata",
    default=None,
)


def _expected_caption(value):
    return str(value or "").strip()


def _actual_caption(message):
    return str(getattr(message, "message", "") or "").strip()


def register(worker, session_key="primary"):
    publisher = worker.button_publisher
    original_publisher_send_file = publisher.send_file

    # Os clientes dos bots já foram criados durante o import/configuração do
    # publisher. Interceptamos o upload real para injetar metadata apenas quando
    # o roteador explicitamente fornecer metadata da mensagem de origem.
    for bot_key, bot in publisher.bots.items():
        client = bot.get("client")
        if client is None:
            continue

        original_client_send_file = client.send_file

        async def metadata_aware_client_send_file(
            entity,
            file,
            *args,
            __original=original_client_send_file,
            __bot_key=bot_key,
            **kwargs,
        ):
            metadata = _MEDIA_METADATA.get()
            if isinstance(metadata, dict):
                attributes = metadata.get("attributes")
                mime_type = metadata.get("mime_type")
                if attributes:
                    kwargs["attributes"] = attributes
                if mime_type:
                    kwargs["mime_type"] = mime_type
                if metadata.get("is_video"):
                    kwargs["supports_streaming"] = True
                print(
                    f"[Media Integrity:{session_key}] metadata original aplicada "
                    f"bot={__bot_key} attrs={len(attributes or [])} mime={mime_type or '-'}"
                )
            return await __original(entity, file, *args, **kwargs)

        client.send_file = metadata_aware_client_send_file

    async def integrity_send_file(
        destination_chat_id,
        file_path,
        caption,
        entities,
        automation,
    ):
        metadata = None
        if isinstance(automation, dict):
            candidate = automation.get("_source_media_metadata")
            if isinstance(candidate, dict):
                metadata = candidate

        token = _MEDIA_METADATA.set(metadata)
        try:
            result = await original_publisher_send_file(
                destination_chat_id,
                file_path,
                caption,
                entities,
                automation,
            )
        finally:
            _MEDIA_METADATA.reset(token)

        expected = _expected_caption(caption)
        actual = _actual_caption(result)

        if expected and not actual:
            print(
                f"[Media Integrity:{session_key}] caption ausente apos upload; "
                f"corrigindo dest={destination_chat_id} msg={getattr(result, 'id', None)}"
            )
            edited = await publisher.edit_message(
                destination_chat_id,
                getattr(result, "id", None),
                caption,
                entities or [],
                automation,
            )
            if edited is not None:
                result = edited

            if not _actual_caption(result):
                raise RuntimeError(
                    "Telegram confirmou mídia, mas a legenda continuou ausente após correção"
                )

        return result

    publisher.send_file = integrity_send_file
    print(
        f"[Media Integrity:{session_key}] ativo: metadata original + garantia de caption"
    )
