import os
import re
import copy
import asyncio
import httpx
import time
import fcntl
import tempfile
import hashlib
import gc
import json

from collections import defaultdict, OrderedDict
from types import SimpleNamespace

from telethon import TelegramClient, events
from telethon.errors import ChatForwardsRestrictedError
from telethon.tl.types import (
    Channel,
    Chat,
    User,
    MessageEntityBold,
    MessageEntityItalic,
    MessageEntityCode,
    MessageEntityPre,
    MessageEntityTextUrl,
    MessageEntityMentionName,
    MessageEntityStrike,
    MessageEntityUnderline,
    MessageEntitySpoil,
    MessageEntityCustomEmoji,
)

from dotenv import load_dotenv


# ============================================================
# CONFIGURAÇÃO DE AMBIENTE
# ============================================================

load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]

LOVABLE_API_URL = os.environ["LOVABLE_API_URL"].rstrip("/")
WORKER_SECRET = os.environ["LOVABLE_WORKER_SECRET"]

SESSION_NAME = os.getenv(
    "TELEGRAM_SESSION_NAME",
    "telegram_main"
)

WORKER_ID = os.getenv(
    "TELEGRAM_WORKER_ID",
    "telegram-main"
)

WORKER_VERSION = "1.3.0"

# Diagnóstico refinado dos updates recebidos do Telegram.
# 1 -> imprime chats/mensagens tratados
# 2 -> imprime inclusive mensagens ignoradas
EVENT_DEBUG = os.getenv("TELEGRAM_EVENT_DEBUG", "1").strip() == "1"

HEARTBEAT_INTERVAL = 60
CHAT_SYNC_INTERVAL = 15 * 60

# ============================================================
# AJUSTES DE PRODUÇÃO / MEMÓRIA / RECOVER
# ============================================================

AUTOMATIONS_CACHE_TTL = 5
ENTITY_CACHE_MAX = 1500
MESSAGE_LINK_CACHE_MAX = 5000
MESSAGE_LINK_CACHE_TTL = 6 * 60 * 60
FINGERPRINT_CACHE_MAX = 5000
CACHE_MAINTENANCE_INTERVAL = 5 * 60
MEDIA_SEND_CONCURRENCY = 1

SOURCE_RECOVERY_INTERVAL = int(
    os.getenv("TELEGRAM_SOURCE_RECOVERY_INTERVAL", "15")
)
SOURCE_RECOVERY_MAX_MESSAGES = int(
    os.getenv("TELEGRAM_SOURCE_RECOVERY_MAX_MESSAGES", "500")
)
SOURCE_RECOVERY_STATE_FILE = os.getenv(
    "TELEGRAM_SOURCE_RECOVERY_STATE_FILE",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "source_recovery_state.json"
    )
)


# ============================================================
# CLIENT TELETHON
# ============================================================

client = TelegramClient(
    SESSION_NAME,
    API_ID,
    API_HASH
)


# ============================================================
# CACHE LRU EM MEMÓRIA
# ============================================================

ENTITY_CACHE = OrderedDict()
MESSAGE_LINK_CACHE = OrderedDict()

AUTOMATIONS_CACHE = {
    "data": [],
    "expires_at": 0.0
}

MEDIA_SEND_SEMAPHORE = asyncio.Semaphore(
    MEDIA_SEND_CONCURRENCY
)


def cache_set_lru(cache, key, value, max_size):
    if key in cache:
        cache.pop(key, None)

    cache[key] = value

    while len(cache) > max_size:
        cache.popitem(last=False)


def entity_cache_get(key):
    value = ENTITY_CACHE.get(key)

    if value is not None:
        ENTITY_CACHE.move_to_end(key)

    return value


def entity_cache_set(key, value):
    cache_set_lru(
        ENTITY_CACHE,
        key,
        value,
        ENTITY_CACHE_MAX
    )


def message_link_cache_set(key, links):
    cache_set_lru(
        MESSAGE_LINK_CACHE,
        key,
        {
            "links": links,
            "expires_at": (
                time.monotonic()
                +
                MESSAGE_LINK_CACHE_TTL
            )
        },
        MESSAGE_LINK_CACHE_MAX
    )


def message_link_cache_get(key):
    item = MESSAGE_LINK_CACHE.get(key)

    if not item:
        return []

    if item["expires_at"] <= time.monotonic():
        MESSAGE_LINK_CACHE.pop(key, None)
        return []

    MESSAGE_LINK_CACHE.move_to_end(key)

    return item.get("links", []) or []


