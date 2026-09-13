"""Resiliencia HTTP para o worker Telegram.

Evita tempestade de requests ao endpoint de automacoes, isola heartbeat do pool
principal e usa cache stale quando o backend tiver falhas transitórias.

Esta camada e registrada pelo entrypoint multi-sessao depois que session_worker
carrega o worker base. Nao altera contratos HTTP nem schema do Lovable.
"""

import asyncio
import os
import time

import httpx


_REGISTERED = False
_PRIMARY_CLIENT = None
_HEARTBEAT_CLIENT = None
_AUTOMATIONS_LOCK = None


def _env_int(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _new_client(*, heartbeat=False):
    if heartbeat:
        max_connections = _env_int("TELEGRAM_HEARTBEAT_HTTP_MAX_CONNECTIONS", 4, 2, 20)
        keepalive = _env_int("TELEGRAM_HEARTBEAT_HTTP_KEEPALIVE", 2, 1, 10)
    else:
        max_connections = _env_int("TELEGRAM_HTTP_MAX_CONNECTIONS", 30, 10, 100)
        keepalive = _env_int("TELEGRAM_HTTP_KEEPALIVE", 15, 5, 50)

    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=15,
            read=60,
            write=60,
            pool=20,
        ),
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=min(keepalive, max_connections),
        ),
    )


async def _get_client(*, heartbeat=False):
    global _PRIMARY_CLIENT, _HEARTBEAT_CLIENT

    if heartbeat:
        if _HEARTBEAT_CLIENT is None or _HEARTBEAT_CLIENT.is_closed:
            _HEARTBEAT_CLIENT = _new_client(heartbeat=True)
        return _HEARTBEAT_CLIENT

    if _PRIMARY_CLIENT is None or _PRIMARY_CLIENT.is_closed:
        _PRIMARY_CLIENT = _new_client(heartbeat=False)
    return _PRIMARY_CLIENT


def _safe_to_retry(method, path, worker):
    method = str(method or "GET").upper()
    if method == "GET":
        return True
    return path == worker.HEARTBEAT_ENDPOINT


def _request_was_not_sent(error):
    return isinstance(
        error,
        (
            httpx.PoolTimeout,
            httpx.ConnectTimeout,
            httpx.ConnectError,
        ),
    )


async def _perform_request(worker, path, method="GET", data=None):
    method = str(method or "GET").upper()
    heartbeat = path == worker.HEARTBEAT_ENDPOINT
    client = await _get_client(heartbeat=heartbeat)

    headers = {
        "x-worker-secret": worker.WORKER_SECRET,
        "Content-Type": "application/json",
    }
    url = f"{worker.LOVABLE_API_URL}{path}"

    if method == "POST":
        response = await client.post(url, json=data, headers=headers)
    elif method == "PUT":
        response = await client.put(url, json=data, headers=headers)
    elif method == "DELETE":
        response = await client.request("DELETE", url, json=data, headers=headers)
    else:
        response = await client.get(url, headers=headers)

    response.raise_for_status()
    if not response.content:
        return {}
    return response.json()


async def _resilient_request(worker, session_key, path, method="GET", data=None):
    method = str(method or "GET").upper()
    safe_retry = _safe_to_retry(method, path, worker)
    max_attempts = 3 if safe_retry else 2

    for attempt in range(1, max_attempts + 1):
        try:
            return await _perform_request(worker, path, method, data)

        except httpx.HTTPStatusError as error:
            status = int(error.response.status_code)
            retryable_status = status in {502, 503, 504} and safe_retry
            if not retryable_status or attempt >= max_attempts:
                print(
                    f"[HTTP Resilience:{session_key}] HTTP {status} "
                    f"{method} {path} tentativa={attempt}/{max_attempts}"
                )
                raise

        except httpx.RequestError as error:
            can_retry = safe_retry or _request_was_not_sent(error)
            if not can_retry or attempt >= max_attempts:
                print(
                    f"[HTTP Resilience:{session_key}] falha {type(error).__name__} "
                    f"{method} {path} tentativa={attempt}/{max_attempts}"
                )
                raise

        delay = 0.5 * (2 ** (attempt - 1))
        print(
            f"[HTTP Resilience:{session_key}] retry {method} {path} "
            f"em {delay:.1f}s tentativa={attempt + 1}/{max_attempts}"
        )
        await asyncio.sleep(delay)

    raise RuntimeError("HTTP retry loop terminou sem resultado")


