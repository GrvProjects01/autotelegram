"""Entrypoint do worker multi-sessão com backfill histórico programado.

Mantém session_worker.py intacto e adiciona somente a camada opcional de histórico.
Também corrige a ordem da rotação de botões para que `sort_order` defina os
pares/grupos e `row` seja usado apenas como layout visual dentro do post.
"""

import asyncio

import album_buttons_addon
import historical_backfill
import session_worker as base


worker = base.worker
SESSION_KEY = base.SESSION_KEY
_original_session_loader = base.load_session_automations


def rotation_batch_by_sort_order(automation, cursor):
    """Monta os grupos da rotação sem deixar `row` alterar a sequência."""
    buttons = worker.button_publisher.normalize_buttons(automation)
    if not buttons:
        return [], 0

    def rotation_order(item):
        try:
            sort_order = int(item.get("sort_order", 0))
        except (TypeError, ValueError):
            sort_order = 0

        try:
            row = int(item.get("row", 0))
        except (TypeError, ValueError):
            row = 0

        return (sort_order, row)

    buttons = sorted(buttons, key=rotation_order)

    size = base._button_rotation_size(automation)
    groups = [
        buttons[index:index + size]
        for index in range(0, len(buttons), size)
    ]

    if not groups:
        return [], 0

    group_index = int(cursor or 0) % len(groups)
    next_cursor = (group_index + 1) % len(groups)

    selected = groups[group_index]
    print(
        f"[Buttons Rotation:{SESSION_KEY}] grupo={group_index} "
        f"ordens={[item.get('sort_order') for item in selected]} "
        f"rows={[item.get('row') for item in selected]}"
    )

    return selected, next_cursor


# Patch pequeno e isolado: apenas a escolha do grupo da rotação muda.
base._rotation_batch = rotation_batch_by_sort_order


async def history_aware_load_automations(force_refresh=False):
    automations = await _original_session_loader(force_refresh=force_refresh)

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

# Telegram não aceita keyboard inline diretamente em media groups.
# O addon preserva o álbum e envia um CTA separado com o mesmo bot/rotação.
album_buttons_addon.register(worker=worker, session_key=SESSION_KEY)


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
