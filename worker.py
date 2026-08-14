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
    MessageEntityTextUrl,
)

from dotenv import load_dotenv


# ============================================================
# CONFIGURAÇÃO
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

WORKER_VERSION = "1.3.0-safe-protected"

# Diagnóstico dos updates recebidos do Telegram.
# Deixe "1" durante os testes. Depois pode mudar para "0" no .env
# se quiser reduzir a quantidade de logs.
EVENT_DEBUG = os.getenv("TELEGRAM_EVENT_DEBUG", "1").strip() == "1"

HEARTBEAT_INTERVAL = 60
CHAT_SYNC_INTERVAL = 15 * 60

# ============================================================
# AJUSTES DE PRODUÇÃO / MEMÓRIA
# ============================================================

AUTOMATIONS_CACHE_TTL = 5
ENTITY_CACHE_MAX = 1500
MESSAGE_LINK_CACHE_MAX = 5000
MESSAGE_LINK_CACHE_TTL = 6 * 60 * 60
FINGERPRINT_CACHE_MAX = 5000
CACHE_MAINTENANCE_INTERVAL = 5 * 60
MEDIA_SEND_CONCURRENCY = 1

# Recuperação de updates perdidos. Além dos eventos em tempo real, o Worker
# consulta somente os canais configurados como fonte. Isso cobre postagens
# feitas pela própria conta em outra sessão (por exemplo, scheduled_sender)
# que eventualmente não sejam entregues como update à sessão desta EC2.
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
# CLIENT
# ============================================================

client = TelegramClient(
    SESSION_NAME,
    API_ID,
    API_HASH
)


# ============================================================
# CACHE
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
                "[Memory] Cache:",
                f"entities={len(ENTITY_CACHE)}",
                f"links={len(MESSAGE_LINK_CACHE)}",
                f"fingerprints={len(FINGERPRINT_CACHE)}"
            )

        except Exception as error:
            print(
                "[Memory] Erro na manutenção:",
                type(error).__name__,
                str(error)
            )

        await asyncio.sleep(
            CACHE_MAINTENANCE_INTERVAL
        )


# ============================================================
# ANTI-DUPLICAÇÃO / LOCK
# ============================================================

# Impede duas execuções do mesmo Worker no mesmo Mac/servidor.
# Isso é importante porque dois processos Telethon com a mesma
# conta podem receber o mesmo update e publicar duas vezes.
PROCESS_LOCK_HANDLE = None

# Impede que dois handlers concorrentes do MESMO processo
# processem a mesma mensagem/álbum ao mesmo tempo.
PROCESSING_KEYS = set()
PROCESSING_KEYS_LOCK = asyncio.Lock()

# Cache temporal por fingerprint para impedir duplicações
# que venham de updates diferentes do Telegram para o mesmo conteúdo.
FINGERPRINT_CACHE = {}
FINGERPRINT_CACHE_LOCK = asyncio.Lock()

# Janela de deduplicação.
FINGERPRINT_TTL_SECONDS = 30

# ============================================================
# MENSAGENS PUBLICADAS PELO PRÓPRIO WORKER
# ============================================================
#
# A v1.0.0 ignorava TODO event.out. Isso também ignora uma
# postagem legítima feita manualmente pela própria conta
# conectada dentro de um canal que é origem de automação.
#
# Agora só ignoramos mensagens que o próprio Worker publicou
# recentemente em um destino.
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
            "[Worker Lock] Processo exclusivo adquirido."
        )

    except BlockingIOError:
        raise RuntimeError(
            "Já existe outro Worker rodando com esta sessão "
            "neste computador/servidor. Feche o outro processo "
            "antes de iniciar este Worker."
        )


async def claim_processing_key(key):
    async with PROCESSING_KEYS_LOCK:

        if key in PROCESSING_KEYS:
            return False

        PROCESSING_KEYS.add(
            key
        )

        return True


async def release_processing_key(key):
    async with PROCESSING_KEYS_LOCK:
        PROCESSING_KEYS.discard(
            key
        )


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
# ENDPOINTS
# ============================================================

AUTOMATIONS_ENDPOINT = (
    "/api/public/worker/automations"
)

LOGS_ENDPOINT = (
    "/api/public/worker/logs"
)

HEARTBEAT_ENDPOINT = (
    "/api/public/worker/heartbeat"
)

CHATS_SYNC_ENDPOINT = (
    "/api/public/worker/chats/sync"
)

MESSAGE_LINK_UPSERT_ENDPOINT = (
    "/api/public/worker/message-links/upsert"
)

MESSAGE_LINK_FIND_ENDPOINT = (
    "/api/public/worker/message-links/find"
)

MESSAGE_LINK_REMOVE_ENDPOINT = (
    "/api/public/worker/message-links/remove"
)


# ============================================================
# LOVABLE
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

            response = await http.post(
                url,
                json=data,
                headers=headers
            )

        elif method == "PUT":

            response = await http.put(
                url,
                json=data,
                headers=headers
            )

        elif method == "DELETE":

            response = await http.request(
                "DELETE",
                url,
                json=data,
                headers=headers
            )

        else:

            response = await http.get(
                url,
                headers=headers
            )

        response.raise_for_status()

        if not response.content:
            return {}

        return response.json()

    except httpx.HTTPStatusError as error:

        print(
            f"[Lovable] HTTP "
            f"{error.response.status_code}"
        )

        print(
            "[Lovable] URL:",
            url
        )

        try:
            print(
                "[Lovable] Resposta:",
                error.response.text
            )
        except Exception:
            pass

        raise

    except httpx.RequestError as error:

        print(
            "[Lovable] Erro de conexão:",
            str(error)
        )

        raise


# ============================================================
# AUTOMAÇÕES
# ============================================================

async def load_automations(
    force_refresh=False
):

    now = time.monotonic()

    if (
        not force_refresh
        and
        AUTOMATIONS_CACHE["expires_at"] > now
    ):
        return AUTOMATIONS_CACHE["data"]

    result = await lovable_request(
        AUTOMATIONS_ENDPOINT
    )

    automations = result.get(
        "automations",
        []
    ) or []

    AUTOMATIONS_CACHE["data"] = automations
    AUTOMATIONS_CACHE["expires_at"] = (
        now
        +
        AUTOMATIONS_CACHE_TTL
    )

    return automations


def debug_automations(
    automations
):

    print(
        "\n================ DEBUG AUTOMAÇÕES ================"
    )

    print(
        "[DEBUG] Automações recebidas:",
        len(automations)
    )

    for index, automation in enumerate(
        automations,
        start=1
    ):

        replacements = (
            automation.get(
                "replacements",
                []
            )
            or []
        )

        blacklist = (
            automation.get(
                "blacklist",
                []
            )
            or []
        )

        print(
            f"\n[DEBUG] Automação #{index}"
        )

        print(
            "[DEBUG] ID:",
            automation.get("id")
        )

        print(
            "[DEBUG] Nome:",
            automation.get("name")
        )

        print(
            "[DEBUG] Source:",
            automation.get(
                "source_chat_id"
            )
        )

        print(
            "[DEBUG] Destination:",
            automation.get(
                "destination_chat_id"
            )
        )

        print(
            "[DEBUG] Replacements:",
            len(replacements)
        )

        print(
            "[DEBUG] Blacklist:",
            len(blacklist)
        )

    print(
        "\n===================================================\n"
    )


