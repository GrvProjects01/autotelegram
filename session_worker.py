"""Executa o worker existente isolado por conta/sessão Telegram.

Cada processo recebe TELEGRAM_SESSION_KEY e TELEGRAM_SESSION_NAME diferentes.
O wrapper preserva o worker.py e filtra as automações para impedir que duas
contas processem a mesma tarefa.

Compatibilidade:
- TELEGRAM_SESSION_IS_DEFAULT=1 faz a sessão atual assumir automações antigas
  que ainda não possuem `telegram_session_key`.
- sessões secundárias só processam automações explicitamente vinculadas à sua chave.
"""

import asyncio
import json
import os
import re
import shutil
import tempfile

from dotenv import load_dotenv
from telethon import Button

load_dotenv()

SESSION_KEY = str(os.getenv("TELEGRAM_SESSION_KEY", "primary")).strip().lower()
SESSION_KEY = re.sub(r"[^a-z0-9_.-]+", "_", SESSION_KEY).strip("_.-") or "primary"
IS_DEFAULT = str(os.getenv("TELEGRAM_SESSION_IS_DEFAULT", "0")).strip() == "1"
FORCE_MEDIA_REUPLOAD = str(
    os.getenv("TELEGRAM_FORCE_MEDIA_REUPLOAD", "1")
).strip().lower() not in {"0", "false", "no", "off"}

# Importar depois de carregar o ambiente é intencional: worker.py lê as variáveis
# de sessão no import e cria o TelegramClient correspondente.
import worker  # noqa: E402

_original_load_automations = worker.load_automations
_original_lovable_request = worker.lovable_request
_original_process_rich_text = worker.process_rich_text
_original_client_send_file = worker.client.send_file
_original_bot_send_text = worker.button_publisher.send_text
_original_bot_send_file = worker.button_publisher.send_file
_original_bot_edit_message = worker.button_publisher.edit_message

BUTTON_ROTATION_STATE_FILE = os.getenv(
    "TELEGRAM_BUTTON_ROTATION_STATE_FILE",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"button_rotation_state_{SESSION_KEY}.json",
    ),
)
BUTTON_ROTATION_LOCK = asyncio.Lock()


def automation_session_key(automation):
    if not isinstance(automation, dict):
        return ""

    value = (
        automation.get("telegram_session_key")
        or automation.get("source_session_key")
        or automation.get("telegram_account_key")
        or ""
    )

    return str(value).strip().lower()


async def load_session_automations(force_refresh=False):
    automations = await _original_load_automations(force_refresh=force_refresh)
    filtered = []

    for automation in automations:
        configured_key = automation_session_key(automation)

        if configured_key:
            if configured_key == SESSION_KEY:
                filtered.append(automation)
            continue

        if IS_DEFAULT:
            filtered.append(automation)

    if force_refresh:
        print(
            f"[Session:{SESSION_KEY}] automações desta sessão: "
            f"{len(filtered)}/{len(automations)}"
        )

    return filtered


def _utf16_slice(text, offset, length):
    """Extrai o trecho apontado por uma entity do Telegram (offset UTF-16)."""
    if not text or length <= 0:
        return ""

    raw = text.encode("utf-16-le")
    start = max(0, int(offset)) * 2
    end = max(start, int(offset + length)) * 2
    return raw[start:end].decode("utf-16-le", errors="ignore")


def _link_from_replaced_visible_text(value):
    """Infere um destino seguro quando o texto substituído virou URL/@username."""
    value = str(value or "").strip()

    if re.match(r"^https?://", value, flags=re.IGNORECASE):
        return value

    if re.match(r"^(?:https?://)?t\.me/[A-Za-z0-9_+\-/]+$", value, flags=re.IGNORECASE):
        if value.lower().startswith(("http://", "https://")):
            return value
        return "https://" + value

    if re.match(r"^@[A-Za-z0-9_]{5,}$", value):
        return "https://t.me/" + value[1:]

    return None


