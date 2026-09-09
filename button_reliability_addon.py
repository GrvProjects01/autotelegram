"""Camada global de confiabilidade para publicações com botões.

Objetivos:
- tentar reconectar e repetir uma vez quando o bot selecionado falhar;
- cobrir texto puro que cair no fallback humano sem botão;
- manter os addons de mídia/álbum existentes como segunda camada.

Não altera a origem dos botões: o worker continua usando `automation['buttons']`.
"""

import asyncio
import contextvars


_pending_text_cta = contextvars.ContextVar("pending_text_cta", default=None)


def _has_buttons(worker, automation):
    try:
        return bool(worker.automation_has_inline_buttons(automation))
    except Exception:
        return False


async def _reconnect_selected_bot(worker, automation, session_key):
    publisher = worker.button_publisher
    try:
        key = publisher._automation_bot_key(automation)
        if not key and len(publisher.bots) == 1:
            key = next(iter(publisher.bots.keys()))
        bot = publisher.bots.get(key) if key else None
        if not bot:
            return False

        client = bot["client"]
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass

        await asyncio.sleep(0.25)
        await client.start(bot_token=bot["token"])
        me = await client.get_me()

        expected = str(bot.get("token_bot_id") or "").strip()
        actual = str(me.id)
        if expected and expected != actual:
            raise RuntimeError(
                f"bot key={key} autenticou como {actual}, esperado={expected}"
            )

        bot["connected"] = True
        bot["telegram_id"] = actual
        bot["username"] = getattr(me, "username", None)
        bot.get("entity_cache", {}).clear()

        print(
            f"[Buttons Reliability:{session_key}] bot reconectado key={key}"
        )
        return True
    except Exception as error:
        print(
            f"[Buttons Reliability:{session_key}] reconexão falhou:",
            type(error).__name__,
            str(error),
        )
        return False


def register(worker, session_key="primary", cta_text="👇"):
    publisher = worker.button_publisher

    original_bot_send_text = publisher.send_text
    original_bot_send_file = publisher.send_file
    original_try_publish = worker.try_publish_with_inline_buttons
    original_human_send_message = worker.client.send_message

    async def reliable_bot_send_text(destination_chat_id, text, entities, automation):
        try:
            return await original_bot_send_text(
                destination_chat_id, text, entities, automation
            )
        except Exception as first_error:
            print(
                f"[Buttons Reliability:{session_key}] send_text falhou; retry:",
                type(first_error).__name__,
                str(first_error),
            )
            if await _reconnect_selected_bot(worker, automation, session_key):
                return await original_bot_send_text(
                    destination_chat_id, text, entities, automation
                )
            raise

    async def reliable_bot_send_file(destination_chat_id, file_path, caption, entities, automation):
        try:
            return await original_bot_send_file(
                destination_chat_id, file_path, caption, entities, automation
            )
        except Exception as first_error:
            print(
                f"[Buttons Reliability:{session_key}] send_file falhou; retry:",
                type(first_error).__name__,
                str(first_error),
            )
            if await _reconnect_selected_bot(worker, automation, session_key):
                return await original_bot_send_file(
                    destination_chat_id, file_path, caption, entities, automation
                )
            raise

    # Primeiro fortalecemos todo envio pelo bot (inclusive rotação e addons).
    publisher.send_text = reliable_bot_send_text
    publisher.send_file = reliable_bot_send_file

    async def try_publish_with_text_fallback(*args, **kwargs):
        automation = kwargs.get("automation")
        message = kwargs.get("message")
        destination = kwargs.get("destination")

        result = await original_try_publish(*args, **kwargs)
        if result is not None:
            _pending_text_cta.set(None)
            return result

        # Mídia é tratada pelos addons próprios. Aqui cobrimos somente texto puro.
        if (
            automation
            and message is not None
            and not getattr(message, "media", None)
            and destination
            and _has_buttons(worker, automation)
        ):
            _pending_text_cta.set({
                "automation": automation,
                "destination": destination,
                "source_message_id": getattr(message, "id", None),
            })
            print(
                f"[Buttons Reliability:{session_key}] texto sem botão no fluxo principal; "
                "CTA de recuperação armado"
            )
        else:
            _pending_text_cta.set(None)

        return result

    async def human_send_message_with_cta(entity, message, *args, **kwargs):
        result = await original_human_send_message(entity, message, *args, **kwargs)

        pending = _pending_text_cta.get()
        if not pending:
            return result

        _pending_text_cta.set(None)
        try:
            await publisher.send_text(
                destination_chat_id=pending["destination"],
                text=cta_text,
                entities=[],
                automation=pending["automation"],
            )
            print(
                f"[Buttons Reliability:{session_key}] CTA texto enviado "
                f"source_message_id={pending.get('source_message_id')}"
            )
        except Exception as error:
            print(
                f"[Buttons Reliability:{session_key}] CTA texto falhou definitivamente:",
                type(error).__name__,
                str(error),
            )

        return result

    worker.try_publish_with_inline_buttons = try_publish_with_text_fallback
    worker.client.send_message = human_send_message_with_cta

    print(f"[Buttons Reliability:{session_key}] camada global registrada")