async def get_matching_automations(
    source_id
):
    """
    Retorna todas as automações cuja origem é o chat do evento.

    IMPORTANTE:
    Antes o Worker simplesmente retornava [] quando o source_chat_id
    não coincidia. Isso fazia parecer que o Telegram não tinha recebido
    a mensagem. Agora o log mostra claramente o chat recebido e as
    origens cadastradas no painel.
    """

    automations = await load_automations()

    source_id_text = str(source_id).strip()
    matches = []

    configured_sources = []

    for automation in automations:

        source = automation.get(
            "source_chat_id"
        )

        if source is None:
            continue

        source_text = str(source).strip()

        if not source_text:
            continue

        configured_sources.append(
            source_text
        )

        if source_text == source_id_text:

            matches.append(
                automation
            )

    if EVENT_DEBUG:

        print(
            "[Match] chat recebido:",
            source_id_text,
            "| automações encontradas:",
            len(matches)
        )

        if not matches:

            print(
                "[Match] Nenhuma automação para este chat."
            )

            print(
                "[Match] Origens configuradas:",
                configured_sources
            )

    return matches


# ============================================================
# HEARTBEAT
# ============================================================

async def send_heartbeat():

    me = await client.get_me()

    display_name = (
        f"{me.first_name or ''} "
        f"{me.last_name or ''}"
    ).strip()

    payload = {

        "worker_id":
            WORKER_ID,

        "telegram_user_id":
            str(me.id),

        "telegram_display_name":
            display_name,

        "telegram_username":
            me.username,

        "status":
            "online",

        "version":
            WORKER_VERSION
    }

    response = await lovable_request(
        HEARTBEAT_ENDPOINT,
        "POST",
        payload
    )

    print(
        "[Heartbeat] enviado."
    )

    if isinstance(
        response,
        dict
    ):

        account_id = response.get(
            "telegram_account_id"
        )

        if account_id:

            print(
                "[Heartbeat] Conta Lovable:",
                account_id
            )

    return response


async def heartbeat_loop():

    while True:

        try:

            await send_heartbeat()

        except Exception as error:

            print(
                "[Heartbeat] erro:",
                type(error).__name__,
                str(error)
            )

        await asyncio.sleep(
            HEARTBEAT_INTERVAL
        )


# ============================================================
# CHATS / ENTIDADES
# ============================================================

def get_chat_type(
    entity
):

    if isinstance(
        entity,
        Channel
    ):

        if getattr(
            entity,
            "megagroup",
            False
        ):

            return "supergroup"

        return "channel"

    if isinstance(
        entity,
        Chat
    ):

        return "group"

    return None


async def warm_entity_cache():

    print(
        "[Entities] Carregando entidades..."
    )

    count = 0

    async for dialog in client.iter_dialogs():

        entity_cache_set(
            str(dialog.id),
            dialog.input_entity
        )

        count += 1

    print(
        f"[Entities] {count} entidades carregadas."
    )


async def resolve_destination_entity(
    destination_chat_id
):

    destination_str = str(
        destination_chat_id
    ).strip()

    cached = entity_cache_get(
        destination_str
    )

    if cached is not None:

        return cached

    try:

        entity = await client.get_input_entity(
            int(destination_str)
        )

        entity_cache_set(
            destination_str,
            entity
        )

        return entity

    except Exception:
        pass

    async for dialog in client.iter_dialogs():

        dialog_id = str(
            dialog.id
        )

        entity_cache_set(
            dialog_id,
            dialog.input_entity
        )

        if dialog_id == destination_str:

            return dialog.input_entity

    raise ValueError(
        "Não foi possível resolver o chat "
        f"{destination_str}"
    )


async def sync_telegram_chats():

    print(
        "[Chats] Sincronizando..."
    )

    me = await client.get_me()

    chats = []

    async for dialog in client.iter_dialogs():

        entity = dialog.entity

        entity_cache_set(
            str(dialog.id),
            dialog.input_entity
        )

        chat_type = get_chat_type(
            entity
        )

        if not chat_type:
            continue

        username = getattr(
            entity,
            "username",
            None
        )

        title = (
            dialog.name
            or getattr(
                entity,
                "title",
                None
            )
            or "Sem nome"
        )

        chats.append({

            "telegram_chat_id":
                str(dialog.id),

            "title":
                title,

            "username":
                username,

            "type":
                chat_type,

            "is_private":
                not bool(username)
        })

    response = await lovable_request(
        CHATS_SYNC_ENDPOINT,
        "POST",
        {
            "telegram_user_id":
                str(me.id),

            "chats":
                chats
        }
    )

    print(
        "[Chats] Sincronizados:",
        response.get(
            "synced",
            len(chats)
        )
    )

    chats.clear()
    gc.collect()

    return response


async def chat_sync_loop():

    while True:

        try:

            await sync_telegram_chats()

        except Exception as error:

            print(
                "[Chats] erro:",
                type(error).__name__,
                str(error)
            )

        await asyncio.sleep(
            CHAT_SYNC_INTERVAL
        )


# ============================================================
# MESSAGE LINKS
# ============================================================

def message_link_key(
    source_chat_id,
    source_message_id,
    automation_id
):

    return (
        f"{source_chat_id}:"
        f"{source_message_id}:"
        f"{automation_id}"
    )


async def save_message_link(
    automation_id,
    source_chat_id,
    source_message_id,
    destination_chat_id,
    destination_message_id,
    source_grouped_id=None
):

    link = {

        "automation_id":
            automation_id,

        "source_chat_id":
            str(source_chat_id),

        "source_message_id":
            int(source_message_id),

        "source_grouped_id":
            (
                str(source_grouped_id)
                if source_grouped_id
                else None
            ),

        "destination_chat_id":
            str(destination_chat_id),

        "destination_message_id":
            int(destination_message_id)
    }

    key = message_link_key(
        source_chat_id,
        source_message_id,
        automation_id
    )

    message_link_cache_set(
        key,
        [link]
    )

    print(
        "[Link] Registrado localmente:",
        source_message_id,
        "→",
        destination_message_id
    )

    try:

        await lovable_request(
            MESSAGE_LINK_UPSERT_ENDPOINT,
            "POST",
            link
        )

        print(
            "[Link] Persistido no Lovable."
        )

    except Exception as error:

        print(
            "[Link] Persistência remota falhou, "
            "cache local continua ativo:",
            type(error).__name__
        )


