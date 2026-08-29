"""Entrypoint do worker multi-sessão com backfill histórico programado.

Mantém session_worker.py intacto e adiciona somente a camada opcional de histórico.
"""

import asyncio

import historical_backfill
import session_worker as base


worker = base.worker
SESSION_KEY = base.SESSION_KEY
_original_session_loader = base.load_session_automations


async def history_aware_load_automations(force_refresh=False):
    automations = await _original_session_loader(force_refresh=force_refresh)

    # Contexto local da task de backfill: quando um álbum histórico é processado,
    # restringe o handler somente à automação responsável por aquela fila.
    only_id = historical_backfill.active_automation_id.get()
    if only_id:
        automations = [
            automation
            for automation in automations
            if str(automation.get("id") or "").strip() == str(only_id).strip()
        ]

    return automations


base.load_session_automations = history_aware_load_automations
worker.load_automations = history_aware_load_automations


async def main():
    history_task = asyncio.create_task(
        historical_backfill.run(
            worker=worker,
            load_automations=history_aware_load_automations,
            session_key=SESSION_KEY,
        )
    )

    try:
        await base.main()
    finally:
        history_task.cancel()
        try:
            await history_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
