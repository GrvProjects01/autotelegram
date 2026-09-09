"""Rodapé opcional e link embutido para todas as mensagens processadas.

A camada roda depois do processamento normal de Replace/blacklist e antes da
publicação. Assim, texto puro, mídia, álbum e histórico reutilizam a mesma regra.

Campos aceitos por automação:
- footer_enabled: bool (default false)
- footer_text: texto livre do rodapé
- footer_link_text: trecho clicável opcional
- footer_link_url: URL do trecho clicável opcional
- footer_separator: separador opcional (default "\n\n")

O hyperlink usa MessageEntityTextUrl e offsets UTF-16, compatíveis com Telegram.
"""

import copy


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "sim"}


def _utf16_length(value):
    value = str(value or "")
    return len(value.encode("utf-16-le")) // 2


def _valid_link(url):
    url = str(url or "").strip()
    if not url:
        return ""
    if url.lower().startswith(("http://", "https://", "tg://")):
        return url
    if url.startswith("@") and len(url) > 1:
        return "https://t.me/" + url[1:]
    if url.lower().startswith("t.me/"):
        return "https://" + url
    return ""


def _footer_config(automation):
    automation = automation if isinstance(automation, dict) else {}
    enabled = _as_bool(
        automation.get("footer_enabled", automation.get("message_footer_enabled")),
        default=False,
    )
    text = str(
        automation.get("footer_text")
        or automation.get("message_footer_text")
        or ""
    ).strip()
    link_text = str(
        automation.get("footer_link_text")
        or automation.get("message_footer_link_text")
        or ""
    ).strip()
    link_url = _valid_link(
        automation.get("footer_link_url")
        or automation.get("message_footer_link_url")
        or ""
    )
    separator = automation.get("footer_separator")
    if separator is None:
        separator = "\n\n"
    separator = str(separator)
    return enabled, text, link_text, link_url, separator


def register(worker, session_key="primary"):
    original_process_rich_text = worker.process_rich_text

    def process_rich_text_with_footer(text, entities, automation):
        processed_text, processed_entities = original_process_rich_text(
            text,
            entities,
            automation,
        )

        # Blacklist/fluxo normal pode bloquear a publicação retornando None.
        if processed_text is None:
            return processed_text, processed_entities

        enabled, footer_text, link_text, link_url, separator = _footer_config(automation)
        if not enabled:
            return processed_text, processed_entities

        if not footer_text and not link_text:
            print(
                f"[Footer:{session_key}] habilitado sem conteúdo; ignorado"
            )
            return processed_text, processed_entities

        parts = []
        if footer_text:
            parts.append(footer_text)
        if link_text:
            parts.append(link_text)
        footer = "\n".join(parts)

        base_text = str(processed_text or "")
        prefix = separator if base_text else ""
        final_text = base_text + prefix + footer

        final_entities = copy.deepcopy(processed_entities or [])

        if link_text and link_url:
            # O texto clicável é sempre a última parte do rodapé.
            link_start_chars = len(base_text + prefix)
            if footer_text:
                link_start_chars += len(footer_text) + 1

            prefix_text = final_text[:link_start_chars]
            offset = _utf16_length(prefix_text)
            length = _utf16_length(link_text)

            final_entities.append(
                worker.MessageEntityTextUrl(
                    offset=offset,
                    length=length,
                    url=link_url,
                )
            )
        elif link_text and not link_url:
            print(
                f"[Footer:{session_key}] link_text informado sem URL válida; "
                "texto mantido sem hyperlink"
            )

        print(
            f"[Footer:{session_key}] aplicado automação="
            f"{str((automation or {}).get('id') or '')} "
            f"link={'yes' if link_text and link_url else 'no'}"
        )

        return final_text, final_entities

    worker.process_rich_text = process_rich_text_with_footer
    print(f"[Footer:{session_key}] camada registrada")