def cleanup_local_caches():
    now = time.monotonic()

    expired_fp = [
        key
        for key, expires_at
        in FINGERPRINT_CACHE.items()
        if expires_at <= now
    ]

    for key in expired_fp:
        FINGERPRINT_CACHE.pop(key, None)

    if len(FINGERPRINT_CACHE) > FINGERPRINT_CACHE_MAX:
        excess = (
            len(FINGERPRINT_CACHE)
            -
            FINGERPRINT_CACHE_MAX
        )

        for key in list(FINGERPRINT_CACHE.keys())[:excess]:
            FINGERPRINT_CACHE.pop(key, None)

    expired_links = [
        key
        for key, item
        in MESSAGE_LINK_CACHE.items()
        if item.get("expires_at", 0) <= now
    ]

    for key in expired_links:
        MESSAGE_LINK_CACHE.pop(key, None)

    while len(MESSAGE_LINK_CACHE) > MESSAGE_LINK_CACHE_MAX:
        MESSAGE_LINK_CACHE.popitem(last=False)

    while len(ENTITY_CACHE) > ENTITY_CACHE_MAX:
        ENTITY_CACHE.popitem(last=False)


async def cache_maintenance_loop():
    while True:
        try:
            cleanup_local_caches()
            gc.collect()

            print(
                "[Memory] Limpeza concluída.",
                f"Entities: {len(ENTITY_CACHE)} |",
                f"Links: {len(MESSAGE_LINK_CACHE)} |",
                f"Fingerprints: {len(FINGERPRINT_CACHE)}"
            )

        except Exception as error:
            print(
                "[Memory] Erro no ciclo de manutenção:",
                type(error).__name__,
                str(error)
            )

        await asyncio.sleep(
            CACHE_MAINTENANCE_INTERVAL
        )


# ============================================================
# TRAVA ANTI-DUPLICAÇÃO E SISTEMA DE INSTÂNCIA ÚNICA
# ============================================================

PROCESS_LOCK_HANDLE = None
PROCESSING_KEYS = set()
PROCESSING_KEYS_LOCK = asyncio.Lock()

FINGERPRINT_CACHE = {}
FINGERPRINT_CACHE_LOCK = asyncio.Lock()
FINGERPRINT_TTL_SECONDS = 30

SELF_PUBLISHED_CACHE = {}
SELF_PUBLISHED_CACHE_LOCK = asyncio.Lock()
SELF_PUBLISHED_TTL_SECONDS = 120


def acquire_process_lock():
    global PROCESS_LOCK_HANDLE

    safe_session = re.sub(
        r"[^a-zA-Z0-9_.-]+",
        "_",
        str(SESSION_NAME)
    )

    lock_path = os.path.join(
        tempfile.gettempdir(),
        f"{safe_session}_{WORKER_ID}.lock"
    )

    PROCESS_LOCK_HANDLE = open(
        lock_path,
        "w"
    )

    try:
        fcntl.flock(
            PROCESS_LOCK_HANDLE.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB
        )

        PROCESS_LOCK_HANDLE.seek(0)
        PROCESS_LOCK_HANDLE.truncate()
        PROCESS_LOCK_HANDLE.write(
            str(os.getpid())
        )
        PROCESS_LOCK_HANDLE.flush()

        print(
            "[Worker Lock] Instância exclusiva iniciada com sucesso."
        )

    except BlockingIOError:
        raise RuntimeError(
            "Conflito de Instância: Já existe um Worker rodando para esta sessão "
            "neste ambiente. Feche o outro processo antes de prosseguir."
        )


async def claim_processing_key(key):
    async with PROCESSING_KEYS_LOCK:
        if key in PROCESSING_KEYS:
            return False
        PROCESSING_KEYS.add(key)
        return True


async def release_processing_key(key):
    async with PROCESSING_KEYS_LOCK:
        PROCESSING_KEYS.discard(key)


def normalize_text_for_fingerprint(text):
    text = text or ""
    return " ".join(text.split())


def media_signature(message):
    media = getattr(message, "media", None)

    if media is None:
        return "no-media"

    parts = [type(media).__name__]

    photo = getattr(media, "photo", None)
    if photo is not None:
        parts.append(f"photo:{getattr(photo, 'id', '')}")

    document = getattr(media, "document", None)
    if document is not None:
        parts.append(f"document:{getattr(document, 'id', '')}")

    webpage = getattr(media, "webpage", None)
    if webpage is not None:
        parts.append(f"webpage:{getattr(webpage, 'id', '')}")

    return "|".join(str(part) for part in parts)