async def find_message_links(
    source_chat_id,
    source_message_id,
    automation_id=None
):

    local_links = []

    if automation_id:

        key = message_link_key(
            source_chat_id,
            source_message_id,
            automation_id
        )

        local_links = (
            message_link_cache_get(
                key
            )
            or []
        )

    else:

        suffix = (
            f":{source_message_id}:"
        )

        now = time.monotonic()

        for key, item in list(
            MESSAGE_LINK_CACHE.items()
        ):

            if item.get(
                "expires_at",
                0
            ) <= now:
                MESSAGE_LINK_CACHE.pop(
                    key,
                    None
                )
                continue

            if (
                key.startswith(
                    f"{source_chat_id}:"
                )
                and suffix in key
            ):

                local_links.extend(
                    item.get(
                        "links",
                        []
                    )
                    or []
                )

    if local_links:

        return local_links

    payload = {

        "source_chat_id":
            str(source_chat_id),

        "source_message_id":
            int(source_message_id)
    }

    if automation_id:

        payload[
            "automation_id"
        ] = automation_id

    try:

        response = await lovable_request(
            MESSAGE_LINK_FIND_ENDPOINT,
            "POST",
            payload
        )

        links = (
            response.get(
                "links",
                []
            )
            or []
        )

        for link in links:

            key = message_link_key(
                link["source_chat_id"],
                link["source_message_id"],
                link["automation_id"]
            )

            message_link_cache_set(
                key,
                [link]
            )

        return links

    except Exception as error:

        print(
            "[Link] Busca remota falhou:",
            type(error).__name__
        )

        return []


async def remove_message_links(
    source_chat_id,
    source_message_id,
    automation_id=None
):

    keys_to_remove = []

    for key in (
        MESSAGE_LINK_CACHE.keys()
    ):

        prefix = (
            f"{source_chat_id}:"
            f"{source_message_id}:"
        )

        if key.startswith(
            prefix
        ):

            if (
                automation_id is None
                or
                key.endswith(
                    f":{automation_id}"
                )
            ):

                keys_to_remove.append(
                    key
                )

    for key in keys_to_remove:

        MESSAGE_LINK_CACHE.pop(
            key,
            None
        )

    payload = {

        "source_chat_id":
            str(source_chat_id),

        "source_message_id":
            int(source_message_id)
    }

    if automation_id:

        payload[
            "automation_id"
        ] = automation_id

    try:

        await lovable_request(
            MESSAGE_LINK_REMOVE_ENDPOINT,
            "POST",
            payload
        )

    except Exception:

        pass


# ============================================================
# TEXTO RICO
# ============================================================

def utf16_length(
    text
):

    if not text:
        return 0

    return len(
        text.encode(
            "utf-16-le"
        )
    ) // 2


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
    for key in (
        "replacement",
        "replace",
        "target",
        "to",
        "new_value",
        "destination"
    ):
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

    for index, rule in enumerate(raw_replacements, start=1):
        normalized_rule = normalize_replacement_rule(rule)

        if normalized_rule is None:
            print(
                f"[Replace] Regra #{index} ignorada. Campos recebidos: "
                f"{list(rule.keys()) if isinstance(rule, dict) else type(rule).__name__}"
            )
            continue

        normalized.append(normalized_rule)

    normalized = sorted(
        normalized,
        key=lambda x: x.get("priority", 0)
    )

    if raw_replacements:
        print(
            "[Replace] Regras recebidas:",
            len(raw_replacements),
            "| válidas:",
            len(normalized),
        )

        for index, rule in enumerate(normalized, start=1):
            print(
                f"[Replace] #{index}: "
                f"{rule['match']!r} -> {rule['replacement']!r} "
                f"| case_sensitive={rule['case_sensitive']}"
            )

    return normalized


def replace_value(value, replacements):
    if not value:
        return value

    result = value

    for rule in replacements:
        find = rule.get("match", "")
        replacement = rule.get("replacement", "")

        if not find:
            continue

        before = result

        if rule.get("case_sensitive", False):
            result = result.replace(find, replacement)
        else:
            result = re.sub(
                re.escape(find),
                lambda _: replacement,
                result,
                flags=re.IGNORECASE,
            )

        if result != before:
            print(
                "[Replace] Aplicado em URL/valor:",
                repr(find),
                "→",
                repr(replacement),
            )

    return result


def find_replacement_occurrences(text, replacements):
    occurrences = []
    working_text = text

    for rule in replacements:
        find = rule.get("match", "")
        replacement = rule.get("replacement", "")

        if not find:
            continue

        flags = 0 if rule.get("case_sensitive", False) else re.IGNORECASE
        pattern = re.compile(re.escape(find), flags)
        applied_count = 0

        while True:
            match = pattern.search(working_text)

            if not match:
                break

            start = match.start()
            end = match.end()
            before = working_text[:start]
            old_value = working_text[start:end]

            occurrences.append({
                "start": utf16_length(before),
                "old_length": utf16_length(old_value),
                "new_length": utf16_length(replacement),
            })

            working_text = (
                working_text[:start]
                + replacement
                + working_text[end:]
            )

            applied_count += 1

        if applied_count:
            print(
                "[Replace] Texto alterado:",
                repr(find),
                "→",
                repr(replacement),
                f"| ocorrências={applied_count}",
            )

    return working_text, occurrences


def adjust_entities_for_replacements(
    entities,
    occurrences
):

    if not entities:
        return []

    result = copy.deepcopy(
        entities
    )

    for occurrence in occurrences:

        replace_start = (
            occurrence["start"]
        )

        old_length = (
            occurrence[
                "old_length"
            ]
        )

        new_length = (
            occurrence[
                "new_length"
            ]
        )

        replace_end = (
            replace_start
            +
            old_length
        )

        delta = (
            new_length
            -
            old_length
        )

        for entity in result:

            entity_start = (
                entity.offset
            )

            entity_end = (
                entity.offset
                +
                entity.length
            )

            if (
                replace_end
                <= entity_start
            ):

                entity.offset += delta

            elif (
                replace_start
                < entity_end
                and
                replace_end
                > entity_start
            ):

                entity.length = max(
                    0,
                    entity.length
                    +
                    delta
                )

    return [
        entity
        for entity in result

        if getattr(
            entity,
            "length",
            0
        ) > 0
    ]


def process_hidden_urls(
    entities,
    replacements
):

    result = copy.deepcopy(
        entities
        or []
    )

    for entity in result:

        if isinstance(
            entity,
            MessageEntityTextUrl
        ):

            entity.url = (
                replace_value(
                    entity.url or "",
                    replacements
                )
            )

    return result


