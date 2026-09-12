"""Entrypoint do worker multi-sessao com backfill historico programado.

Mantem session_worker.py intacto e adiciona camadas isoladas de historico,
rodape, diagnostico, idempotencia duravel, mensagens recorrentes, importacao de
mensagens Telegram e roteamento estrito de publicacao.
"""

import asyncio

import album_buttons_addon
import automation_controls_addon
import button_contract_guard
import button_destination_health
import heartbeat_guard
import historical_backfill
import historical_media_integrity_addon
import media_transport_hardening
import message_footer_addon
import publication_ledger
import recurring_messages_addon
import recurring_session_transport_addon
import recurring_media_addon
import recurring_rich_text_addon
import telegram_message_import_addon
import runtime_safety
import session_worker as base
import strict_publication_router
import video_preview_addon


worker = base.worker
SESSION_KEY = base.SESSION_KEY
_original_session_loader = base.load_session_automations


def _explicit_false(value):
    if value is False:
        return True
    if isinstance(value, (int, float)):
        return value == 0
    return str(value or "").strip().lower() in {
        "0", "false", "no", "off", "nao", "não", "disabled", "inactive"
    }


def _truthy(value):
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {
        "1", "true", "yes", "on", "sim", "enabled", "active", "paused"
    }


def automation_is_active(automation):
    if not isinstance(automation, dict):
        return False

    for key in ("enabled", "is_active", "active"):
        if key in automation and _explicit_false(automation.get(key)):
            return False

    for key in ("paused", "is_paused"):
        if key in automation and _truthy(automation.get(key)):
            return False

    status = str(automation.get("status") or "").strip().lower()
    if status in {
        "paused", "pause", "inactive", "disabled", "stopped", "stop",
        "deleted", "archived", "cancelled", "canceled"
    }:
        return False

    if automation.get("deleted_at") not in (None, ""):
        return False
    if automation.get("archived_at") not in (None, ""):
        return False

    return True


def rotation_batch_by_sort_order(automation, cursor):
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


base._rotation_batch = rotation_batch_by_sort_order


async def history_aware_load_automations(force_refresh=False):
    raw_automations = await _original_session_loader(force_refresh=force_refresh)

    automations = [
        automation
        for automation in raw_automations
        if automation_is_active(automation)
    ]

    skipped = len(raw_automations) - len(automations)
    if force_refresh or skipped:
        print(
            f"[Automation Guard:{SESSION_KEY}] ativas={len(automations)} "
            f"bloqueadas={skipped} recebidas={len(raw_automations)}"
        )

    only_id = historical_backfill.active_automation_id.get()
    if only_id:
        automations = [
            automation
            for automation in automations
            if str(automation.get("id") or "").strip() == str(only_id).strip()
        ]

    try:
        await button_destination_health.check_all(
            worker,
            automations,
            session_key=SESSION_KEY,
            force=force_refresh,
        )
    except Exception as error:
        print(
            f"[Buttons Health:{SESSION_KEY}] falha no health-check:",
            type(error).__name__,
            str(error),
        )

    return automations


base.load_session_automations = history_aware_load_automations
worker.load_automations = history_aware_load_automations

runtime_safety.register(worker=worker, session_key=SESSION_KEY)
publication_ledger.register(worker=worker, session_key=SESSION_KEY)
heartbeat_guard.register(worker=worker, session_key=SESSION_KEY, min_interval_seconds=45)
message_footer_addon.register(worker=worker, session_key=SESSION_KEY)

button_contract_guard.register(worker=worker, session_key=SESSION_KEY)
automation_controls_addon.register(worker=worker, session_key=SESSION_KEY)

strict_publication_router.register(
    worker=worker,
    base=base,
    session_key=SESSION_KEY,
)

# Depois do roteador estrito, protege uploads de mídia do bot: conserva metadata
# original de vídeo e garante que caption esperada não seja perdida silenciosamente.
historical_media_integrity_addon.register(worker=worker, session_key=SESSION_KEY)

# Preserva o thumbnail original do Telegram ANTES do hardening envolver o
# downloader. Assim vídeos reupados por bot e sessão mantêm preview atraente.
video_preview_addon.register(worker=worker, session_key=SESSION_KEY)

# Hardening global para TODAS as publicações de mídia: bot e sessão humana.
# Também move temporários para o EBS e remove o fallback GetDialogsRequest de bots.
media_transport_hardening.register(worker=worker, session_key=SESSION_KEY)

album_buttons_addon.register(worker=worker, session_key=SESSION_KEY)

# Camadas do scheduler recorrente. A ordem importa:
# 1) session adiciona transport=session;
# 2) media adiciona image/video;
# 3) rich text preserva entidades e custom emoji em todos os caminhos.
recurring_session_transport_addon.register()
recurring_media_addon.register()
recurring_rich_text_addon.register(worker)


async def main():
    history_task = asyncio.create_task(
        historical_backfill.run(
            worker=worker,
            load_automations=history_aware_load_automations,
            session_key=SESSION_KEY,
        )
    )
    recurring_task = asyncio.create_task(
        recurring_messages_addon.run(
            worker=worker,
            session_key=SESSION_KEY,
        )
    )
    import_task = asyncio.create_task(
        telegram_message_import_addon.run(
            worker=worker,
            session_key=SESSION_KEY,
        )
    )

    try:
        await base.main()
    finally:
        for task in (history_task, recurring_task, import_task):
            task.cancel()
        for task in (history_task, recurring_task, import_task):
            try:
                await task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
