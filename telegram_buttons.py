import json
import os
import re
from collections import defaultdict, OrderedDict

from dotenv import load_dotenv
from telethon import TelegramClient, Button


load_dotenv()

LEGACY_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
LEGACY_BOT_SESSION_NAME = os.getenv(
    "TELEGRAM_BOT_SESSION_NAME",
    "telegram_button_bot"
).strip() or "telegram_button_bot"

BOTS_JSON = os.getenv("TELEGRAM_BOTS_JSON", "").strip()
BOT_SESSION_DIR = os.getenv(
    "TELEGRAM_BOT_SESSION_DIR",
    os.path.dirname(os.path.abspath(__file__))
).strip() or os.path.dirname(os.path.abspath(__file__))


class TelegramButtonPublisher:
    """Pool de bots publicadores selecionáveis por automação.

    Tokens ficam somente no ambiente privado da AWS em TELEGRAM_BOTS_JSON.
    O Lovable guarda/retorna apenas a chave do bot em `telegram_bot_key`.

    Formatos aceitos em TELEGRAM_BOTS_JSON:

    Simples:
        {"north":"123:ABC", "marca_b":"456:DEF"}

    Com metadata:
        {
          "north": {"token":"123:ABC", "name":"North Finance"},
          "marca_b": {"token":"456:DEF", "name":"Marca B"}
        }

    Compatibilidade: se TELEGRAM_BOTS_JSON não existir, TELEGRAM_BOT_TOKEN
    continua funcionando como bot único com chave `default`.
    """

    def __init__(self, api_id, api_hash):
        self.api_id = api_id
        self.api_hash = api_hash
        self.bots = OrderedDict()
        self.destination_bot_cache = OrderedDict()
        self.destination_bot_cache_max = 1000
        self._load_config()

    @staticmethod
    def _safe_key(value):
        value = str(value or "").strip().lower()
        value = re.sub(r"[^a-z0-9_.-]+", "_", value)
        return value.strip("_.-")

    @staticmethod
    def _token_bot_id(token):
        """Extrai somente o ID público do bot da parte anterior aos dois-pontos.

        Isso permite isolar o arquivo .session por identidade do bot sem expor
        nem persistir o segredo do token no nome do arquivo/log.
        """
        token = str(token or "").strip()
        if ":" not in token:
            return ""
        candidate = token.split(":", 1)[0].strip()
        return candidate if candidate.isdigit() else ""

    def _load_config(self):
        raw_bots = {}

        if BOTS_JSON:
            try:
                parsed = json.loads(BOTS_JSON)
                if not isinstance(parsed, dict):
                    raise ValueError("TELEGRAM_BOTS_JSON precisa ser um objeto JSON")
                raw_bots = parsed
            except Exception as error:
                print(
                    "[Bots] TELEGRAM_BOTS_JSON inválido:",
                    type(error).__name__,
                    str(error)
                )

        if not raw_bots and LEGACY_BOT_TOKEN:
            raw_bots = {
                "default": {
                    "token": LEGACY_BOT_TOKEN,
                    "name": "Bot padrão",
                    "session_name": LEGACY_BOT_SESSION_NAME,
                }
            }

        os.makedirs(BOT_SESSION_DIR, exist_ok=True)

        for raw_key, raw_config in raw_bots.items():
            key = self._safe_key(raw_key)
            if not key:
                print("[Bots] Bot ignorado: chave vazia/inválida")
                continue

            if isinstance(raw_config, str):
                token = raw_config.strip()
                name = key
                session_name = f"telegram_bot_{key}"
            elif isinstance(raw_config, dict):
                token = str(raw_config.get("token") or "").strip()
                name = str(raw_config.get("name") or key).strip()
                session_name = str(
                    raw_config.get("session_name")
                    or f"telegram_bot_{key}"
                ).strip()
            else:
                print(f"[Bots] Configuração ignorada para '{key}'")
                continue

            if not token:
                print(f"[Bots] Token vazio para '{key}'. Bot ignorado.")
                continue

            safe_session = self._safe_key(session_name) or f"telegram_bot_{key}"

            # IMPORTANTE: uma mesma key pode receber um token de um bot novo.
            # Telethon reaproveitaria o .session antigo e poderia continuar
            # autorizado como o bot anterior. O ID numérico (parte pública do
            # token) entra no nome da sessão para forçar uma sessão nova quando
            # a identidade do bot mudar, sem expor o segredo do token.
            token_bot_id = self._token_bot_id(token)
            if token_bot_id:
                identity_suffix = f"_{token_bot_id}"
                if not safe_session.endswith(identity_suffix):
                    safe_session = f"{safe_session}{identity_suffix}"

            session_path = os.path.join(BOT_SESSION_DIR, safe_session)

            self.bots[key] = {
                "key": key,
                "name": name,
                "token": token,
                "token_bot_id": token_bot_id or None,
                "session_name": safe_session,
                "client": TelegramClient(session_path, self.api_id, self.api_hash),
                "entity_cache": OrderedDict(),
                "entity_cache_max": 500,
                "connected": False,
                "telegram_id": None,
                "username": None,
            }

    @property
    def configured(self):
        return bool(self.bots)

    @property
    def available(self):
        return any(
            bot.get("connected")
            and bot["client"].is_connected()
            for bot in self.bots.values()
        )

    def public_bots(self):
        return [
            {
                "key": bot["key"],
                "name": bot["name"],
                "telegram_id": bot.get("telegram_id"),
                "username": bot.get("username"),
                "connected": bool(
                    bot.get("connected")
                    and bot["client"].is_connected()
                ),
            }
            for bot in self.bots.values()
        ]

    async def start(self):
        if not self.configured:
            print(
                "[Bots] Nenhum bot publicador configurado. "
                "Defina TELEGRAM_BOTS_JSON na AWS."
            )
            return False

        connected = 0

        for key, bot in self.bots.items():
            try:
                await bot["client"].start(bot_token=bot["token"])
                me = await bot["client"].get_me()

                expected_bot_id = bot.get("token_bot_id")
                actual_bot_id = str(me.id)
                if expected_bot_id and actual_bot_id != expected_bot_id:
                    raise RuntimeError(
                        f"Sessão do bot '{key}' autenticou como ID {actual_bot_id}, "
                        f"mas o token pertence ao ID {expected_bot_id}. "
                        "Remova a sessão antiga desse bot e reinicie o worker."
                    )

                bot["connected"] = True
                bot["telegram_id"] = actual_bot_id
                bot["username"] = me.username
                connected += 1

                print(
                    f"[Bots] '{key}' conectado:",
                    f"@{me.username}" if me.username else me.id
                )
                await self._warm_entity_cache(bot)
            except Exception as error:
                bot["connected"] = False
                print(
                    f"[Bots] Falha ao conectar '{key}':",
                    type(error).__name__,
                    str(error)
                )

        print(
            "[Bots] Pool inicializado:",
            f"{connected}/{len(self.bots)} conectado(s)"
        )
        return connected > 0

    async def close(self):
        for bot in self.bots.values():
            client = bot["client"]
            if client.is_connected():
                await client.disconnect()
            bot["connected"] = False

    @staticmethod
    def _automation_bot_key(automation):
        if not isinstance(automation, dict):
            return ""

        direct = (
            automation.get("telegram_bot_key")
            or automation.get("publisher_bot_key")
            or automation.get("bot_key")
        )
        if direct:
            return TelegramButtonPublisher._safe_key(direct)

        publisher_bot = automation.get("publisher_bot")
        if isinstance(publisher_bot, dict):
            nested = (
                publisher_bot.get("key")
                or publisher_bot.get("slug")
                or publisher_bot.get("id")
            )
            if nested:
                return TelegramButtonPublisher._safe_key(nested)

        return ""

    def _get_bot(self, automation=None, explicit_key=None):
        key = self._safe_key(explicit_key) if explicit_key else self._automation_bot_key(automation)

        if key:
            bot = self.bots.get(key)
            if bot is None:
                raise KeyError(
                    f"Bot '{key}' não existe em TELEGRAM_BOTS_JSON. "
                    "Cadastre a mesma chave na AWS ou selecione outro bot no Lovable."
                )
        elif len(self.bots) == 1:
            bot = next(iter(self.bots.values()))
        else:
            raise ValueError(
                "Automação não possui telegram_bot_key. "
                "Com múltiplos bots, selecione um bot publicador no Lovable."
            )

        if not (
            bot.get("connected")
            and bot["client"].is_connected()
        ):
            raise RuntimeError(
                f"Bot '{bot['key']}' está configurado, mas não conectado."
            )

        return bot

    def _remember_destination_bot(self, destination_chat_id, bot_key):
        destination = str(destination_chat_id).strip()
        if destination in self.destination_bot_cache:
            self.destination_bot_cache.pop(destination, None)
        self.destination_bot_cache[destination] = bot_key
        while len(self.destination_bot_cache) > self.destination_bot_cache_max:
            self.destination_bot_cache.popitem(last=False)

    async def _warm_entity_cache(self, bot):
        try:
            async for dialog in bot["client"].iter_dialogs():
                self._cache_entity(bot, str(dialog.id), dialog.input_entity)
        except Exception as error:
            print(
                f"[Bots] Cache de entidades falhou para '{bot['key']}':",
                type(error).__name__,
                str(error)
            )

    @staticmethod
    def _cache_entity(bot, key, value):
        key = str(key).strip()
        cache = bot["entity_cache"]
        if key in cache:
            cache.pop(key, None)
        cache[key] = value
        while len(cache) > bot["entity_cache_max"]:
            cache.popitem(last=False)

    async def _resolve_destination(self, bot, destination_chat_id):
        destination = str(destination_chat_id).strip()
        cache = bot["entity_cache"]
        cached = cache.get(destination)

        if cached is not None:
            cache.move_to_end(destination)
            return cached

        try:
            entity = await bot["client"].get_input_entity(int(destination))
            self._cache_entity(bot, destination, entity)
            return entity
        except Exception:
            pass

        async for dialog in bot["client"].iter_dialogs():
            dialog_id = str(dialog.id)
            self._cache_entity(bot, dialog_id, dialog.input_entity)
            if dialog_id == destination:
                return dialog.input_entity

        raise ValueError(
            f"Bot '{bot['key']}' não consegue resolver o canal {destination}. "
            "Adicione esse bot como administrador do destino com permissão de publicar."
        )

    @staticmethod
    def normalize_style(value):
        raw = str(value or "").strip().lower()
        aliases = {
            "blue": "primary",
            "azul": "primary",
            "primary": "primary",
            "green": "success",
            "verde": "success",
            "success": "success",
            "red": "danger",
            "vermelho": "danger",
            "danger": "danger",
            "default": None,
            "neutral": None,
            "neutro": None,
            "": None,
        }
        return aliases.get(raw)

    @staticmethod
    def normalize_buttons(automation):
        raw_buttons = automation.get("buttons", []) or []
        normalized = []

        for index, item in enumerate(raw_buttons):
            if not isinstance(item, dict):
                continue
            if item.get("enabled", True) is False:
                continue

            text = str(item.get("text") or item.get("label") or "").strip()
            url = str(item.get("url") or "").strip()

            if not text or not url:
                continue
            if not url.lower().startswith(("http://", "https://", "tg://")):
                print("[Buttons] URL ignorada por protocolo inválido:", url)
                continue

            try:
                row = int(item.get("row", item.get("row_index", index)))
            except (TypeError, ValueError):
                row = index

            try:
                sort_order = int(item.get("sort_order", index))
            except (TypeError, ValueError):
                sort_order = index

            style = TelegramButtonPublisher.normalize_style(
                item.get("style") or item.get("color")
            )

            normalized.append({
                "text": text,
                "url": url,
                "row": max(0, row),
                "sort_order": sort_order,
                "style": style,
            })

        normalized.sort(key=lambda button: (button["row"], button["sort_order"]))
        return normalized

    @classmethod
    def build_keyboard(cls, automation):
        normalized = cls.normalize_buttons(automation)
        if not normalized:
            return []

        rows = defaultdict(list)
        for button in normalized:
            rows[button["row"]].append(
                Button.url(
                    button["text"],
                    button["url"],
                    style=button.get("style"),
                )
            )

        return [rows[row] for row in sorted(rows.keys())]

    @classmethod
    def has_buttons(cls, automation):
        return bool(cls.normalize_buttons(automation))

    async def send_text(self, destination_chat_id, text, entities, automation):
        bot = self._get_bot(automation)
        destination = await self._resolve_destination(bot, destination_chat_id)
        result = await bot["client"].send_message(
            destination,
            text,
            formatting_entities=entities or [],
            buttons=self.build_keyboard(automation),
        )
        self._remember_destination_bot(destination_chat_id, bot["key"])
        return result

    async def send_file(self, destination_chat_id, file_path, caption, entities, automation):
        bot = self._get_bot(automation)
        destination = await self._resolve_destination(bot, destination_chat_id)
        result = await bot["client"].send_file(
            destination,
            file_path,
            caption=caption or "",
            formatting_entities=entities or [],
            buttons=self.build_keyboard(automation),
            supports_streaming=True,
        )
        self._remember_destination_bot(destination_chat_id, bot["key"])
        return result

    async def edit_message(
        self,
        destination_chat_id,
        destination_message_id,
        text,
        entities,
        automation,
    ):
        bot = self._get_bot(automation)
        destination = await self._resolve_destination(bot, destination_chat_id)
        return await bot["client"].edit_message(
            destination,
            int(destination_message_id),
            text,
            formatting_entities=entities or [],
            buttons=self.build_keyboard(automation),
        )

    async def delete_messages(self, destination_chat_id, message_ids):
        destination_key = str(destination_chat_id).strip()
        preferred_key = self.destination_bot_cache.get(destination_key)

        candidates = []
        if preferred_key and preferred_key in self.bots:
            candidates.append(self.bots[preferred_key])

        for bot in self.bots.values():
            if bot not in candidates and bot.get("connected"):
                candidates.append(bot)

        last_error = None
        for bot in candidates:
            try:
                destination = await self._resolve_destination(bot, destination_chat_id)
                result = await bot["client"].delete_messages(
                    destination,
                    [int(message_id) for message_id in message_ids],
                    revoke=True,
                )
                self._remember_destination_bot(destination_chat_id, bot["key"])
                return result
            except Exception as error:
                last_error = error

        if last_error:
            raise last_error
        raise RuntimeError("Nenhum bot conectado disponível para excluir mensagem")
