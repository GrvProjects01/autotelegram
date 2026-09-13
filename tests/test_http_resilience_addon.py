import asyncio
import time
from types import SimpleNamespace

import httpx

import http_resilience_addon as addon


def _reset_addon_state():
    addon._REGISTERED = False
    addon._PRIMARY_CLIENT = None
    addon._HEARTBEAT_CLIENT = None
    addon._AUTOMATIONS_LOCK = None


def _fake_worker():
    async def close_http_client():
        return None

    return SimpleNamespace(
        AUTOMATIONS_CACHE_TTL=5,
        AUTOMATIONS_CACHE={"data": [], "expires_at": 0.0},
        AUTOMATIONS_ENDPOINT="/api/public/worker/automations",
        HEARTBEAT_ENDPOINT="/api/public/worker/heartbeat",
        WORKER_SECRET="test-secret",
        LOVABLE_API_URL="https://example.invalid",
        close_http_client=close_http_client,
        get_http_client=None,
    )


def test_automation_refresh_is_single_flight(monkeypatch):
    _reset_addon_state()
    worker = _fake_worker()
    base = SimpleNamespace(
        _original_lovable_request=None,
        _original_load_automations=None,
    )

    calls = {"count": 0}

    async def fake_resilient_request(worker_arg, session_key, path, method="GET", data=None):
        calls["count"] += 1
        await asyncio.sleep(0.02)
        return {"automations": [{"id": "a1"}]}

    monkeypatch.setattr(addon, "_resilient_request", fake_resilient_request)
    monkeypatch.setenv("TELEGRAM_AUTOMATIONS_CACHE_TTL", "20")

    addon.register(worker=worker, base=base, session_key="primary")

    async def run():
        results = await asyncio.gather(
            *[base._original_load_automations() for _ in range(25)]
        )
        assert all(result == [{"id": "a1"}] for result in results)

    asyncio.run(run())
    assert calls["count"] == 1


def test_automation_refresh_uses_stale_cache_on_pool_timeout(monkeypatch):
    _reset_addon_state()
    worker = _fake_worker()
    worker.AUTOMATIONS_CACHE = {
        "data": [{"id": "cached"}],
        "expires_at": time.monotonic() - 1,
    }
    base = SimpleNamespace(
        _original_lovable_request=None,
        _original_load_automations=None,
    )

    async def failing_request(worker_arg, session_key, path, method="GET", data=None):
        raise httpx.PoolTimeout("pool saturated")

    monkeypatch.setattr(addon, "_resilient_request", failing_request)
    monkeypatch.setenv("TELEGRAM_AUTOMATIONS_STALE_GRACE", "30")

    addon.register(worker=worker, base=base, session_key="primary")

    result = asyncio.run(base._original_load_automations(force_refresh=True))
    assert result == [{"id": "cached"}]
    assert worker.AUTOMATIONS_CACHE["expires_at"] > time.monotonic()


def test_cold_start_backend_failure_returns_empty_instead_of_crashing(monkeypatch):
    _reset_addon_state()
    worker = _fake_worker()
    base = SimpleNamespace(
        _original_lovable_request=None,
        _original_load_automations=None,
    )

    async def failing_request(worker_arg, session_key, path, method="GET", data=None):
        raise httpx.ConnectTimeout("backend unavailable")

    monkeypatch.setattr(addon, "_resilient_request", failing_request)
    addon.register(worker=worker, base=base, session_key="primary")

    result = asyncio.run(base._original_load_automations(force_refresh=True))
    assert result == []