def check_blacklist(
    text,
    entities,
    automation
):

    blacklist = (
        automation.get(
            "blacklist",
            []
        )
        or []
    )

    hidden_urls = []

    for entity in (
        entities or []
    ):

        if isinstance(
            entity,
            MessageEntityTextUrl
        ):

            if entity.url:

                hidden_urls.append(
                    entity.url
                )

    content = (
        (text or "")
        +
        "\n"
        +
        "\n".join(
            hidden_urls
        )
    )

    for rule in blacklist:

        if not rule.get(
            "enabled",
            True
        ):

            continue

        term = rule.get(
            "term",
            ""
        )

        if not term:
            continue

        match_type = rule.get(
            "match_type",
            "contains"
        )

        detected = False

        if match_type == "contains":

            detected = (
                term.lower()
                in
                content.lower()
            )

        elif match_type == "exact":

            detected = (
                content.lower().strip()
                ==
                term.lower().strip()
            )

        elif match_type == "regex":

            try:

                detected = bool(
                    re.search(
                        term,
                        content,
                        flags=re.IGNORECASE
                    )
                )

            except re.error:

                continue

        if (
            detected
            and
            rule.get(
                "action",
                "block"
            )
            ==
            "block"
        ):

            return True

    return False


def process_rich_text(
    text,
    entities,
    automation
):

    text = text or ""

    entities = copy.deepcopy(
        entities
        or []
    )

    if check_blacklist(
        text,
        entities,
        automation
    ):

        return None, []

    replacements = (
        get_active_replacements(
            automation
        )
    )

    entities = process_hidden_urls(
        entities,
        replacements
    )

    (
        processed_text,
        occurrences
    ) = find_replacement_occurrences(
        text,
        replacements
    )

    if replacements:
        if processed_text != text:
            print(
                "[Replace] Resultado alterado com sucesso."
            )
        else:
            print(
                "[Replace] Nenhuma regra encontrou correspondência "
                "no texto visível."
            )

    processed_entities = (
        adjust_entities_for_replacements(
            entities,
            occurrences
        )
    )

    return (
        processed_text,
        processed_entities
    )


# ============================================================
# LOGS
# ============================================================

async def send_log(
    automation_id,
    source_message_id,
    status,
    original_text="",
    processed_text="",
    destination_message_id=None,
    blocked_reason=None,
    error_message=None
):

    payload = {

        "automation_id":
            automation_id,

        # O endpoint do Lovable espera STRING.
        "source_message_id":
            (
                str(source_message_id)
                if source_message_id is not None
                else None
            ),

        # O endpoint do Lovable espera STRING ou null.
        "destination_message_id":
            (
                str(destination_message_id)
                if destination_message_id is not None
                else None
            ),

        "original_text":
            original_text,

        "processed_text":
            processed_text,

        "status":
            status,

        "blocked_reason":
            blocked_reason,

        "error_message":
            error_message
    }

    try:

        await lovable_request(
            LOGS_ENDPOINT,
            "POST",
            payload
        )

    except Exception as error:

        print(
            "[Logs] Falha:",
            str(error)
        )



# ============================================================
# CONTEÚDO PROTEGIDO / NOFORWARDS
# ============================================================

PROTECTED_CONTENT_PLACEHOLDER = os.getenv(
    "TELEGRAM_PROTECTED_CONTENT_PLACEHOLDER",
    "🔒 Conteúdo protegido na origem. A mídia não pode ser copiada automaticamente."
)

def message_is_protected(message):
    return bool(
        getattr(message, "noforwards", False)
        or getattr(message, "no_forwards", False)
    )


async def publish_text_fallback_for_protected(
    message,
    source_id,
    automation,
    processed_text,
    processed_entities,
    preserve_formatting,
    reason
):
    automation_id = automation["id"]
    destination = automation.get("destination_chat_id")

    if not destination:
        return None

    if str(destination).strip() == str(source_id).strip():
        return None

    destination_entity = await resolve_destination_entity(destination)

    final_text = processed_text or PROTECTED_CONTENT_PLACEHOLDER
    final_entities = (
        processed_entities
        if processed_text and preserve_formatting
        else []
    )

    sent = await client.send_message(
        destination_entity,
        final_text,
        formatting_entities=final_entities
    )

    await remember_self_published(destination, sent.id)

    await save_message_link(
        automation_id=automation_id,
        source_chat_id=source_id,
        source_message_id=message.id,
        destination_chat_id=destination,
        destination_message_id=sent.id
    )

    await send_log(
        automation_id=automation_id,
        source_message_id=message.id,
        destination_message_id=sent.id,
        status="published",
        original_text=message.message or "",
        processed_text=final_text,
        blocked_reason="protected_media",
        error_message=(
            f"{reason}: mídia protegida pela origem; "
            "texto/legenda foi publicado normalmente."
        )
    )

    print(
        "[Protected Chat] Texto/legenda publicado sem mídia:",
        message.id,
        "→",
        sent.id
    )

    return sent


async def publish_album_text_fallback_for_protected(
    caption_message,
    source_id,
    automation,
    grouped_id,
    processed_caption,
    processed_entities,
    preserve_formatting,
    reason
):
    automation_id = automation["id"]
    destination = automation.get("destination_chat_id")

    if not destination:
        return None

    if str(destination).strip() == str(source_id).strip():
        return None

    destination_entity = await resolve_destination_entity(destination)

    final_text = processed_caption or PROTECTED_CONTENT_PLACEHOLDER
    final_entities = (
        processed_entities
        if processed_caption and preserve_formatting
        else []
    )

    sent = await client.send_message(
        destination_entity,
        final_text,
        formatting_entities=final_entities
    )

    await remember_self_published(destination, sent.id)

    await save_message_link(
        automation_id=automation_id,
        source_chat_id=source_id,
        source_message_id=caption_message.id,
        source_grouped_id=grouped_id,
        destination_chat_id=destination,
        destination_message_id=sent.id
    )

    await send_log(
        automation_id=automation_id,
        source_message_id=caption_message.id,
        destination_message_id=sent.id,
        status="published",
        original_text=caption_message.message or "",
        processed_text=final_text,
        blocked_reason="protected_album_media",
        error_message=(
            f"{reason}: álbum protegido pela origem; "
            "texto/legenda foi publicado normalmente."
        )
    )

    print(
        "[Protected Chat] Texto/legenda do álbum publicado sem mídia:",
        grouped_id,
        "→",
        sent.id
    )

    return sent


# ============================================================
# PUBLICAR MENSAGEM NORMAL
# ============================================================