def build_message_fingerprint(source_id, automation_id, message, grouped_id=None):
    normalized_text = normalize_text_for_fingerprint(
        getattr(message, "message", "")
    )

    signature = media_signature(message)

    raw = "|".join([
        str(automation_id),
        str(source_id),
        str(grouped_id if grouped_id is not None else getattr(message, "id", "")),
        normalized_text,
        signature
    ])

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_album_fingerprint(source_id, automation_id, grouped_id, messages):
    components = []

    for message in messages:
        components.append(
            "|".join([
                str(getattr(message, "id", "")),
                normalize_text_for_fingerprint(getattr(message, "message", "")),
                media_signature(message)
            ])
        )

    raw = "|".join([
        str(automation_id),
        str(source_id),
        str(grouped_id),
        "||".join(components)
    ])

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def claim_fingerprint(fingerprint):
    now = time.monotonic()

    async with FINGERPRINT_CACHE_LOCK:
        expired = [
            key
            for key, expires_at in FINGERPRINT_CACHE.items()
            if expires_at <= now
        ]

        for key in expired:
            FINGERPRINT_CACHE.pop(key, None)

        expires_at = FINGERPRINT_CACHE.get(fingerprint)

        if expires_at is not None and expires_at > now:
            return False

        FINGERPRINT_CACHE[fingerprint] = now + FINGERPRINT_TTL_SECONDS

        if len(FINGERPRINT_CACHE) > FINGERPRINT_CACHE_MAX:
            excess = (
                len(FINGERPRINT_CACHE)
                -
                FINGERPRINT_CACHE_MAX
            )

            for key in list(FINGERPRINT_CACHE.keys())[:excess]:
                FINGERPRINT_CACHE.pop(key, None)

        return True


async def remember_self_published(chat_id, message_id):
    key = (
        str(chat_id).strip(),
        str(message_id).strip()
    )

    async with SELF_PUBLISHED_CACHE_LOCK:
        now = time.monotonic()

        expired = [
            cache_key
            for cache_key, expires_at
            in SELF_PUBLISHED_CACHE.items()
            if expires_at <= now
        ]

        for cache_key in expired:
            SELF_PUBLISHED_CACHE.pop(cache_key, None)

        SELF_PUBLISHED_CACHE[key] = (
            now + SELF_PUBLISHED_TTL_SECONDS
        )


async def is_self_published(chat_id, message_id):
    key = (
        str(chat_id).strip(),
        str(message_id).strip()
    )

    async with SELF_PUBLISHED_CACHE_LOCK:
        now = time.monotonic()

        expires_at = SELF_PUBLISHED_CACHE.get(key)

        if (
            expires_at is not None
            and expires_at > now
        ):
            return True

        if expires_at is not None:
            SELF_PUBLISHED_CACHE.pop(key, None)

        return False


async def is_self_published_album(chat_id, messages):
    for message in messages:
        if await is_self_published(
            chat_id,
            getattr(message, "id", "")
        ):
            return True

    return False


# ============================================================
# ENDPOINTS REST BACKEND (LOVABLE)
# ============================================================

AUTOMATIONS_ENDPOINT = "/api/public/worker/automations"
LOGS_ENDPOINT = "/api/public/worker/logs"
HEARTBEAT_ENDPOINT = "/api/public/worker/heartbeat"
CHATS_SYNC_ENDPOINT = "/api/public/worker/chats/sync"
MESSAGE_LINK_UPSERT_ENDPOINT = "/api/public/worker/message-links/upsert"
MESSAGE_LINK_FIND_ENDPOINT = "/api/public/worker/message-links/find"
MESSAGE_LINK_REMOVE_ENDPOINT = "/api/public/worker/message-links/remove"


# ============================================================
# COMUNICAÇÃO HTTP
# ============================================================

HTTP_CLIENT = None


async def get_http_client():
    global HTTP_CLIENT

    if (
        HTTP_CLIENT is None
        or HTTP_CLIENT.is_closed
    ):
        HTTP_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=15,
                read=60,
                write=60,
                pool=15
            ),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10
            )
        )

    return HTTP_CLIENT


async def close_http_client():
    global HTTP_CLIENT

    if (
        HTTP_CLIENT is not None
        and not HTTP_CLIENT.is_closed
    ):
        await HTTP_CLIENT.aclose()


async def lovable_request(
    path,
    method="GET",
    data=None
):
    headers = {
        "x-worker-secret": WORKER_SECRET,
        "Content-Type": "application/json"
    }

    url = f"{LOVABLE_API_URL}{path}"
    http = await get_http_client()

    try:
        if method == "POST":
            response = await http.post(url, json=data, headers=headers)
        elif method == "PUT":
            response = await http.put(url, json=data, headers=headers)
        elif method == "DELETE":
            response = await http.request("DELETE", url, json=data, headers=headers)
        else:
            response = await http.get(url, headers=headers)

        response.raise_for_status()

        if not response.content:
            return {}

        return response.json()

    except httpx.HTTPStatusError as error:
        print(f"[Lovable] HTTP {error.response.status_code} - URL: {url}")
        try:
            print("[Lovable] Resposta:", error.response.text)
        except Exception:
            pass
        raise

    except httpx.RequestError as error:
        print("[Lovable] Erro de rede/conexão:", str(error))
        raise


