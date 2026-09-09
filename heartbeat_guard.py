"""Protege heartbeat contra rajadas duplicadas no mesmo processo."""

import asyncio
import time


def register(worker, session_key="primary", min_interval_seconds=45):
    original_send_heartbeat = worker.send_heartbeat
    lock = asyncio.Lock()
    state = {"last_success_at": 0.0, "in_flight": False}

    async def guarded_send_heartbeat():
        async with lock:
            now = time.monotonic()
            elapsed = now - float(state["last_success_at"] or 0.0)

            if state["in_flight"]:
                print(
                    f"[Heartbeat Guard:{session_key}] chamada ignorada: heartbeat em andamento"
                )
                return {}

            if state["last_success_at"] and elapsed < float(min_interval_seconds):
                print(
                    f"[Heartbeat Guard:{session_key}] chamada duplicada ignorada "
                    f"elapsed={elapsed:.1f}s"
                )
                return {}

            state["in_flight"] = True
            try:
                result = await original_send_heartbeat()
                state["last_success_at"] = time.monotonic()
                return result
            finally:
                state["in_flight"] = False

    worker.send_heartbeat = guarded_send_heartbeat
    print(
        f"[Heartbeat Guard:{session_key}] ativo intervalo_min={min_interval_seconds}s"
    )
    return original_send_heartbeat
