"""Compatibilidade e validação do contrato de botões Lovable -> worker.

Objetivos:
- aceitar exatamente os formatos de URL permitidos no painel;
- impedir que `buttons_count > 0` vire silenciosamente automação sem botões;
- registrar divergências entre payload recebido e normalização do worker;
- manter a arquitetura atual sem duplicar publisher.
"""

import copy
import re


def _normalize_url(value):
    raw = str(value or "").strip()
    if not raw:
        return ""

    lower = raw.lower()
    if lower.startswith(("http://", "https://", "tg://")):
        return raw

    if lower.startswith("t.me/") or lower.startswith("www.t.me/"):
        return "https://" + raw

    if raw.startswith("@"):
        username = raw[1:].strip()
        if re.fullmatch(r"[A-Za-z0-9_]{4,}", username):
            return f"https://t.me/{username}"

    return raw


def _expected_count(automation):
    try:
        value = automation.get("buttons_count")
        if value is None:
            return None
        return max(0, int(value))
    except (AttributeError, TypeError, ValueError):
        return None


def register(worker, session_key="primary"):
    publisher = worker.button_publisher
    publisher_cls = type(publisher)

    original_normalize = publisher_cls.normalize_buttons
    original_has_buttons = publisher_cls.has_buttons
    original_send_text = publisher.send_text
    original_send_file = publisher.send_file

    def normalize_buttons_compat(automation):
        if not isinstance(automation, dict):
            return original_normalize(automation)

        prepared = copy.deepcopy(automation)
        raw_buttons = prepared.get("buttons", []) or []

        for item in raw_buttons:
            if not isinstance(item, dict):
                continue
            if "url" in item:
                item["url"] = _normalize_url(item.get("url"))

        normalized = original_normalize(prepared)
        expected = _expected_count(automation)

        if expected is not None and expected != len(normalized):
            print(
                f"[Buttons Contract:{session_key}] DIVERGENCIA "
                f"automation={automation.get('id')} "
                f"endpoint={expected} raw={len(raw_buttons)} "
                f"worker_validos={len(normalized)}"
            )
        elif expected is not None:
            print(
                f"[Buttons Contract:{session_key}] OK "
                f"automation={automation.get('id')} "
                f"buttons={len(normalized)}"
            )

        return normalized

    def has_buttons_compat(cls, automation):
        normalized = cls.normalize_buttons(automation)
        if normalized:
            return True

        expected = _expected_count(automation)
        if expected and expected > 0:
            # Importante: força o roteador estrito a tratar como automação COM
            # botões. O envio então falha de forma explícita se o teclado não
            # puder ser montado, em vez de cair no envio humano sem botão.
            print(
                f"[Buttons Contract:{session_key}] endpoint declara {expected} "
                f"botões, mas nenhum ficou válido automation={automation.get('id')}"
            )
            return True

        return original_has_buttons(automation)

    async def send_text_guarded(destination_chat_id, text, entities, automation):
        normalized = publisher_cls.normalize_buttons(automation)
        expected = _expected_count(automation)
        if expected and expected > 0 and not normalized:
            raise RuntimeError(
                f"Contrato de botões inválido: endpoint={expected}, worker_validos=0, "
                f"automation={automation.get('id')}"
            )
        return await original_send_text(
            destination_chat_id=destination_chat_id,
            text=text,
            entities=entities,
            automation=automation,
        )

    async def send_file_guarded(destination_chat_id, file_path, caption, entities, automation):
        normalized = publisher_cls.normalize_buttons(automation)
        expected = _expected_count(automation)
        if expected and expected > 0 and not normalized:
            raise RuntimeError(
                f"Contrato de botões inválido: endpoint={expected}, worker_validos=0, "
                f"automation={automation.get('id')}"
            )
        return await original_send_file(
            destination_chat_id=destination_chat_id,
            file_path=file_path,
            caption=caption,
            entities=entities,
            automation=automation,
        )

    publisher_cls.normalize_buttons = staticmethod(normalize_buttons_compat)
    publisher_cls.has_buttons = classmethod(has_buttons_compat)
    publisher.send_text = send_text_guarded
    publisher.send_file = send_file_guarded

    print(
        f"[Buttons Contract:{session_key}] ativo: URLs Lovable compatíveis + "
        "fail-closed quando endpoint declara botões"
    )