# ============================================================
# GESTÃO DE AUTOMAÇÕES
# ============================================================

async def load_automations(force_refresh=False):
    now = time.monotonic()

    if (
        not force_refresh
        and AUTOMATIONS_CACHE["expires_at"] > now
    ):
        return AUTOMATIONS_CACHE["data"]

    result = await lovable_request(AUTOMATIONS_ENDPOINT)
    automations = result.get("automations", []) or []

    AUTOMATIONS_CACHE["data"] = automations
    AUTOMATIONS_CACHE["expires_at"] = now + AUTOMATIONS_CACHE_TTL

    return automations


def debug_automations(automations):
    print("\n================ DIAGNÓSTICO DE AUTOMAÇÕES ================")
    print("[DEBUG] Automações ativas no painel:", len(automations))

    for index, automation in enumerate(automations, start=1):
        replacements = automation.get("replacements", []) or []
        blacklist = automation.get("blacklist", []) or []

        print(f"\n[DEBUG] Automação #{index}")
        print("[DEBUG] ID:", automation.get("id"))
        print("[DEBUG] Nome:", automation.get("name"))
        print("[DEBUG] Chat Origem (Source):", automation.get("source_chat_id"))
        print("[DEBUG] Chat Destino (Destination):", automation.get("destination_chat_id"))
        print("[DEBUG] Substituições de texto:", len(replacements))
        print("[DEBUG] Palavras bloqueadas (Blacklist):", len(blacklist))

    print("\n===========================================================\n")


async def get_matching_automations(source_id):
    automations = await load_automations()
    source_id_text = str(source_id).strip()
    matches = []
    configured_sources = []

    for automation in automations:
        source = automation.get("source_chat_id")
        if source is None:
            continue

        source_text = str(source).strip()
        if not source_text:
            continue

        configured_sources.append(source_text)

        if source_text == source_id_text:
            matches.append(automation)

    if EVENT_DEBUG:
        print(f"[Match] Chat evento: {source_id_text} | Automações localizadas: {len(matches)}")
        if not matches:
            print("[Match] Sem automação. Origens mapeadas pelo backend:", configured_sources)

    return matches


# ============================================================
# HEARTBEAT DO WORKER
# ============================================================

async def send_heartbeat():
    me = await client.get_me()
    display_name = f"{me.first_name or ''} {me.last_name or ''}".strip()

    payload = {
        "worker_id": WORKER_ID,
        "telegram_user_id": str(me.id),
        "telegram_display_name": display_name,
        "telegram_username": me.username,
        "status": "online",
        "version": WORKER_VERSION
    }

    response = await lovable_request(HEARTBEAT_ENDPOINT, "POST", payload)
    print("[Heartbeat] Status enviado com sucesso.")

    if isinstance(response, dict):
        account_id = response.get("telegram_account_id")
        if account_id:
            print("[Heartbeat] Conta associada no Lovable:", account_id)

    return response


async def heartbeat_loop():
    while True:
        try:
            await send_heartbeat()
        except Exception as error:
            print("[Heartbeat] Falha na sincronização de presença:", type(error).__name__, str(error))

        await asyncio.sleep(HEARTBEAT_INTERVAL)


# ============================================================
# CHATS E RESOLUÇÃO DE ENTIDADES
# ============================================================

def get_chat_type(entity):
    if isinstance(entity, Channel):
        if getattr(entity, "megagroup", False):
            return "supergroup"
        return "channel"

    if isinstance(entity, Chat):
        return "group"

    return None


async def warm_entity_cache():
    print("[Entities] Inicializando cache de diálogos...")
    count = 0

    async for dialog in client.iter_dialogs():
        entity_cache_set(str(dialog.id), dialog.input_entity)
        count += 1

    print(f"[Entities] {count} entidades mapeadas com sucesso.")


async def resolve_destination_entity(destination_chat_id):
    destination_str = str(destination_chat_id).strip()
    cached = entity_cache_get(destination_str)

    if cached is not None:
        return cached

    try:
        entity = await client.get_input_entity(int(destination_str))
        entity_cache_set(destination_str, entity)
        return entity
    except Exception:
        pass

    async for dialog in client.iter_dialogs():
        dialog_id = str(dialog.id)
        entity_cache_set(dialog_id, dialog.input_entity)

        if dialog_id == destination_str:
            return dialog.input_entity

    raise ValueError(f"Não foi possível localizar o chat de destino: {destination_str}")