async def publish_single_message(
    message,
    source_id,
    automation
):

    automation_id = automation["id"]

    processing_key = (
        f"single:"
        f"{source_id}:"
        f"{message.id}:"
        f"{automation_id}"
    )

    claimed = await claim_processing_key(
        processing_key
    )

    if not claimed:

        print(
            "[Duplicate] Mensagem já está sendo processada:",
            message.id
        )

        return

    fingerprint = build_message_fingerprint(
        source_id=source_id,
        automation_id=automation_id,
        message=message
    )

    fingerprint_claimed = await claim_fingerprint(fingerprint)

    if not fingerprint_claimed:
        print(
            "[Duplicate Guard] Fingerprint repetido ignorado:",
            fingerprint[:12],
            "| message_id:",
            message.id
        )
        await release_processing_key(processing_key)
        return

    started_at = time.monotonic()

    try:

        # ----------------------------------------------------
        # IDEMPOTÊNCIA
        #
        # Se já existe vínculo source -> destination,
        # esta mensagem já foi publicada antes.
        # ----------------------------------------------------

        existing_links = await find_message_links(

            source_chat_id=
                source_id,

            source_message_id=
                message.id,

            automation_id=
                automation_id
        )

        if existing_links:

            print(
                "[Duplicate] Mensagem já publicada. Ignorando:",
                message.id
            )

            return


        original_text = (
            message.message
            or ""
        )

        (
            processed_text,
            processed_entities
        ) = process_rich_text(

            original_text,
            message.entities or [],
            automation
        )

        if processed_text is None:

            print(
                "[Blacklist] Bloqueada."
            )

            await send_log(

                automation_id=
                    automation_id,

                source_message_id=
                    message.id,

                status="blocked",

                original_text=
                    original_text,

                blocked_reason=
                    "blacklist"
            )

            return


        destination = automation.get(
            "destination_chat_id"
        )

        if not destination:
            return


        if (
            str(destination).strip()
            ==
            str(source_id).strip()
        ):

            return


        destination_entity = (
            await resolve_destination_entity(
                destination
            )
        )


        preserve_media = automation.get(
            "preserve_media",
            True
        )

        preserve_caption = automation.get(
            "preserve_caption",
            True
        )

        preserve_formatting = (
            automation.get(
                "preserve_formatting",
                True
            )
        )

        formatting_entities = (
            processed_entities
            if preserve_formatting
            else []
        )

        if (
            message.media
            and preserve_media
            and message_is_protected(message)
        ):
            await publish_text_fallback_for_protected(
                message=message,
                source_id=source_id,
                automation=automation,
                processed_text=(processed_text if preserve_caption else ""),
                processed_entities=(processed_entities if preserve_caption else []),
                preserve_formatting=preserve_formatting,
                reason="noforwards"
            )
            return


        try:

            # ------------------------------------------------
            # MÍDIA
            # ------------------------------------------------

            if (
                message.media
                and
                preserve_media
            ):

                print(
                    "[Media] Iniciando envio:",
                    message.id
                )

                async with MEDIA_SEND_SEMAPHORE:

                    sent = await client.send_file(

                        destination_entity,

                        message.media,

                        caption=(
                            processed_text
                            if preserve_caption
                            else ""
                        ),

                        formatting_entities=(
                            formatting_entities
                            if preserve_caption
                            else []
                        )
                    )


            # ------------------------------------------------
            # TEXTO
            # ------------------------------------------------

            else:

                if not processed_text:
                    return

                sent = await client.send_message(

                    destination_entity,

                    processed_text,

                    formatting_entities=
                        formatting_entities
                )


        except ChatForwardsRestrictedError:

            print(
                "[Protected Chat] Telegram bloqueou a mídia. "
                "Publicando apenas texto/legenda processado."
            )

            await publish_text_fallback_for_protected(
                message=message,
                source_id=source_id,
                automation=automation,
                processed_text=(processed_text if preserve_caption else ""),
                processed_entities=(processed_entities if preserve_caption else []),
                preserve_formatting=preserve_formatting,
                reason="ChatForwardsRestrictedError"
            )

            return


        elapsed = (
            time.monotonic()
            -
            started_at
        )

        print(
            "[Publish] Publicada:",
            message.id,
            "→",
            sent.id,
            f"({elapsed:.2f}s)"
        )

        await remember_self_published(
            destination,
            sent.id
        )


        # ----------------------------------------------------
        # PERSISTIR VÍNCULO ANTES DE ENCERRAR O HANDLER
        # ----------------------------------------------------

        await save_message_link(

            automation_id=
                automation_id,

            source_chat_id=
                source_id,

            source_message_id=
                message.id,

            destination_chat_id=
                destination,

            destination_message_id=
                sent.id
        )


        await send_log(

            automation_id=
                automation_id,

            source_message_id=
                message.id,

            destination_message_id=
                sent.id,

            status="published",

            original_text=
                original_text,

            processed_text=
                processed_text
        )


    finally:

        await release_processing_key(
            processing_key
        )


# ============================================================
# NOVA MENSAGEM
# ============================================================

@client.on(
    events.NewMessage()
)
async def new_message_handler(
    event
):
    """
    Captura TODA mensagem nova entregue à sessão Telethon,
    seja enviada pela própria conta ou por terceiros.

    Não usamos incoming=True/outgoing=True aqui porque a origem
    da automação é o CHAT, não o autor. Assim evitamos excluir
    mensagens legítimas de terceiros ou postagens de canal.
    """

    message = event.message
    source_id = event.chat_id

    if source_id is None:

        print(
            "[Event] NewMessage sem chat_id. Ignorada."
        )

        return

    if EVENT_DEBUG:

        sender_id = getattr(
            event,
            "sender_id",
            None
        )

        print(
            "\n================ EVENTO TELEGRAM ================"
        )

        print(
            "[Event] Tipo: NewMessage"
        )

        print(
            "[Event] Chat ID:",
            source_id
        )

        print(
            "[Event] Sender ID:",
            sender_id
        )

        print(
            "[Event] Message ID:",
            getattr(
                message,
                "id",
                None
            )
        )

        print(
            "[Event] Outgoing:",
            bool(
                getattr(
                    event,
                    "out",
                    False
                )
            )
        )

        print(
            "[Event] Grouped ID:",
            getattr(
                message,
                "grouped_id",
                None
            )
        )

        preview = (
            getattr(
                message,
                "message",
                ""
            )
            or ""
        )

        print(
            "[Event] Texto:",
            repr(
                preview[:300]
            )
        )

        print(
            "=================================================="
        )

    # event.out=True também pode acontecer quando a própria
    # conta conectada publica MANUALMENTE numa origem.
    # Portanto NÃO descartamos todas as mensagens outgoing.
    #
    # Só descartamos mensagens que sabemos que foram criadas
    # pelo próprio Worker em um destino, evitando cascata.
    if getattr(
        event,
        "out",
        False
    ):

        if await is_self_published(
            source_id,
            message.id
        ):

            print(
                "[Self Published] Mensagem criada pelo Worker "
                "ignorada para evitar cascata."
            )

            return

        print(
            "[Own Message] Mensagem da própria conta detectada. "
            "Processando normalmente."
        )

    # Álbum é processado pelo events.Album.
    if getattr(
        message,
        "grouped_id",
        None
    ):

        if EVENT_DEBUG:

            print(
                "[Event] Mensagem pertence a álbum. "
                "Aguardando Album handler."
            )

        return

    matches = (
        await get_matching_automations(
            source_id
        )
    )

    if not matches:
        return

    print(
        "[NewMessage] Automação encontrada. "
        "Iniciando processamento:",
        source_id,
        message.id
    )

    for automation in matches:

        try:

            print(
                "[NewMessage] Executando automação:",
                automation.get(
                    "name"
                )
                or automation.get(
                    "id"
                )
            )

            await publish_single_message(
                message,
                source_id,
                automation
            )

        except Exception as error:

            print(
                "[NewMessage] ERRO:",
                type(error).__name__,
                str(error)
            )


