"""Controles por automacao: liga/desliga botoes e override global de links.

Campos esperados no payload Lovable:
- buttons_enabled: bool (default True para compatibilidade)
- link_override_enabled: bool (default False)
- link_override_url: str
- link_override_scope: 'telegram' | 'all' (default 'telegram')

O addon nao cuida de grupos organizacionais; grupos sao apenas UI/banco no Lovable.
"""

import copy
import re


URL_PATTERN = re.compile(
    r"(?P<url>(?:https?://|tg://|www\.)[^\s<>]+|(?:t\.me/)[^\s<>]+)",
    flags=re.IGNORECASE,
)
MENTION_PATTERN = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{4,}")


def _truthy(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "sim", "enabled", "active"}


def _buttons_enabled(automation):
    if not isinstance(automation, dict):
        return True
    if "buttons_enabled" not in automation:
        return True
    return _truthy(automation.get("buttons_enabled"), default=True)


def _normalize_target(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    if lower.startswith(("http://", "https://", "tg://")):
        return raw
    if lower.startswith(("t.me/", "www.t.me/")):
        return "https://" + raw
    if raw.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_]{4,}", raw):
        return "https://t.me/" + raw[1:]
    return raw


def _is_telegram_url(value):
    lower = str(value or "").strip().lower()
    return (
        lower.startswith("tg://")
        or lower.startswith("t.me/")
        or lower.startswith("www.t.me/")
        or lower.startswith("https://t.me/")
        or lower.startswith("http://t.me/")
        or lower.startswith("https://www.t.me/")
        or lower.startswith("http://www.t.me/")
    )


def _override_config(automation):
    if not isinstance(automation, dict):
        return False, "", "telegram"
    enabled = _truthy(automation.get("link_override_enabled"), default=False)
    target = _normalize_target(automation.get("link_override_url"))
    scope = str(automation.get("link_override_scope") or "telegram").strip().lower()
    if scope not in {"telegram", "all"}:
        scope = "telegram"
    return enabled and bool(target), target, scope


def _replace_visible_links(worker, text, entities, target, scope):
    text = text or ""
    occurrences = []

    matches = []
    for match in URL_PATTERN.finditer(text):
        raw = match.group("url")
        if scope == "all" or _is_telegram_url(raw):
            matches.append((match.start(), match.end(), raw))

    if scope == "telegram":
        for match in MENTION_PATTERN.finditer(text):
            matches.append((match.start(), match.end(), match.group(0)))

    if not matches:
        return text, entities

    matches.sort(key=lambda item: (item[0], item[1]))
    filtered = []
    last_end = -1
    for item in matches:
        if item[0] < last_end:
            continue
        filtered.append(item)
        last_end = item[1]

    pieces = []
    cursor = 0
    for start, end, raw in filtered:
        pieces.append(text[cursor:start])
        current_before = "".join(pieces)
        occurrence_start = worker.utf16_length(current_before)
        pieces.append(target)
        occurrences.append({
            "start": occurrence_start,
            "old_length": worker.utf16_length(raw),
            "new_length": worker.utf16_length(target),
        })
        cursor = end
    pieces.append(text[cursor:])
    new_text = "".join(pieces)

    new_entities = worker.adjust_entities_for_replacements(
        entities or [],
        occurrences,
        max_text_len=worker.utf16_length(new_text),
    )
    return new_text, new_entities


def register(worker, session_key="primary"):
    publisher_cls = type(worker.button_publisher)
    original_normalize_buttons = publisher_cls.normalize_buttons
    original_has_buttons = publisher_cls.has_buttons
    original_process_rich_text = worker.process_rich_text

    def normalize_buttons_with_toggle(automation):
        if not _buttons_enabled(automation):
            return []
        return original_normalize_buttons(automation)

    def has_buttons_with_toggle(cls, automation):
        if not _buttons_enabled(automation):
            print(
                f"[Buttons Toggle:{session_key}] desativados "
                f"automation={automation.get('id') if isinstance(automation, dict) else '-'}"
            )
            return False
        return original_has_buttons(automation)

    def process_rich_text_with_override(text, entities, automation):
        processed_text, processed_entities = original_process_rich_text(
            text,
            entities,
            automation,
        )
        if processed_text is None:
            return processed_text, processed_entities

        enabled, target, scope = _override_config(automation)
        if not enabled:
            return processed_text, processed_entities

        result_entities = copy.deepcopy(processed_entities or [])

        for entity in result_entities:
            if isinstance(entity, worker.MessageEntityTextUrl):
                current = str(getattr(entity, "url", "") or "")
                if scope == "all" or _is_telegram_url(current):
                    entity.url = target

        result_text, result_entities = _replace_visible_links(
            worker,
            processed_text,
            result_entities,
            target,
            scope,
        )

        print(
            f"[Link Override:{session_key}] automation={automation.get('id')} "
            f"scope={scope} target={target}"
        )
        return result_text, result_entities

    publisher_cls.normalize_buttons = staticmethod(normalize_buttons_with_toggle)
    publisher_cls.has_buttons = classmethod(has_buttons_with_toggle)
    worker.process_rich_text = process_rich_text_with_override

    print(
        f"[Automation Controls:{session_key}] ativo: buttons_enabled + link_override"
    )