async def _close_clients(original_close=None):
    global _PRIMARY_CLIENT, _HEARTBEAT_CLIENT

    clients = [_PRIMARY_CLIENT, _HEARTBEAT_CLIENT]
    _PRIMARY_CLIENT = None
    _HEARTBEAT_CLIENT = None

    for client in clients:
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception:
                pass

    if original_close is not None:
        try:
            await original_close()
        except Exception:
            pass


def register(*, worker, base, session_key):
    """Instala a camada sem alterar o contrato usado pelo restante do worker."""
    global _REGISTERED, _AUTOMATIONS_LOCK

    if _REGISTERED:
        return
    _REGISTERED = True

    ttl = _env_int("TELEGRAM_AUTOMATIONS_CACHE_TTL", 20, 5, 300)
    stale_grace = _env_int("TELEGRAM_AUTOMATIONS_STALE_GRACE", 30, 5, 300)
    worker.AUTOMATIONS_CACHE_TTL = ttl
    _AUTOMATIONS_LOCK = asyncio.Lock()

    original_close = worker.close_http_client

    async def resilient_request(path, method="GET", data=None):
        return await _resilient_request(
            worker,
            session_key,
            path,
            method,
            data,
        )

    async def resilient_load_automations(force_refresh=False):
        now = time.monotonic()
        cache = worker.AUTOMATIONS_CACHE

        if not force_refresh and cache.get("expires_at", 0.0) > now:
            return cache.get("data", []) or []

        async with _AUTOMATIONS_LOCK:
            now = time.monotonic()
            if not force_refresh and cache.get("expires_at", 0.0) > now:
                return cache.get("data", []) or []

            try:
                result = await resilient_request(worker.AUTOMATIONS_ENDPOINT)
                automations = result.get("automations", []) or []
                cache["data"] = automations
                cache["expires_at"] = time.monotonic() + ttl
                print(
                    f"[Automation Cache:{session_key}] refresh ok "
                    f"itens={len(automations)} ttl={ttl}s"
                )
                return automations

            except Exception as error:
                stale = cache.get("data", []) or []
                if stale:
                    cache["expires_at"] = time.monotonic() + stale_grace
                    print(
                        f"[Automation Cache:{session_key}] usando stale cache "
                        f"itens={len(stale)} grace={stale_grace}s "
                        f"erro={type(error).__name__}"
                    )
                    return stale

                # No cold start sem backend, manter o daemon vivo. O recovery e o
                # proximo refresh recuperam as automacoes assim que a API voltar.
                cache["expires_at"] = time.monotonic() + min(5, stale_grace)
                print(
                    f"[Automation Cache:{session_key}] backend indisponivel e sem cache; "
                    f"retornando lista vazia erro={type(error).__name__}"
                )
                return []

    async def close_resilient_clients():
        await _close_clients(original_close)

    async def compatible_get_http_client():
        return await _get_client(heartbeat=False)

    # session_worker.session_lovable_request delega por esta variavel capturada
    # no modulo. Atualiza-la e suficiente para preservar a injecao de session_key.
    base._original_lovable_request = resilient_request
    base._original_load_automations = resilient_load_automations

    # Mantem compatibilidade para chamadas diretas do worker e addons.
    worker.get_http_client = compatible_get_http_client
    worker.close_http_client = close_resilient_clients

    print(
        f"[HTTP Resilience:{session_key}] ativo "
        f"automation_ttl={ttl}s primary_pool=30 heartbeat_pool=4"
    )
