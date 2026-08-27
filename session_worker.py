"""Executa o worker existente isolado por conta/sessão Telegram.

Cada processo recebe TELEGRAM_SESSION_KEY e TELEGRAM_SESSION_NAME diferentes.
O wrapper preserva o worker.py e filtra as automações para impedir que duas
contas processem a mesma tarefa.

Compatibilidade:
- TELEGRAM_SESSION_IS_DEFAULT=1 faz a sessão atual assumir automações antigas
  que ainda não possuem `telegram_session_key`.
- sessões secundárias só processam automações explicitamente vinculadas à sua chave.
"""

import asyncio
import os
import re

from dotenv import load_dotenv

load_dotenv()

SESSION_KEY = str(os.getenv("TELEGRAM_SESSION_KEY", "primary")).strip().lower()
SESSION_KEY = re.sub(r"[^a-z0-9_.-]+", "_", SESSION_KEY).strip("_.-") or "primary"
IS_DEFAULT = str(os.getenv("TELEGRAM_SESSION_IS_DEFAULT", "0")).strip() == "1"

# Importar depois de carregar o ambiente é intencional: worker.py lê as variáveis
# de sessão no import e cria o TelegramClient correspondente.
import worker  # noqa: E402

_original_load_automations = worker.load_automations
_original_lovable_request = worker.lovable_request


def automation_session_key(automation):
    if not isinstance(automation, dict):
        return ""

    value = (
        automation.get("telegram_session_key")
        or automation.get("source_session_key")
        or automation.get("telegram_account_key")
        or ""
    )

    return str(value).strip().lower()


async def load_session_automations(force_refresh=False):
    automations = await _original_load_automations(force_refresh=force_refresh)
    filtered = []

    for automation in automations:
        configured_key = automation_session_key(automation)

        if configured_key:
            if configured_key == SESSION_KEY:
                filtered.append(automation)
            continue

        # Apenas a sessão marcada como padrão herda tarefas antigas sem chave.
        if IS_DEFAULT:
            filtered.append(automation)

    return filtered


async def session_lovable_request(path, method="GET", data=None):
    """Acrescenta identidade da sessão sem alterar endpoints existentes.

    Heartbeat recebe também a lista PÚBLICA de bots configurados no worker.
    Nenhum token é enviado ao Lovable.
    """
    payload = data

    if isinstance(data, dict):
        payload = dict(data)

        if path == worker.HEARTBEAT_ENDPOINT and method == "POST":
            payload["telegram_session_key"] = SESSION_KEY
            payload["telegram_session_is_default"] = IS_DEFAULT

            try:
                payload["publisher_bots"] = worker.button_publisher.public_bots()
            except Exception:
                payload["publisher_bots"] = []

        elif path == worker.CHATS_SYNC_ENDPOINT and method == "POST":
            payload["telegram_session_key"] = SESSION_KEY

            chats = payload.get("chats", []) or []
            normalized_chats = []

            for chat in chats:
                if isinstance(chat, dict):
                    item = dict(chat)
                    item["telegram_session_key"] = SESSION_KEY
                    normalized_chats.append(item)
                else:
                    normalized_chats.append(chat)

            payload["chats"] = normalized_chats

    return await _original_lovable_request(path, method, payload)


# Substitui somente as bordas necessárias. Handlers, replaces, blacklist,
# recovery, botões, mídia, logs e message-links continuam no worker.py.
worker.load_automations = load_session_automations
worker.lovable_request = session_lovable_request

# Evita colisão entre locks/cache de recuperação de duas sessões no mesmo EC2.
base_worker_id = str(worker.WORKER_ID or "telegram-main")
if not base_worker_id.endswith(f"-{SESSION_KEY}"):
    worker.WORKER_ID = f"{base_worker_id}-{SESSION_KEY}"

recovery_dir = os.path.dirname(os.path.abspath(worker.SOURCE_RECOVERY_STATE_FILE))
worker.SOURCE_RECOVERY_STATE_FILE = os.getenv(
    "TELEGRAM_SOURCE_RECOVERY_STATE_FILE",
    os.path.join(recovery_dir, f"source_recovery_state_{SESSION_KEY}.json")
)


async def main():
    print("=================================")
    print(" TELEGRAM MULTI-SESSION WRAPPER")
    print(f" SESSION KEY: {SESSION_KEY}")
    print(f" SESSION NAME: {worker.SESSION_NAME}")
    print(f" DEFAULT: {IS_DEFAULT}")
    print("=================================")
    await worker.main()


if __name__ == "__main__":
    asyncio.run(main())
