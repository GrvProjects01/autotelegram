"""CTA de botões separado para álbuns do Telegram.

Telegram não aceita inline keyboard diretamente em media groups. O worker base
preserva o álbum sem botão; este addon envia uma mensagem curta logo depois,
usando o mesmo bot publicador/rotação da automação.
"""

import asyncio
import json
import os
import tempfile

from telethon import events


_STATE_LOCK = asyncio.Lock()


def _state_file(session_key):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.getenv(
        "TELEGRAM_ALBUM_BUTTON_STATE_FILE",
        os.path.join(base_dir, f"album_button_cta_state_{session_key}.json"),
    )


def _load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(path, state):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="album_cta_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _cta_text(automation):
    value = str(
        automation.get("album_buttons_text")
        or automation.get("buttons_cta_text")
        or "👇"
    ).strip()
    return value or "👇"


def register(worker, session_key):
    state_path = _state_file(session_key)

    @worker.client.on(events.Album())
    async def album_button_cta_handler(event):
        source_id = event.chat_id
        if source_id is None:
            return

        media_messages = [msg for msg in event.messages if getattr(msg, "media", None)]
        if not media_messages:
            return

        matches = await worker.get_matching_automations(source_id)
        if not matches:
            return

        caption_message = media_messages[0]
        for message in media_messages:
            if (getattr(message, "message", "") or "").strip():
                caption_message = message
                break

        grouped_id = str(getattr(event, "grouped_id", "") or "")

        # O handler original foi registrado antes deste addon. Ainda assim,
        # aguardamos um pouco e confirmamos message-link para não soltar CTA
        # quando o álbum falhou.
        await asyncio.sleep(0.8)

        for automation in matches:
            if not worker.automation_has_inline_buttons(automation):
                continue

            automation_id = str(automation.get("id") or "").strip()
            destination = automation.get("destination_chat_id")
            if not automation_id or not destination:
                continue

            dedupe_key = f"{source_id}:{grouped_id}:{automation_id}"

            async with _STATE_LOCK:
                state = _load_state(state_path)
                if state.get(dedupe_key):
                    continue

            links = await worker.find_message_links(
                source_chat_id=source_id,
                source_message_id=caption_message.id,
                automation_id=automation_id,
            )
            if not links:
                print(
                    f"[Album Buttons:{session_key}] álbum sem link persistido; "
                    f"CTA adiado automação={automation_id}"
                )
                continue

            if not worker.button_publisher.available:
                print(
                    f"[Album Buttons:{session_key}] bot publicador indisponível; "
                    f"CTA não enviado automação={automation_id}"
                )
                continue

            try:
                sent = await worker.button_publisher.send_text(
                    destination_chat_id=destination,
                    text=_cta_text(automation),
                    entities=[],
                    automation=automation,
                )

                async with _STATE_LOCK:
                    state = _load_state(state_path)
                    state[dedupe_key] = {
                        "destination_chat_id": str(destination),
                        "destination_message_id": int(sent.id),
                    }
                    _save_state(state_path, state)

                await worker.remember_self_published(destination, sent.id)
                print(
                    f"[Album Buttons:{session_key}] CTA enviado "
                    f"automação={automation_id} msg={sent.id}"
                )
            except Exception as error:
                print(
                    f"[Album Buttons:{session_key}] falha no CTA "
                    f"automação={automation_id}:",
                    type(error).__name__,
                    str(error),
                )

    return album_button_cta_handler
