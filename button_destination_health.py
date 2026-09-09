"""Diagnóstico de botões por automação/destino.

Valida sem publicar mensagens:
- quantidade de botões válidos que o worker recebeu;
- bot selecionado pela automação;
- conexão do bot;
- capacidade do bot de resolver o chat de destino.

O objetivo é parar de mascarar falhas específicas de canal como se fossem
problemas genéricos do worker/AWS.
"""

import asyncio
import time


_LAST_CHECK = {}


def _automation_id(automation):
    return str(automation.get("id") or "unknown").strip()


def _destination(automation):
    return str(automation.get("destination_chat_id") or "").strip()


def _bot_key(publisher, automation):
    try:
        key = publisher._automation_bot_key(automation)
    except Exception:
        key = ""
    if key:
        return key
    if len(getattr(publisher, "bots", {}) or {}) == 1:
        return next(iter(publisher.bots.keys()))
    return ""


async def check_automation(worker, automation, session_key="primary", force=False):
    publisher = worker.button_publisher
    automation_id = _automation_id(automation)
    destination = _destination(automation)

    try:
        buttons = publisher.normalize_buttons(automation)
    except Exception as error:
        print(
            f"[Buttons Health:{session_key}] FAIL automation={automation_id} "
            f"stage=normalize error={type(error).__name__}:{error}"
        )
        return False

    if not buttons:
        return True

    key = f"{automation_id}:{destination}"
    now = time.monotonic()
    if not force and now - _LAST_CHECK.get(key, 0) < 300:
        return True
    _LAST_CHECK[key] = now

    bot_key = _bot_key(publisher, automation)
    if not bot_key:
        print(
            f"[Buttons Health:{session_key}] FAIL automation={automation_id} "
            f"destination={destination} buttons={len(buttons)} stage=bot_key "
            "error=missing_telegram_bot_key"
        )
        return False

    try:
        bot = publisher._get_bot(automation)
    except Exception as error:
        print(
            f"[Buttons Health:{session_key}] FAIL automation={automation_id} "
            f"bot={bot_key} destination={destination} buttons={len(buttons)} "
            f"stage=get_bot error={type(error).__name__}:{error}"
        )
        return False

    try:
        await publisher._resolve_destination(bot, destination)
    except Exception as error:
        print(
            f"[Buttons Health:{session_key}] FAIL automation={automation_id} "
            f"bot={bot_key} destination={destination} buttons={len(buttons)} "
            f"stage=resolve_destination error={type(error).__name__}:{error}"
        )
        return False

    print(
        f"[Buttons Health:{session_key}] OK automation={automation_id} "
        f"bot={bot_key} destination={destination} buttons={len(buttons)}"
    )
    return True


async def check_all(worker, automations, session_key="primary", force=False):
    for automation in automations or []:
        try:
            await check_automation(
                worker,
                automation,
                session_key=session_key,
                force=force,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(
                f"[Buttons Health:{session_key}] erro inesperado:",
                type(error).__name__,
                str(error),
            )
