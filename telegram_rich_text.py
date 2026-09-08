"""Normaliza entidades de texto Telegram vindas do painel/Lovable.

O contrato usa offsets/lengths em unidades UTF-16, como a API do Telegram e
JavaScript String indexes. Isso permite preservar formatacao rica e custom
emoji (Telegram Premium) tanto em texto quanto em captions.
"""

from telethon import types


SUPPORTED_TYPES = {
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "strike",
    "spoiler",
    "code",
    "pre",
    "text_url",
    "text_link",
    "blockquote",
    "custom_emoji",
}


def _utf16_length(text):
    return len(str(text or "").encode("utf-16-le")) // 2


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _entity_type(item):
    return str(item.get("type") or item.get("entity_type") or "").strip().lower()


def _entity_bounds(item, text_utf16_len):
    offset = _int(item.get("offset"))
    length = _int(item.get("length"))
    if offset is None or length is None or offset < 0 or length <= 0:
        return None
    if offset + length > text_utf16_len:
        return None
    return offset, length


def normalize_payload(raw_entities):
    if not isinstance(raw_entities, list):
        return []
    return [item for item in raw_entities if isinstance(item, dict)]


def build_entities(item, text):
    """Converte `message_entities` JSON em entidades Telethon.

    Formato principal:
      {"type":"bold","offset":0,"length":5}
      {"type":"text_url","offset":0,"length":5,"url":"https://..."}
      {"type":"custom_emoji","offset":0,"length":2,"custom_emoji_id":"123"}

    `custom_emoji_id` e `document_id` sao aliases. O trecho envolvido deve ser
    exatamente um emoji Unicode fallback, conforme regra do Telegram.
    """
    raw = (
        item.get("message_entities")
        or item.get("entities")
        or item.get("text_entities")
        or []
    ) if isinstance(item, dict) else []

    text = str(text or "")
    text_len = _utf16_length(text)
    result = []

    for raw_entity in normalize_payload(raw):
        kind = _entity_type(raw_entity)
        if kind not in SUPPORTED_TYPES:
            continue
        bounds = _entity_bounds(raw_entity, text_len)
        if not bounds:
            continue
        offset, length = bounds

        try:
            if kind == "bold":
                entity = types.MessageEntityBold(offset=offset, length=length)
            elif kind == "italic":
                entity = types.MessageEntityItalic(offset=offset, length=length)
            elif kind == "underline":
                entity = types.MessageEntityUnderline(offset=offset, length=length)
            elif kind in {"strikethrough", "strike"}:
                entity = types.MessageEntityStrike(offset=offset, length=length)
            elif kind == "spoiler":
                entity = types.MessageEntitySpoiler(offset=offset, length=length)
            elif kind == "code":
                entity = types.MessageEntityCode(offset=offset, length=length)
            elif kind == "pre":
                language = str(raw_entity.get("language") or "")
                entity = types.MessageEntityPre(offset=offset, length=length, language=language)
            elif kind in {"text_url", "text_link"}:
                url = str(raw_entity.get("url") or "").strip()
                if not url.lower().startswith(("https://", "http://", "tg://")):
                    continue
                entity = types.MessageEntityTextUrl(offset=offset, length=length, url=url)
            elif kind == "blockquote":
                entity = types.MessageEntityBlockquote(offset=offset, length=length)
            elif kind == "custom_emoji":
                document_id = _int(
                    raw_entity.get("custom_emoji_id")
                    or raw_entity.get("document_id")
                    or raw_entity.get("emoji_id")
                )
                if not document_id or document_id <= 0:
                    continue
                entity = types.MessageEntityCustomEmoji(
                    offset=offset,
                    length=length,
                    document_id=document_id,
                )
            else:
                continue
            result.append(entity)
        except Exception:
            # Uma entidade malformada nao pode derrubar o worker inteiro.
            continue

    result.sort(key=lambda entity: (entity.offset, -entity.length))
    return result


def has_custom_emoji(item):
    raw = (
        item.get("message_entities")
        or item.get("entities")
        or item.get("text_entities")
        or []
    ) if isinstance(item, dict) else []
    return any(
        isinstance(entity, dict) and _entity_type(entity) == "custom_emoji"
        for entity in raw
    )
