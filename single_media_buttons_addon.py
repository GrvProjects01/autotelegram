"""Fallback de botões para publicações com uma única mídia.

Quando o fluxo principal tenta publicar mídia + inline keyboard pelo bot e falha,
o worker atual faz fallback para a sessão humana e a publicação chega sem botão.
Este addon preserva esse fallback de entrega e, após o envio normal da mídia,
publica um CTA separado com os mesmos botões/bot/rotação da automação.
"""

import contextvars


_pending_single_media_cta = contextvars.ContextVar(
    "pending_single_media_cta",
    default=None,
)


def _automation_has_buttons(worker, automation):
    try:
        return bool(worker.automation_has_inline_buttons(automation))
    except Exception:
        return False


def register(worker, session_key="primary", cta_text="👇"):
    original_try_publish = worker.try_publish_with_inline_buttons
    original_send_file = worker.client.send_file

    async def try_publish_with_single_media_fallback(*args, **kwargs):
        automation = kwargs.get("automation")
        message = kwargs.get("message")
        destination = kwargs.get("destination")
        preserve_media = kwargs.get("preserve_media", True)

        result = await original_try_publish(*args, **kwargs)

        # Sucesso pelo bot: não há fallback a fazer.
        if result is not None:
            _pending_single_media_cta.set(None)
            return result

        # Só armamos o CTA para publicação individual com mídia e botões.
        # Álbuns não passam por esta função e são tratados pelo addon próprio.
        if (
            automation
            and message is not None
            and getattr(message, "media", None)
            and preserve_media
            and destination
            and _automation_has_buttons(worker, automation)
        ):
            _pending_single_media_cta.set({
                "automation": automation,
                "destination": destination,
                "source_message_id": getattr(message, "id", None),
            })
            print(
                f"[Single Media Buttons:{session_key}] envio com botão não concluiu; "
                "CTA de fallback armado"
            )
        else:
            _pending_single_media_cta.set(None)

        return result

    async def send_file_with_cta_fallback(entity, file, *args, **kwargs):
        # Se o envio normal falhar, mantenha o contexto para uma eventual
        # segunda tentativa do fluxo protegido/download+reupload.
        result = await original_send_file(entity, file, *args, **kwargs)

        pending = _pending_single_media_cta.get()
        if not pending:
            return result

        # Limpa antes do CTA para impedir duplicação se qualquer chamada interna
        # de envio ocorrer durante a publicação do botão.
        _pending_single_media_cta.set(None)

        try:
            await worker.button_publisher.send_text(
                destination_chat_id=pending["destination"],
                text=cta_text,
                entities=[],
                automation=pending["automation"],
            )
            print(
                f"[Single Media Buttons:{session_key}] CTA enviado "
                f"source_message_id={pending.get('source_message_id')}"
            )
        except Exception as error:
            print(
                f"[Single Media Buttons:{session_key}] falha no CTA de fallback:",
                type(error).__name__,
                str(error),
            )

        return result

    worker.try_publish_with_inline_buttons = try_publish_with_single_media_fallback
    worker.client.send_file = send_file_with_cta_fallback

    print(
        f"[Single Media Buttons:{session_key}] fallback registrado"
    )
