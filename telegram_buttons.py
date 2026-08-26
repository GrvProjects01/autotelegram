import os
from collections import defaultdict, OrderedDict

from dotenv import load_dotenv
from telethon import TelegramClient, Button


load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BOT_SESSION_NAME = os.getenv(
    "TELEGRAM_BOT_SESSION_NAME",
    "telegram_button_bot"
).strip() or "telegram_button_bot"


class TelegramButtonPublisher:
    """Bot auxiliar usado apenas para mensagens que precisam de inline keyboard.

    O worker principal continua usando a sessão de usuário para leitura, replaces,
    blacklist, recovery e publicações sem botão. O bot só entra no envio/edição
    de mensagens únicas que tenham `buttons` configurado na automação.
    """

    def __init__(self, api_id, api_hash):
        self.token = BOT_TOKEN
        self.client = (
            TelegramClient(BOT_SESSION_NAME, api_id, api_hash)
            if self.token
            else None
        )
        self.entity_cache = OrderedDict()
        self.entity_cache_max = 500

    @property
    def configured(self):
        return bool(self.token and self.client is not None)

    @property
    def available(self):
        return bool(
            self.configured
            and self.client.is_connected()
        )

    async def start(self):
        if not self.configured:
            print(
                "[Buttons] TELEGRAM_BOT_TOKEN não configurado. "
                "Automações continuam funcionando sem botões."
            )
            return False

        try:
            await self.client.start(bot_token=self.token)
            me = await self.client.get_me()
            print(
                "[Buttons] Bot publicador conectado:",
                f"@{me.username}" if me.username else me.id
            )
            await self.warm_entity_cache()
            return True
        except Exception as error:
            print(
                "[Buttons] Falha ao conectar bot publicador:",
                type(error).__name__,
                str(error)
            )
            return False

    async def close(self):
        if self.client is not None and self.client.is_connected():
            await self.client.disconnect()

    async def warm_entity_cache(self):
        if not self.available:
            return

        try:
            async for dialog in self.client.iter_dialogs():
                self._cache_entity(str(dialog.id), dialog.input_entity)
        except Exception as error:
            print(
                "[Buttons] Não foi possível aquecer cache do bot:",
                type(error).__name__,
                str(error)
            )

    def _cache_entity(self, key, value):
        key = str(key).strip()
        if key in self.entity_cache:
            self.entity_cache.pop(key, None)
        self.entity_cache[key] = value
        while len(self.entity_cache) > self.entity_cache_max:
            self.entity_cache.popitem(last=False)

    async def resolve_destination(self, destination_chat_id):
        if not self.available:
            raise RuntimeError("Bot de botões não está conectado")

        destination = str(destination_chat_id).strip()
        cached = self.entity_cache.get(destination)
        if cached is not None:
            self.entity_cache.move_to_end(destination)
            return cached

        try:
            entity = await self.client.get_input_entity(int(destination))
            self._cache_entity(destination, entity)
            return entity
        except Exception:
            pass

        async for dialog in self.client.iter_dialogs():
            dialog_id = str(dialog.id)
            self._cache_entity(dialog_id, dialog.input_entity)
            if dialog_id == destination:
                return dialog.input_entity

        raise ValueError(
            "Bot não consegue resolver o canal de destino "
            f"{destination}. Adicione o bot como administrador do canal "
            "com permissão para publicar mensagens."
        )

    @staticmethod
    def normalize_buttons(automation):
        raw_buttons = automation.get("buttons", []) or []
        normalized = []

        for index, item in enumerate(raw_buttons):
            if not isinstance(item, dict):
                continue
            if item.get("enabled", True) is False:
                continue

            text = str(
                item.get("text")
                or item.get("label")
                or ""
            ).strip()
            url = str(item.get("url") or "").strip()

            if not text or not url:
                continue
            if not url.lower().startswith(("http://", "https://", "tg://")):
                print("[Buttons] URL ignorada por protocolo inválido:", url)
                continue

            try:
                row = int(
                    item.get(
                        "row",
                        item.get("row_index", index)
                    )
                )
            except (TypeError, ValueError):
                row = index

            try:
                sort_order = int(item.get("sort_order", index))
            except (TypeError, ValueError):
                sort_order = index

            normalized.append({
                "text": text,
                "url": url,
                "row": max(0, row),
                "sort_order": sort_order,
            })

        normalized.sort(
            key=lambda button: (
                button["row"],
                button["sort_order"]
            )
        )
        return normalized

    @classmethod
    def build_keyboard(cls, automation):
        normalized = cls.normalize_buttons(automation)
        if not normalized:
            return []

        rows = defaultdict(list)
        for button in normalized:
            rows[button["row"]].append(
                Button.url(button["text"], button["url"])
            )

        return [rows[row] for row in sorted(rows.keys())]

    @classmethod
    def has_buttons(cls, automation):
        return bool(cls.normalize_buttons(automation))

    async def send_text(
        self,
        destination_chat_id,
        text,
        entities,
        automation,
    ):
        destination = await self.resolve_destination(destination_chat_id)
        keyboard = self.build_keyboard(automation)
        return await self.client.send_message(
            destination,
            text,
            formatting_entities=entities or [],
            buttons=keyboard,
        )

    async def send_file(
        self,
        destination_chat_id,
        file_path,
        caption,
        entities,
        automation,
    ):
        destination = await self.resolve_destination(destination_chat_id)
        keyboard = self.build_keyboard(automation)
        return await self.client.send_file(
            destination,
            file_path,
            caption=caption or "",
            formatting_entities=entities or [],
            buttons=keyboard,
        )

    async def edit_message(
        self,
        destination_chat_id,
        destination_message_id,
        text,
        entities,
        automation,
    ):
        destination = await self.resolve_destination(destination_chat_id)
        keyboard = self.build_keyboard(automation)
        return await self.client.edit_message(
            destination,
            int(destination_message_id),
            text,
            formatting_entities=entities or [],
            buttons=keyboard,
        )

    async def delete_messages(
        self,
        destination_chat_id,
        message_ids,
    ):
        destination = await self.resolve_destination(destination_chat_id)
        return await self.client.delete_messages(
            destination,
            [int(message_id) for message_id in message_ids],
            revoke=True,
        )