def session_process_rich_text(text, entities, automation):
    """Evita reaproveitar hyperlink oculto antigo quando o texto ligado foi trocado."""
    original_entities = entities or []
    replacements = worker.get_active_replacements(automation)
    stale_urls = set()
    inferred_urls = {}

    if replacements:
        print(
            f"[Replace:{SESSION_KEY}] regras ativas: {len(replacements)}"
        )

    for entity in original_entities:
        if not isinstance(entity, worker.MessageEntityTextUrl):
            continue

        old_url = str(getattr(entity, "url", "") or "")
        if not old_url:
            continue

        replaced_url = worker.replace_value(old_url, replacements)
        if replaced_url != old_url:
            continue

        linked_text = _utf16_slice(
            text or "",
            getattr(entity, "offset", 0),
            getattr(entity, "length", 0),
        )
        replaced_linked_text = worker.replace_value(linked_text, replacements)

        if replaced_linked_text == linked_text:
            continue

        inferred_url = _link_from_replaced_visible_text(replaced_linked_text)
        if inferred_url:
            inferred_urls[old_url] = inferred_url
        else:
            stale_urls.add(old_url)

    processed_text, processed_entities = _original_process_rich_text(
        text,
        entities,
        automation,
    )

    if processed_text is None:
        return processed_text, processed_entities

    if replacements and processed_text == (text or ""):
        print(
            f"[Replace:{SESSION_KEY}] nenhuma regra alterou o texto visível desta mensagem"
        )

    if not processed_entities:
        return processed_text, processed_entities

    sanitized_entities = []
    removed = 0
    rewritten = 0

    for entity in processed_entities:
        if isinstance(entity, worker.MessageEntityTextUrl):
            current_url = str(getattr(entity, "url", "") or "")

            if current_url in inferred_urls:
                entity.url = inferred_urls[current_url]
                rewritten += 1
            elif current_url in stale_urls:
                removed += 1
                continue

        sanitized_entities.append(entity)

    if removed or rewritten:
        print(
            f"[Links:{SESSION_KEY}] hyperlinks ocultos sanitizados: "
            f"removidos={removed} reescritos={rewritten}"
        )

    return processed_text, sanitized_entities


def _is_raw_telegram_media(value):
    if value is None:
        return False
    cls = type(value)
    return (
        cls.__module__.startswith("telethon.tl.types")
        and cls.__name__.startswith("MessageMedia")
    )


async def _download_media_checked(media, temp_dir, index=0):
    path = await worker.client.download_media(media, file=temp_dir)
    if not path:
        raise RuntimeError("download_media retornou caminho vazio")

    path = os.fspath(path)
    if not os.path.exists(path):
        raise RuntimeError(f"arquivo baixado não existe: {path}")

    size = os.path.getsize(path)
    if size <= 0:
        raise RuntimeError(f"arquivo baixado está vazio: {path}")

    print(
        f"[Media:{SESSION_KEY}] download completo #{index + 1}: "
        f"{size} bytes"
    )
    return path


