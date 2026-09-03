import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient


load_dotenv()


async def main():
    if len(sys.argv) < 2:
        print("Uso: python create_user_session.py <nome_da_sessao>")
        print("Exemplo: python create_user_session.py telegram_marca_b")
        raise SystemExit(2)

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session_name = sys.argv[1].strip()

    if not session_name:
        raise SystemExit("Nome da sessão não pode ser vazio")

    client = TelegramClient(session_name, api_id, api_hash)

    try:
        await client.start()
        me = await client.get_me()
        print("Sessão criada com sucesso.")
        print("Telegram user id:", me.id)
        print("Username:", f"@{me.username}" if me.username else "sem username")
        print("Arquivo base da sessão:", session_name)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