# ============================================================
# ÁLBUM
# ============================================================

@client.on(
    events.Album()
)
async def album_handler(
    event
):

    source_id = event.chat_id

    # Álbum manual da própria conta em um canal de origem
    # também deve ser processado. Só ignoramos o que o Worker
    # acabou de publicar num destino.
    if getattr(event, "out", False):

        if await is_self_published_album(
            source_id,
            event.messages
        ):
            print(
                "[Self Published] Álbum criado pelo Worker "
                "ignorado para evitar cascata."
            )
            return

        print(
            "[Own Album] Álbum manual da conta detectado. "
            "Processando normalmente."
        )

    matches = (
        await get_matching_automations(
            source_id
        )
    )

    if not matches:
        return


    media_messages = [
        message
        for message
        in event.messages
        if message.media
    ]

    if not media_messages:
        return


    caption_index = 0
    caption_message = (
        media_messages[0]
    )


    for index, message in enumerate(
        media_messages
    ):

        if (
            message.message
            and
            message.message.strip()
        ):

            caption_index = index
            caption_message = message
            break


    original_caption = (
        caption_message.message
        or ""
    )

    original_entities = (
        caption_message.entities
        or []
    )

    medias = [
        message.media
        for message
        in media_messages
    ]


    for automation in matches:

        automation_id = automation["id"]

        processing_key = (
            f"album:"
            f"{source_id}:"
            f"{event.grouped_id}:"
            f"{automation_id}"
        )

        claimed = await claim_processing_key(
            processing_key
        )

        if not claimed:

            print(
                "[Duplicate] Álbum já está sendo processado:",
                event.grouped_id
            )

            continue

        fingerprint = build_album_fingerprint(
            source_id=source_id,
            automation_id=automation_id,
            grouped_id=event.grouped_id,
            messages=media_messages
        )

        fingerprint_claimed = await claim_fingerprint(fingerprint)

        if not fingerprint_claimed:
            print(
                "[Duplicate Guard] Álbum repetido ignorado:",
                fingerprint[:12],
                "| grouped_id:",
                event.grouped_id
            )
            await release_processing_key(processing_key)
            continue

        started_at = time.monotonic()

        try:

            # ------------------------------------------------
            # IDEMPOTÊNCIA DO ÁLBUM
            #
            # Se qualquer item principal já estiver vinculado,
            # tratamos o álbum como já publicado.
            # ------------------------------------------------

            existing_links = await find_message_links(

                source_chat_id=
                    source_id,

                source_message_id=
                    caption_message.id,

                automation_id=
                    automation_id
            )

            if existing_links:

                print(
                    "[Duplicate] Álbum já publicado. Ignorando:",
                    event.grouped_id
                )

                continue


            (
                processed_caption,
                processed_entities
            ) = process_rich_text(

                original_caption,
                original_entities,
                automation
            )


            if processed_caption is None:

                print(
                    "[Blacklist] Álbum bloqueado."
                )

                await send_log(

                    automation_id=
                        automation_id,

                    source_message_id=
                        caption_message.id,

                    status="blocked",

                    original_text=
                        original_caption,

                    blocked_reason=
                        "blacklist"
                )

                continue


            destination = (
                automation.get(
                    "destination_chat_id"
                )
            )

            if not destination:
                continue


            if (
                str(destination).strip()
                ==
                str(source_id).strip()
            ):

                continue


            destination_entity = (
                await resolve_destination_entity(
                    destination
                )
            )


            preserve_media = (
                automation.get(
                    "preserve_media",
                    True
                )
            )

            preserve_caption = (
                automation.get(
                    "preserve_caption",
                    True
                )
            )

            preserve_formatting = (
                automation.get(
                    "preserve_formatting",
                    True
                )
            )


            # ------------------------------------------------
            # NÃO PRESERVAR MÍDIA
            # ------------------------------------------------

            if not preserve_media:

                if not processed_caption:
                    continue

                sent = await client.send_message(

                    destination_entity,

                    processed_caption,

                    formatting_entities=(
                        processed_entities
                        if preserve_formatting
                        else []
                    )
                )


                await remember_self_published(
                    destination,
                    sent.id
                )

                await save_message_link(

                    automation_id=
                        automation_id,

                    source_chat_id=
                        source_id,

                    source_message_id=
                        caption_message.id,

                    source_grouped_id=
                        event.grouped_id,

                    destination_chat_id=
                        destination,

                    destination_message_id=
                        sent.id
                )

                continue


            captions = [
                ""
                for _
                in media_messages
            ]

            album_entities = [
                []
                for _
                in media_messages
            ]


            if preserve_caption:

                captions[
                    caption_index
                ] = processed_caption

                if preserve_formatting:

                    album_entities[
                        caption_index
                    ] = processed_entities


            if any(
                message_is_protected(item)
                for item in media_messages
            ):
                print(
                    "[Protected Chat] Álbum protegido detectado. "
                    "Publicando apenas texto/legenda processado."
                )

                await publish_album_text_fallback_for_protected(
                    caption_message=caption_message,
                    source_id=source_id,
                    automation=automation,
                    grouped_id=event.grouped_id,
                    processed_caption=(processed_caption if preserve_caption else ""),
                    processed_entities=(processed_entities if preserve_caption else []),
                    preserve_formatting=preserve_formatting,
                    reason="noforwards"
                )

                continue


            try:

                print(
                    "[Album] Enviando:",
                    event.grouped_id,
                    "| mídias:",
                    len(medias)
                )

                async with MEDIA_SEND_SEMAPHORE:

                    sent_messages = (
                        await client.send_file(

                            destination_entity,

                            medias,

                            caption=
                                captions,

                            formatting_entities=
                                album_entities
                        )
                    )


            except ChatForwardsRestrictedError:

                print(
                    "[Protected Chat] Telegram bloqueou o álbum. "
                    "Publicando apenas texto/legenda processado."
                )

                await publish_album_text_fallback_for_protected(
                    caption_message=caption_message,
                    source_id=source_id,
                    automation=automation,
                    grouped_id=event.grouped_id,
                    processed_caption=(processed_caption if preserve_caption else ""),
                    processed_entities=(processed_entities if preserve_caption else []),
                    preserve_formatting=preserve_formatting,
                    reason="ChatForwardsRestrictedError"
                )

                continue



            if not isinstance(
                sent_messages,
                list
            ):

                sent_messages = [
                    sent_messages
                ]


            elapsed = (
                time.monotonic()
                -
                started_at
            )

            print(
                "[Album] Publicado:",
                len(sent_messages),
                "itens",
                f"({elapsed:.2f}s)"
            )


            # ------------------------------------------------
            # VINCULAR CADA ITEM
            # ------------------------------------------------

            for (
                source_message,
                destination_message
            ) in zip(
                media_messages,
                sent_messages
            ):

                await remember_self_published(
                    destination,
                    destination_message.id
                )

                await save_message_link(

                    automation_id=
                        automation_id,

                    source_chat_id=
                        source_id,

                    source_message_id=
                        source_message.id,

                    source_grouped_id=
                        event.grouped_id,

                    destination_chat_id=
                        destination,

                    destination_message_id=
                        destination_message.id
                )


            await send_log(

                automation_id=
                    automation_id,

                source_message_id=
                    caption_message.id,

                destination_message_id=(
                    sent_messages[0].id
                    if sent_messages
                    else None
                ),

                status="published",

                original_text=
                    original_caption,

                processed_text=
                    processed_caption
            )


        finally:

            await release_processing_key(
                processing_key
            )