async def sync_telegram_chats():
    print("[Chats] Sincronizando catálogo de canais com a API...")
    me = await client.get_me()
    chats = []

    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        entity_cache_set(str(dialog.id), dialog.input_entity)

        chat_type = get_chat_type(entity)
        if not chat_type:
            continue

        username = getattr(entity, "username", None)
        title = dialog.name or getattr(entity, "title", None) or "Sem título"

        chats.append({
            "telegram_chat_id": str(dialog.id),
            "title": title,
            "username": username,
            "type": chat_type,
            "is_private": not bool(username)
        })

    response = await lovable_request(
        CHATS_SYNC_ENDPOINT,
        "POST",
        {
            "telegram_user_id": str(me.id),
            "chats": chats
        }
    )

    print("[Chats] Canais sincronizados com sucesso:", response.get("synced", len(chats)))
    chats.clear()
    gc.collect()

    return response


async def chat_sync_loop():
    while True:
        try:
            await sync_telegram_chats()
        except Exception as error:
            print("[Chats] Erro no ciclo de sincronização:", type(error).__name__, str(error))

        await asyncio.sleep(CHAT_SYNC_INTERVAL)


# ============================================================
# PERSISTÊNCIA DE LINKS (ORIGEM <-> DESTINO)
# ============================================================

def message_link_key(source_chat_id, source_message_id, automation_id):
    return f"{source_chat_id}:{source_message_id}:{automation_id}"


async def save_message_link(
    automation_id,
    source_chat_id,
    source_message_id,
    destination_chat_id,
    destination_message_id,
    source_grouped_id=None
):
    link = {
        "automation_id": automation_id,
        "source_chat_id": str(source_chat_id),
        "source_message_id": int(source_message_id),
        "source_grouped_id": str(source_grouped_id) if source_grouped_id else None,
        "destination_chat_id": str(destination_chat_id),
        "destination_message_id": int(destination_message_id)
    }

    key = message_link_key(source_chat_id, source_message_id, automation_id)
    message_link_cache_set(key, [link])

    print(f"[Link] Vinculado localmente: {source_message_id} -> {destination_message_id}")

    try:
        await lovable_request(MESSAGE_LINK_UPSERT_ENDPOINT, "POST", link)
        print("[Link] Persistido na API Lovable.")
    except Exception as error:
        print("[Link] Erro ao persistir remoto (cache ativo):", type(error).__name__)


async def find_message_links(source_chat_id, source_message_id, automation_id=None):
    local_links = []

    if automation_id:
        key = message_link_key(source_chat_id, source_message_id, automation_id)
        local_links = message_link_cache_get(key) or []
    else:
        suffix = f":{source_message_id}:"
        now = time.monotonic()

        for key, item in list(MESSAGE_LINK_CACHE.items()):
            if item.get("expires_at", 0) <= now:
                MESSAGE_LINK_CACHE.pop(key, None)
                continue

            if key.startswith(f"{source_chat_id}:") and suffix in key:
                local_links.extend(item.get("links", []) or [])

    if local_links:
        return local_links

    payload = {
        "source_chat_id": str(source_chat_id),
        "source_message_id": int(source_message_id)
    }

    if automation_id:
        payload["automation_id"] = automation_id

    try:
        response = await lovable_request(MESSAGE_LINK_FIND_ENDPOINT, "POST", payload)
        links = response.get("links", []) or []

        for link in links:
            key = message_link_key(
                link["source_chat_id"],
                link["source_message_id"],
                link["automation_id"]
            )
            message_link_cache_set(key, [link])

        return links
    except Exception as error:
        print("[Link] Falha na busca remota:", type(error).__name__)
        return []


async def remove_message_links(source_chat_id, source_message_id, automation_id=None):
    keys_to_remove = []

    for key in MESSAGE_LINK_CACHE.keys():
        prefix = f"{source_chat_id}:{source_message_id}:"
        if key.startswith(prefix):
            if automation_id is None or key.endswith(f":{automation_id}"):
                keys_to_remove.append(key)

    for key in keys_to_remove:
        MESSAGE_LINK_CACHE.pop(key, None)

    payload = {
        "source_chat_id": str(source_chat_id),
        "source_message_id": int(source_message_id)
    }

    if automation_id:
        payload["automation_id"] = automation_id

    try:
        await lovable_request(MESSAGE_LINK_REMOVE_ENDPOINT, "POST", payload)
    except Exception:
        pass


# ============================================================
# PARSER DE ENTIDADES E FORMATAÇÃO DE TEXTO
# ============================================================

def utf16_length(text):
    if not text:
        return 0
    return len(text.encode("utf-16-le")) // 2


