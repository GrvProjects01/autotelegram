import asyncio
from types import SimpleNamespace

import httpx

import http_resilience_addon as addon
from pool_watchdog import PoolWatchdog


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _fail_every(watchdog, clock, seconds, total):
    actions = []
    for elapsed in range(0, total + 1, seconds):
        clock.now = 1000.0 + elapsed
        action = watchdog.record_pool_timeout()
        if action:
            actions.append((elapsed, action))
    return actions


def test_continuous_stall_recycles_then_exits_once():
    clock = Clock()
    watchdog = PoolWatchdog(clock=clock)
    assert _fail_every(watchdog, clock, 10, 400) == [
        (90, "recycle"),
        (180, "recycle"),
        (270, "recycle"),
        (300, "exit"),
    ]


def test_any_response_resets_the_stall():
    clock = Clock()
    watchdog = PoolWatchdog(clock=clock)
    _fail_every(watchdog, clock, 10, 80)
    watchdog.record_response()
    clock.now += 100
    assert watchdog.record_pool_timeout() is None
    assert watchdog.failures == 1


def test_sporadic_timeouts_never_trigger():
    clock = Clock()
    watchdog = PoolWatchdog(clock=clock)
    assert _fail_every(watchdog, clock, 600, 36000) == []


def test_burst_shorter_than_threshold_does_nothing():
    clock = Clock()
    watchdog = PoolWatchdog(clock=clock)
    assert [watchdog.record_pool_timeout() for _ in range(500)] == [None] * 500


async def _stalled_pool_scenario(monkeypatch):
    """Trava real: pool de 1 conexao ocupada por um pedido que nunca responde."""
    release = asyncio.Event()

    async def handle(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        if b"/hang" in request:
            await release.wait()
        else:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    url = "http://127.0.0.1:%d" % server.sockets[0].getsockname()[1]
    clients = []

    def tiny_client(*, heartbeat=False):
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(5, pool=0.05, read=30),
            limits=httpx.Limits(max_connections=1),
            trust_env=False,
        )
        clients.append(client)
        return client

    clock = Clock()
    monkeypatch.setattr(addon, "_new_client", tiny_client)
    monkeypatch.setattr(addon, "_PRIMARY_CLIENT", None)
    monkeypatch.setattr(addon, "_WATCHDOG", PoolWatchdog(min_failures=2, clock=clock))
    worker = SimpleNamespace(
        WORKER_API_URL=url, WORKER_SECRET="x", HEARTBEAT_ENDPOINT="/heartbeat"
    )

    first = await addon._get_client()
    stuck = asyncio.ensure_future(first.get(url + "/hang"))
    await asyncio.sleep(0.05)

    failures = 0
    for _ in range(3):
        try:
            await addon._resilient_request(worker, "test", "/logs", "POST", {})
        except httpx.PoolTimeout:
            failures += 1
        clock.now += 100
        if addon._PRIMARY_CLIENT is None:
            break

    recovered = await addon._resilient_request(worker, "test", "/ok")

    release.set()
    await asyncio.gather(stuck, return_exceptions=True)
    for client in clients:
        await client.aclose()
    server.close()
    return failures, len(clients), recovered


def test_real_stalled_pool_recovers_by_recycling(monkeypatch):
    failures, created, recovered = asyncio.run(_stalled_pool_scenario(monkeypatch))
    assert failures >= 1
    assert created == 2
    assert recovered == {}