async def session_send_file(entity, file, *args, **kwargs):
    """Força download + reupload para mídia crua do Telegram."""
    if not FORCE_MEDIA_REUPLOAD:
        return await _original_client_send_file(entity, file, *args, **kwargs)

    is_album = isinstance(file, (list, tuple))
    items = list(file) if is_album else [file]

    if not items or not any(_is_raw_telegram_media(item) for item in items):
        return await _original_client_send_file(entity, file, *args, **kwargs)

    temp_dir = tempfile.mkdtemp(prefix=f"tg_media_{SESSION_KEY}_")
    prepared = []

    try:
        for index, item in enumerate(items):
            if _is_raw_telegram_media(item):
                prepared.append(
                    await _download_media_checked(item, temp_dir, index=index)
                )
            else:
                prepared.append(item)

        payload = prepared if is_album else prepared[0]
        print(
            f"[Media:{SESSION_KEY}] reupload robusto: "
            f"{len(prepared)} arquivo(s)"
        )
        return await _original_client_send_file(
            entity,
            payload,
            *args,
            **kwargs,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ============================================================
# ROTAÇÃO DE BOTÕES
# ============================================================

def _button_mode(automation):
    if not isinstance(automation, dict):
        return "fixed"

    value = (
        automation.get("button_mode")
        or automation.get("buttons_mode")
        or automation.get("button_rotation_mode")
        or "fixed"
    )
    value = str(value).strip().lower()

    if value in {"rotation", "rotate", "rotating", "rotacao", "rotação"}:
        return "rotation"
    return "fixed"


def _button_rotation_size(automation):
    try:
        value = int(
            automation.get("button_rotation_size")
            or automation.get("buttons_per_post")
            or 2
        )
    except (TypeError, ValueError):
        value = 2
    return max(1, min(value, 8))


def _rotation_automation_key(automation):
    automation_id = str(automation.get("id") or "").strip()
    return automation_id or "unknown"


def _load_rotation_state():
    try:
        with open(BUTTON_ROTATION_STATE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as error:
        print(
            f"[Buttons Rotation:{SESSION_KEY}] estado inválido; reiniciando:",
            type(error).__name__,
            str(error),
        )
        return {}


def _save_rotation_state(state):
    directory = os.path.dirname(os.path.abspath(BUTTON_ROTATION_STATE_FILE))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix="button_rotation_",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, BUTTON_ROTATION_STATE_FILE)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _keyboard_from_buttons(buttons):
    rows = {}
    for button in buttons:
        row = int(button.get("row", 0))
        rows.setdefault(row, []).append(
            Button.url(
                button["text"],
                button["url"],
                style=button.get("style"),
            )
        )
    return [rows[row] for row in sorted(rows)]


def _rotation_batch(automation, cursor):
    buttons = worker.button_publisher.normalize_buttons(automation)
    if not buttons:
        return [], 0

    size = _button_rotation_size(automation)
    groups = [buttons[index:index + size] for index in range(0, len(buttons), size)]
    if not groups:
        return [], 0

    group_index = int(cursor or 0) % len(groups)
    next_cursor = (group_index + 1) % len(groups)
    return groups[group_index], next_cursor


async def _rotation_keyboard_for_send(automation):
    """Retorna teclado + cursor seguinte sem persistir antes do envio."""
    async with BUTTON_ROTATION_LOCK:
        state = _load_rotation_state()
        key = _rotation_automation_key(automation)
        cursor = int(state.get(key, 0) or 0)
        selected, next_cursor = _rotation_batch(automation, cursor)
        return _keyboard_from_buttons(selected), key, next_cursor, selected


async def _commit_rotation(key, next_cursor):
    async with BUTTON_ROTATION_LOCK:
        state = _load_rotation_state()
        state[key] = int(next_cursor)
        _save_rotation_state(state)


async def rotation_send_text(destination_chat_id, text, entities, automation):
    if _button_mode(automation) != "rotation":
        return await _original_bot_send_text(
            destination_chat_id, text, entities, automation
        )

    bot = worker.button_publisher._get_bot(automation)
    destination = await worker.button_publisher._resolve_destination(
        bot, destination_chat_id
    )
    keyboard, key, next_cursor, selected = await _rotation_keyboard_for_send(automation)

    result = await bot["client"].send_message(
        destination,
        text,
        formatting_entities=entities or [],
        buttons=keyboard,
    )

    await _commit_rotation(key, next_cursor)
    worker.button_publisher._remember_destination_bot(destination_chat_id, bot["key"])
    print(
        f"[Buttons Rotation:{SESSION_KEY}] automação={key} "
        f"enviados={len(selected)} próximo_grupo={next_cursor}"
    )
    return result


async def rotation_send_file(destination_chat_id, file_path, caption, entities, automation):
    if _button_mode(automation) != "rotation":
        return await _original_bot_send_file(
            destination_chat_id, file_path, caption, entities, automation
        )

    bot = worker.button_publisher._get_bot(automation)
    destination = await worker.button_publisher._resolve_destination(
        bot, destination_chat_id
    )
    keyboard, key, next_cursor, selected = await _rotation_keyboard_for_send(automation)

    result = await bot["client"].send_file(
        destination,
        file_path,
        caption=caption or "",
        formatting_entities=entities or [],
        buttons=keyboard,
        supports_streaming=True,
    )

    await _commit_rotation(key, next_cursor)
    worker.button_publisher._remember_destination_bot(destination_chat_id, bot["key"])
    print(
        f"[Buttons Rotation:{SESSION_KEY}] automação={key} "
        f"enviados={len(selected)} próximo_grupo={next_cursor}"
    )
    return result


async def rotation_edit_message(
    destination_chat_id,
    destination_message_id,
    text,
    entities,
    automation,
):
    if _button_mode(automation) != "rotation":
        return await _original_bot_edit_message(
            destination_chat_id,
            destination_message_id,
            text,
            entities,
            automation,
        )

    # Em modo rotação, editar o texto não deve consumir um novo grupo de botões
    # nem substituir a combinação que já estava anexada ao post original.
    bot = worker.button_publisher._get_bot(automation)
    destination = await worker.button_publisher._resolve_destination(
        bot, destination_chat_id
    )
    return await bot["client"].edit_message(
        destination,
        int(destination_message_id),
        text,
        formatting_entities=entities or [],
    )


async def session_lovable_request(path, method="GET", data=None):
    """Acrescenta identidade da sessão sem alterar endpoints existentes."""
    payload = data

    if isinstance(data, dict):
        payload = dict(data)

        if path == worker.HEARTBEAT_ENDPOINT and method == "POST":
            payload["telegram_session_key"] = SESSION_KEY
            payload["telegram_session_is_default"] = IS_DEFAULT

            try:
                publisher_bots = worker.button_publisher.public_bots()
            except Exception:
                publisher_bots = []

            payload["publisher_bots"] = publisher_bots

            print(
                f"[Session:{SESSION_KEY}] heartbeat -> bots públicos: "
                f"{len(publisher_bots)}"
            )
            if publisher_bots:
                print(
                    f"[Session:{SESSION_KEY}] bot keys: "
                    + ", ".join(str(item.get("key")) for item in publisher_bots)
                )

        elif path == worker.CHATS_SYNC_ENDPOINT and method == "POST":
            payload["telegram_session_key"] = SESSION_KEY

            chats = payload.get("chats", []) or []
            normalized_chats = []

            for chat in chats:
                if isinstance(chat, dict):
                    item = dict(chat)
                    item["telegram_session_key"] = SESSION_KEY
                    normalized_chats.append(item)
                else:
                    normalized_chats.append(chat)

            payload["chats"] = normalized_chats

            print(
                f"[Session:{SESSION_KEY}] enviando chats ao Lovable: "
                f"{len(normalized_chats)}"
            )

    return await _original_lovable_request(path, method, payload)


worker.load_automations = load_session_automations
worker.lovable_request = session_lovable_request
worker.process_rich_text = session_process_rich_text
worker.client.send_file = session_send_file
worker.button_publisher.send_text = rotation_send_text
worker.button_publisher.send_file = rotation_send_file
worker.button_publisher.edit_message = rotation_edit_message

base_worker_id = str(worker.WORKER_ID or "telegram-main")
if not base_worker_id.endswith(f"-{SESSION_KEY}"):
    worker.WORKER_ID = f"{base_worker_id}-{SESSION_KEY}"

recovery_dir = os.path.dirname(os.path.abspath(worker.SOURCE_RECOVERY_STATE_FILE))
worker.SOURCE_RECOVERY_STATE_FILE = os.getenv(
    "TELEGRAM_SOURCE_RECOVERY_STATE_FILE",
    os.path.join(recovery_dir, f"source_recovery_state_{SESSION_KEY}.json")
)


async def main():
    print("=================================")
    print(" TELEGRAM MULTI-SESSION WRAPPER")
    print(f" SESSION KEY: {SESSION_KEY}")
    print(f" SESSION NAME: {worker.SESSION_NAME}")
    print(f" DEFAULT: {IS_DEFAULT}")
    print(f" FORCE MEDIA REUPLOAD: {FORCE_MEDIA_REUPLOAD}")
    print(f" BUTTON ROTATION STATE: {BUTTON_ROTATION_STATE_FILE}")
    print("=================================")
    await worker.main()


if __name__ == "__main__":
    asyncio.run(main())