def normalize_replacement_rule(rule):
    if not isinstance(rule, dict):
        return None

    find = (
        rule.get("match")
        or rule.get("find")
        or rule.get("source")
        or rule.get("from")
        or rule.get("original")
        or rule.get("old_value")
        or ""
    )

    replacement = None
    for key in ("replacement", "replace", "target", "to", "new_value", "destination"):
        if key in rule:
            replacement = rule.get(key)
            break

    if replacement is None:
        replacement = ""

    find = str(find)
    replacement = str(replacement)

    if not find:
        return None

    if rule.get("enabled", True) is False:
        return None

    case_sensitive = rule.get("case_sensitive", False)

    try:
        priority = int(rule.get("priority", 0) or 0)
    except (TypeError, ValueError):
        priority = 0

    return {
        "match": find,
        "replacement": replacement,
        "case_sensitive": bool(case_sensitive),
        "priority": priority,
        "_raw": rule,
    }


def get_active_replacements(automation):
    raw_replacements = automation.get("replacements", []) or []
    normalized = []

    for rule in raw_replacements:
        normalized_rule = normalize_replacement_rule(rule)
        if normalized_rule is not None:
            normalized.append(normalized_rule)

    return sorted(normalized, key=lambda x: x.get("priority", 0))


def apply_replacements(text, replacements):
    if not text or not replacements:
        return text

    result = text
    for rule in replacements:
        find = rule.get("match", "")
        replacement = rule.get("replacement", "")

        if not find:
            continue

        if rule.get("case_sensitive", False):
            result = result.replace(find, replacement)
        else:
            result = re.sub(
                re.escape(find),
                lambda _: replacement,
                result,
                flags=re.IGNORECASE
            )

    return result


def is_blacklisted(text, blacklist):
    if not text or not blacklist:
        return False

    for word in blacklist:
        if isinstance(word, str) and word.strip():
            if re.search(re.escape(word.strip()), text, flags=re.IGNORECASE):
                return True
    return False


# ============================================================
# PROCESSAMENTO DE MENSAGENS E ÁLBUNS (COM FALLBACK DE RESTRIÇÃO)
# ============================================================

async def send_single_message_with_bypass(destination_entity, message, custom_text=None):
    """
    Tenta encaminhar a mensagem. Se o canal de origem proibir encaminhamento
    (ChatForwardsRestrictedError), realiza o fallback baixando e reenviando a mídia/texto.
    """
    async with MEDIA_SEND_SEMAPHORE:
        text_to_send = custom_text if custom_text is not None else message.message

        try:
            # Tenta o encaminhamento nativo primeiro (preserva estilo original)
            if custom_text is None:
                sent = await client.forward_messages(destination_entity, message)
                if isinstance(sent, list):
                    return sent[0]
                return sent
            else:
                # Se o texto foi modificado, republica dependendo da existência de mídia
                if message.media:
                    temp_file = await client.download_media(message)
                    try:
                        sent = await client.send_file(
                            destination_entity,
                            temp_file,
                            caption=text_to_send,
                            formatting_entities=message.entities
                        )
                        return sent
                    finally:
                        if temp_file and os.path.exists(temp_file):
                            os.remove(temp_file)
                else:
                    return await client.send_message(
                        destination_entity,
                        text_to_send,
                        formatting_entities=message.entities
                    )

        except ChatForwardsRestrictedError:
            print(f"[Bypass] Origem restrita para mensagem {message.id}! Aplicando cópia por download/upload...")
            if message.media:
                temp_file = await client.download_media(message)
                try:
                    sent = await client.send_file(
                        destination_entity,
                        temp_file,
                        caption=text_to_send,
                        formatting_entities=message.entities
                    )
                    return sent
                finally:
                    if temp_file and os.path.exists(temp_file):
                        os.remove(temp_file)
            else:
                return await client.send_message(
                    destination_entity,
                    text_to_send,
                    formatting_entities=message.entities
                )


async def send_album_with_bypass(destination_entity, messages, custom_caption=None):
    """
    Tenta encaminhar um álbum. Se a origem for protegida, baixa os arquivos em
    lote e republica a galeria no destino mantendo a legenda.
    """
    async with MEDIA_SEND_SEMAPHORE:
        temp_files = []
        try:
            # Tenta o encaminhamento nativo se não houve alteração no texto
            if custom_caption is None:
                try:
                    sent_messages = await client.forward_messages(destination_entity, messages)
                    return sent_messages
                except ChatForwardsRestrictedError:
                    print(f"[Bypass Album] Origem restrita no álbum de {len(messages)} mídias! Iniciando cópia...")

            files_to_send = []
            captions = []

            for index, msg in enumerate(messages):
                if msg.media:
                    fpath = await client.download_media(msg)
                    if fpath:
                        temp_files.append(fpath)
                        files_to_send.append(fpath)
                        # Aplica a legenda customizada/original na primeira foto do álbum
                        if index == 0 and custom_caption is not None:
                            captions.append(custom_caption)
                        else:
                            captions.append(msg.message)

            if files_to_send:
                sent_messages = await client.send_file(
                    destination_entity,
                    files_to_send,
                    caption=captions
                )
                return sent_messages if isinstance(sent_messages, list) else [sent_messages]

        finally:
            for file_path in temp_files:
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass


