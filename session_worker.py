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
import os
import re
import shutil
import tempfile

from dotenv import load_dotenv

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

    # Diagnóstico seguro: mostra somente quantas regras estão ativas.
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
    """Força download + reupload para mídia crua do Telegram.

    O worker antigo reaproveitava MessageMedia diretamente. Isso é rápido, mas pode
    reproduzir mídia incompleta/estranha em alguns chats. Para caminhos locais,
    streams e arquivos já baixados, o comportamento original permanece intacto.
    """
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
    print("=================================")
    await worker.main()


if __name__ == "__main__":
    asyncio.run(main())