# ============================================================
# EDITAR MENSAGEM
# ============================================================

@client.on(
    events.MessageEdited()
)
async def edited_message_handler(
    event
):

    source_id = event.chat_id
    message = event.message

    if source_id is None:
        return

    matches = (
        await get_matching_automations(
            source_id
        )
    )

    if not matches:
        return

    print(
        "[Edit] Mensagem editada:",
        source_id,
        message.id
    )

    for automation in matches:

        links = await find_message_links(

            source_chat_id=
                source_id,

            source_message_id=
                message.id,

            automation_id=
                automation["id"]
        )

        if not links:

            print(
                "[Edit] Nenhum vínculo encontrado "
                "para a mensagem."
            )

            continue

        (
            processed_text,
            processed_entities
        ) = process_rich_text(

            message.message or "",

            message.entities or [],

            automation
        )

        # Se edição passou a bater
        # na blacklist, apagar destino.

        if processed_text is None:

            for link in links:

                try:

                    destination_entity = (
                        await resolve_destination_entity(
                            link[
                                "destination_chat_id"
                            ]
                        )
                    )

                    await client.delete_messages(

                        destination_entity,

                        [
                            int(
                                link[
                                    "destination_message_id"
                                ]
                            )
                        ],

                        revoke=True
                    )

                except Exception as error:

                    print(
                        "[Edit] Erro apagando "
                        "mensagem bloqueada:",
                        str(error)
                    )

            continue

        preserve_caption = (
            automation.get(
                "preserve_caption",
                True
            )
        )

        preserve_formatting = (
            automation.get(
                "preserve_formatting",
                True
            )
        )

        # Se for mídia e preserve_caption=False,
        # não existe legenda espelhada para editar.

        if (
            message.media
            and
            not preserve_caption
        ):

            continue

        for link in links:

            try:

                destination_entity = (
                    await resolve_destination_entity(
                        link[
                            "destination_chat_id"
                        ]
                    )
                )

                destination_message_id = int(
                    link[
                        "destination_message_id"
                    ]
                )

                await client.edit_message(

                    destination_entity,

                    destination_message_id,

                    processed_text,

                    formatting_entities=(
                        processed_entities
                        if preserve_formatting
                        else []
                    )
                )

                print(
                    "[Edit] Atualizada:",
                    message.id,
                    "→",
                    destination_message_id
                )

                await send_log(

                    automation_id=
                        automation["id"],

                    source_message_id=
                        message.id,

                    destination_message_id=
                        destination_message_id,

                    status="edited",

                    original_text=
                        message.message or "",

                    processed_text=
                        processed_text
                )

            except Exception as error:

                print(
                    "[Edit] ERRO:",
                    type(error).__name__,
                    str(error)
                )


# ============================================================
# EXCLUIR MENSAGEM
# ============================================================

@client.on(
    events.MessageDeleted()
)
async def deleted_message_handler(
    event
):

    source_id = event.chat_id

    print(
        "[Delete] Evento recebido. Chat:",
        source_id,
        "IDs:",
        event.deleted_ids
    )

    # Em canais/supergrupos normalmente
    # temos chat_id.
    #
    # Se vier None, não vamos apagar no
    # escuro para evitar excluir mensagem
    # errada em outro chat.

    if source_id is None:

        print(
            "[Delete] chat_id ausente. "
            "Exclusão ignorada por segurança."
        )

        return

    for source_message_id in (
        event.deleted_ids
    ):

        links = await find_message_links(

            source_chat_id=
                source_id,

            source_message_id=
                source_message_id
        )

        if not links:

            print(
                "[Delete] Nenhum vínculo:",
                source_message_id
            )

            continue

        # Agrupar por destino.

        grouped_destinations = (
            defaultdict(list)
        )

        for link in links:

            grouped_destinations[
                str(
                    link[
                        "destination_chat_id"
                    ]
                )
            ].append(
                int(
                    link[
                        "destination_message_id"
                    ]
                )
            )

        for (
            destination_chat_id,
            destination_ids
        ) in grouped_destinations.items():

            try:

                destination_entity = (
                    await resolve_destination_entity(
                        destination_chat_id
                    )
                )

                await client.delete_messages(

                    destination_entity,

                    destination_ids,

                    revoke=True
                )

                print(
                    "[Delete] Excluída(s) no destino:",
                    destination_ids
                )

            except Exception as error:

                print(
                    "[Delete] ERRO:",
                    type(error).__name__,
                    str(error)
                )

        for link in links:

            await send_log(

                automation_id=
                    link[
                        "automation_id"
                    ],

                source_message_id=
                    source_message_id,

                destination_message_id=
                    link[
                        "destination_message_id"
                    ],

                status="deleted"
            )

        await remove_message_links(

            source_chat_id=
                source_id,

            source_message_id=
                source_message_id
        )


# ============================================================
# RECUPERAÇÃO DE MENSAGENS PERDIDAS DA FONTE
# ============================================================

def load_source_recovery_state():
    try:
        with open(
            SOURCE_RECOVERY_STATE_FILE,
            "r",
            encoding="utf-8"
        ) as state_file:
            raw_state = json.load(state_file)

        if not isinstance(raw_state, dict):
            return {}

        state = {}

        for source_id, message_id in raw_state.items():
            try:
                state[str(source_id).strip()] = int(message_id)
            except (TypeError, ValueError):
                continue

        return state

    except FileNotFoundError:
        return {}

    except Exception as error:
        print(
            "[Recovery] Estado inválido. Iniciando novo:",
            type(error).__name__,
            str(error)
        )
        return {}


def save_source_recovery_state(state):
    state_directory = os.path.dirname(
        os.path.abspath(SOURCE_RECOVERY_STATE_FILE)
    )
    os.makedirs(state_directory, exist_ok=True)

    temporary_path = (
        SOURCE_RECOVERY_STATE_FILE
        + ".tmp"
    )

    with open(
        temporary_path,
        "w",
        encoding="utf-8"
    ) as state_file:
        json.dump(
            state,
            state_file,
            ensure_ascii=False,
            indent=2,
            sort_keys=True
        )
        state_file.flush()
        os.fsync(state_file.fileno())

    os.replace(
        temporary_path,
        SOURCE_RECOVERY_STATE_FILE
    )