async def process_message_event(event):
    message = event.message
    source_id = str(event.chat_id)

    automations = await get_matching_automations(source_id)
    if not automations:
        return

    for automation in automations:
        automation_id = automation.get("id")
        destination_id = automation.get("destination_chat_id")

        if not destination_id:
            continue

        if await is_self_published(source_id, message.id):
            continue

        lock_key = f"{automation_id}:{source_id}:{message.id}"
        if not await claim_processing_key(lock_key):
            continue

        try:
            fingerprint = build_message_fingerprint(source_id, automation_id, message)
            if not await claim_fingerprint(fingerprint):
                continue

            replacements = get_active_replacements(automation)
            blacklist = automation.get("blacklist", []) or []

            original_text = message.message or ""
            if is_blacklisted(original_text, blacklist):
                print(f"[Automação {automation_id}] Mensagem {message.id} contida pela Blacklist.")
                continue

            modified_text = apply_replacements(original_text, replacements)
            custom_text = modified_text if modified_text != original_text else None

            destination_entity = await resolve_destination_entity(destination_id)

            sent_msg = await send_single_message_with_bypass(
                destination_entity,
                message,
                custom_text=custom_text
            )

            if sent_msg:
                await remember_self_published(destination_id, sent_msg.id)
                await save_message_link(
                    automation_id,
                    source_id,
                    message.id,
                    destination_id,
                    sent_msg.id
                )

        except Exception as err:
            print(f"[Automação Erro] Falha ao clonar mensagem {message.id}: {type(err).__name__} - {err}")

        finally:
            await release_processing_key(lock_key)


async def process_album_messages(source_id, grouped_id, messages):
    automations = await get_matching_automations(source_id)
    if not automations:
        return

    for automation in automations:
        automation_id = automation.get("id")
        destination_id = automation.get("destination_chat_id")

        if not destination_id:
            continue

        if await is_self_published_album(source_id, messages):
            continue

        lock_key = f"album:{automation_id}:{source_id}:{grouped_id}"
        if not await claim_processing_key(lock_key):
            continue

        try:
            fingerprint = build_album_fingerprint(source_id, automation_id, grouped_id, messages)
            if not await claim_fingerprint(fingerprint):
                continue

            replacements = get_active_replacements(automation)
            blacklist = automation.get("blacklist", []) or []

            main_message = next((m for m in messages if m.message), messages[0])
            original_text = main_message.message or ""

            if is_blacklisted(original_text, blacklist):
                print(f"[Álbum {automation_id}] Galeria {grouped_id} bloqueada por filtro de palavra.")
                continue

            modified_text = apply_replacements(original_text, replacements)
            custom_caption = modified_text if modified_text != original_text else None

            destination_entity = await resolve_destination_entity(destination_id)

            sent_messages = await send_album_with_bypass(
                destination_entity,
                messages,
                custom_caption=custom_caption
            )

            if sent_messages:
                for src_m, dst_m in zip(messages, sent_messages):
                    await remember_self_published(destination_id, dst_m.id)
                    await save_message_link(
                        automation_id,
                        source_id,
                        src_m.id,
                        destination_id,
                        dst_m.id,
                        source_grouped_id=grouped_id
                    )

        except Exception as err:
            print(f"[Álbum Erro] Falha ao processar galeria {grouped_id}: {type(err).__name__} - {err}")

        finally:
            await release_processing_key(lock_key)


# ============================================================
# BUFFER DE ÁLBUNS EM MEMÓRIA
# ============================================================

ALBUM_BUFFER = defaultdict(list)
ALBUM_TASKS = {}


async def album_flush_task(source_id, grouped_id):
    await asyncio.sleep(1.5)
    messages = ALBUM_BUFFER.pop((source_id, grouped_id), [])
    ALBUM_TASKS.pop((source_id, grouped_id), None)

    if messages:
        messages.sort(key=lambda x: x.id)
        await process_album_messages(source_id, grouped_id, messages)


# ============================================================
# SOURCE RECOVERY ENGINE (RECUPERAÇÃO DE HISTÓRICO PERDIDO)
# ============================================================

def load_source_recovery_state():
    if not os.path.exists(SOURCE_RECOVERY_STATE_FILE):
        return {}

    try:
        with open(SOURCE_RECOVERY_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as err:
        print("[Recovery] Erro ao ler arquivo de estado:", err)
        return {}


def save_source_recovery_state(state):
    try:
        tmp_file = f"{SOURCE_RECOVERY_STATE_FILE}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_file, SOURCE_RECOVERY_STATE_FILE)
    except Exception as err:
        print("[Recovery] Erro ao salvar estado:", err)


async def source_recovery_loop():
    while True:
        try:
            automations = await load_automations()
            active_sources = set()

            for auto in automations:
                src = auto.get("source_chat_id")
                if src:
                    active_sources.add(str(src).strip())

            state = load_source_recovery_state()

            for source_id in active_sources:
                try:
                    last_processed_id = state.get(source_id, 0)
                    entity = await resolve_destination_entity(source_id)

                    messages_to_process = []
                    async for msg in client.iter_messages(
                        entity,
                        min_id=last_processed_id,
                        limit=SOURCE_RECOVERY_MAX_MESSAGES,
                        reverse=True
                    ):
                        messages_to_process.append(msg)

                    if messages_to_process:
                        max_id = last_processed_id

                        for msg in messages_to_process:
                            if msg.id > max_id:
                                max_id = msg.id

                            event_mock = SimpleNamespace(
                                message=msg,
                                chat_id=int(source_id),
                                grouped_id=msg.grouped_id
                            )

                            if msg.grouped_id:
                                key = (source_id, msg.grouped_id)
                                ALBUM_BUFFER[key].append(msg)
                                if key not in ALBUM_TASKS:
                                    ALBUM_TASKS[key] = asyncio.create_task(
                                        album_flush_task(source_id, msg.grouped_id)
                                    )
                            else:
                                await process_message_event(event_mock)

                        state[source_id] = max_id
                        save_source_recovery_state(state)

                except Exception as src_err:
                    print(f"[Recovery Error] Falha ao varrer canal {source_id}: {src_err}")

        except Exception as loop_err:
            print(f"[Recovery Error] Exceção geral no ciclo de recuperação: {loop_err}")

        await asyncio.sleep(SOURCE_RECOVERY_INTERVAL)


# ============================================================
# LISTENERS DE EVENTOS DO TELEGRAM (NEW / EDIT / DELETE)
# ============================================================

@client.on(events.NewMessage)
async def on_new_message(event):
    if event.grouped_id:
        source_id = str(event.chat_id)
        key = (source_id, event.grouped_id)

        ALBUM_BUFFER[key].append(event.message)

        if key not in ALBUM_TASKS:
            ALBUM_TASKS[key] = asyncio.create_task(
                album_flush_task(source_id, event.grouped_id)
            )
    else:
        await process_message_event(event)


@client.on(events.MessageEdited)
async def on_message_edited(event):
    source_id = str(event.chat_id)
    message_id = event.message.id

    links = await find_message_links(source_id, message_id)
    if not links:
        return

    for link in links:
        try:
            automation_id = link.get("automation_id")
            automations = await load_automations()
            automation = next((a for a in automations if a.get("id") == automation_id), None)

            if not automation:
                continue

            replacements = get_active_replacements(automation)
            original_text = event.message.message or ""
            new_text = apply_replacements(original_text, replacements)

            destination_entity = await resolve_destination_entity(link["destination_chat_id"])
            await client.edit_message(
                destination_entity,
                int(link["destination_message_id"]),
                new_text,
                formatting_entities=event.message.entities
            )
            print(f"[Edição Sincronizada] Destino {link['destination_message_id']} atualizado.")
        except Exception as err:
            print(f"[Edição Erro] Falha ao replicar alteração: {err}")


@client.on(events.MessageDeleted)
async def on_message_deleted(event):
    source_id = str(event.chat_id)
    for deleted_id in event.deleted_ids:
        links = await find_message_links(source_id, deleted_id)
        if not links:
            continue

        for link in links:
            try:
                destination_entity = await resolve_destination_entity(link["destination_chat_id"])
                await client.delete_messages(destination_entity, [int(link["destination_message_id"])])
                print(f"[Deleção Sincronizada] Mensagem {link['destination_message_id']} removida do destino.")
            except Exception as err:
                print(f"[Deleção Erro] Falha ao apagar no destino: {err}")

        await remove_message_links(source_id, deleted_id)


# ============================================================
# INITIALIZATION / ENTRYPOINT
# ============================================================

async def main():
    acquire_process_lock()

    print("[Worker] Estabelecendo conexão com o Telegram...")
    await client.start()

    me = await client.get_me()
    print(f"[Worker] Conectado como: {me.first_name} (@{me.username}) - ID: {me.id}")

    await warm_entity_cache()
    await send_heartbeat()
    await sync_telegram_chats()

    # Dispara serviços em segundo plano
    asyncio.create_task(heartbeat_loop())
    asyncio.create_task(chat_sync_loop())
    asyncio.create_task(cache_maintenance_loop())
    asyncio.create_task(source_recovery_loop())

    print("[Worker] Todos os módulos carregados. Aguardando mensagens...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Worker] Parado pelo usuário.")
    finally:
        asyncio.run(close_http_client())