def active_source_ids(automations):
    sources = []
    seen = set()

    for automation in automations:
        source = automation.get("source_chat_id")

        if source is None:
            continue

        source_text = str(source).strip()

        if not source_text or source_text in seen:
            continue

        seen.add(source_text)
        sources.append(source_text)

    return sources


def source_id_value(source_text):
    if source_text.lstrip("-").isdigit():
        return int(source_text)

    return source_text


async def recover_source_messages(
    source_text,
    last_message_id
):
    source_id = source_id_value(source_text)
    source_entity = await resolve_destination_entity(
        source_id
    )

    recovered_messages = []

    async for message in client.iter_messages(
        source_entity,
        min_id=int(last_message_id),
        limit=SOURCE_RECOVERY_MAX_MESSAGES
    ):
        recovered_messages.append(message)

    if not recovered_messages:

        if EVENT_DEBUG:

            print(
                "[Recovery] Nenhuma mensagem nova | fonte:",
                source_text,
                "| depois do ID:",
                last_message_id
            )

        return int(last_message_id)

    # iter_messages retorna do mais novo para o mais antigo.
    # Processamos na ordem original do canal.
    recovered_messages.sort(
        key=lambda item: int(item.id)
    )

    print(
        "[Recovery] Mensagens recuperadas:",
        len(recovered_messages),
        "| fonte:",
        source_text,
        "| depois do ID:",
        last_message_id
    )

    processed_albums = set()
    newest_message_id = int(last_message_id)

    for message in recovered_messages:
        newest_message_id = max(
            newest_message_id,
            int(message.id)
        )

        grouped_id = getattr(
            message,
            "grouped_id",
            None
        )

        if grouped_id:
            if grouped_id in processed_albums:
                continue

            album_messages = [
                album_message
                for album_message in recovered_messages
                if getattr(
                    album_message,
                    "grouped_id",
                    None
                ) == grouped_id
            ]

            processed_albums.add(grouped_id)

            await album_handler(
                SimpleNamespace(
                    chat_id=source_id,
                    grouped_id=grouped_id,
                    messages=album_messages,
                    out=any(
                        bool(getattr(item, "out", False))
                        for item in album_messages
                    )
                )
            )

            continue

        await new_message_handler(
            SimpleNamespace(
                chat_id=source_id,
                message=message,
                out=bool(getattr(message, "out", False))
            )
        )

    return newest_message_id


async def source_recovery_loop():
    state = load_source_recovery_state()

    print(
        "[Recovery] Ativo. Intervalo:",
        SOURCE_RECOVERY_INTERVAL,
        "segundos"
    )

    while True:
        try:
            automations = await load_automations()
            sources = active_source_ids(automations)

            for source_text in sources:
                source_id = source_id_value(source_text)
                source_entity = await resolve_destination_entity(
                    source_id
                )

                # Na primeira execução, criamos um marco no último ID atual.
                # Assim o deploy não repassa todo o histórico antigo do canal.
                if source_text not in state:
                    latest_messages = await client.get_messages(
                        source_entity,
                        limit=1
                    )

                    latest_id = (
                        int(latest_messages[0].id)
                        if latest_messages
                        else 0
                    )

                    state[source_text] = latest_id
                    save_source_recovery_state(state)

                    print(
                        "[Recovery] Fonte inicializada:",
                        source_text,
                        "| último ID:",
                        latest_id
                    )
                    continue

                newest_id = await recover_source_messages(
                    source_text,
                    state[source_text]
                )

                if newest_id > state[source_text]:
                    state[source_text] = newest_id
                    save_source_recovery_state(state)

        except Exception as error:
            print(
                "[Recovery] ERRO:",
                type(error).__name__,
                str(error)
            )

        await asyncio.sleep(
            max(5, SOURCE_RECOVERY_INTERVAL)
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    print(
        "================================="
    )

    print(
        " TELEGRAM WORKER"
    )

    print(
        f" VERSION {WORKER_VERSION}"
    )

    print(
        "================================="
    )

    print(
        "[Worker] Conectando..."
    )

    # Impede que duas instâncias locais do mesmo Worker
    # publiquem a mesma mensagem em duplicidade.
    acquire_process_lock()

    await client.start()

    me = await client.get_me()

    print(
        "[Telegram] Conectado!"
    )

    print(
        "[Telegram] ID:",
        me.id
    )

    print(
        "[Telegram] Nome:",
        me.first_name
    )

    if me.username:

        print(
            "[Telegram] Username:",
            f"@{me.username}"
        )

    await warm_entity_cache()

    print(
        "[Lovable] Testando..."
    )

    try:

        automations = (
            await load_automations(
                force_refresh=True
            )
        )

        print(
            "[Lovable] Conectado!"
        )

        print(
            "[Lovable] Automações ativas:",
            len(automations)
        )

        debug_automations(
            automations
        )

    except Exception as error:

        print(
            "[Lovable] ERRO:",
            type(error).__name__,
            str(error)
        )

    try:

        await send_heartbeat()

    except Exception as error:

        print(
            "[Heartbeat] inicial falhou:",
            str(error)
        )

    try:

        await sync_telegram_chats()

    except Exception as error:

        print(
            "[Chats] sync inicial falhou:",
            str(error)
        )

    asyncio.create_task(
        heartbeat_loop()
    )

    asyncio.create_task(
        chat_sync_loop()
    )

    asyncio.create_task(
        cache_maintenance_loop()
    )

    asyncio.create_task(
        source_recovery_loop()
    )

    print(
        "================================="
    )

    print(
        "[Worker] ONLINE"
    )

    print(
        "[Worker] Event debug:",
        "ATIVO" if EVENT_DEBUG else "DESATIVADO"
    )

    print(
        "[Worker] Listener NewMessage universal: ATIVO"
    )

    print(
        "[Worker] Álbuns: ATIVO"
    )

    print(
        "[Worker] Formatação: ATIVO"
    )

    print(
        "[Worker] Hyperlink replace: ATIVO"
    )

    print(
        "[Worker] Edição sincronizada: ATIVO"
    )

    print(
        "[Worker] Exclusão sincronizada: ATIVO"
    )

    print(
        "[Worker] Anti-duplicação: ATIVO"
    )

    print(
        "[Worker] Dedupe por fingerprint: ATIVO"
    )

    print(
        "[Worker] Own-message guard inteligente: ATIVO"
    )

    print(
        "[Worker] Recuperação de fontes sem depender do Mac: ATIVO"
    )

    print(
        "[Worker] Proteção de mídia restrita: ATIVO"
    )

    print(
        "[Worker] Fallback texto/legenda para mídia protegida: ATIVO"
    )

    print(
        "[Worker] Cache com TTL/LRU: ATIVO"
    )

    print(
        "[Worker] HTTP Keep-Alive: ATIVO"
    )

    print(
        "[Worker] Upload de mídia serializado: ATIVO"
    )

    print(
        "================================="
    )

    try:
        await client.run_until_disconnected()
    finally:
        await close_http_client()


if __name__ == "__main__":

    asyncio.run(
        main()
    )
