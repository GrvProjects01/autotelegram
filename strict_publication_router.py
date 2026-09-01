"""Roteamento estrito de publicacao para evitar duplicidade e midia incompleta.

Regra:
- automacao sem botoes validos -> fluxo humano normal;
- automacao com botoes validos -> somente o bot publicador envia;
- falha do bot nunca vira fallback humano silencioso;
- midia individual com botoes e baixada e validada antes do upload;
- erros de bookkeeping da rotacao depois de um envio bem-sucedido nao podem
  transformar sucesso no Telegram em falsa falha e gerar duplicidade.
"""

import os
import shutil
import tempfile


class ButtonPublicationError(RuntimeError):
    pass


def _has_buttons(worker, automation):
    try:
        return bool(worker.automation_has_inline_buttons(automation))
    except Exception:
        return False


def _expected_document_size(message):
    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    if document is None:
        return None
    try:
        value = int(getattr(document, "size", 0) or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


async def _download_media_checked(worker, message, session_key, attempts=2):
    last_error = None

    for attempt in range(1, attempts + 1):
        temp_dir = tempfile.mkdtemp(prefix=f"strict_media_{session_key}_")
        try:
            path = await worker.client.download_media(message, file=temp_dir)
            if not path:
                raise RuntimeError("download_media retornou caminho vazio")

            path = os.fspath(path)
            if not os.path.exists(path):
                raise RuntimeError(f"arquivo baixado nao existe: {path}")

            size = os.path.getsize(path)
            if size <= 0:
                raise RuntimeError("arquivo baixado possui 0 bytes")

            expected = _expected_document_size(message)
            if expected and size < expected:
                raise RuntimeError(
                    f"download incompleto: local={size} esperado={expected}"
                )

            print(
                f"[Strict Media:{session_key}] download validado "
                f"tentativa={attempt} bytes={size}"
            )
            return temp_dir, path

        except Exception as error:
            last_error = error
            shutil.rmtree(temp_dir, ignore_errors=True)
            print(
                f"[Strict Media:{session_key}] download falhou "
                f"tentativa={attempt}:",
                type(error).__name__,
                str(error),
            )

    raise RuntimeError(
        f"midia nao pode ser baixada integralmente: {last_error}"
    )


def register(worker, base, session_key="primary"):
    original_try_publish = worker.try_publish_with_inline_buttons
    original_commit_rotation = base._commit_rotation

    # A mensagem ja foi aceita pelo Telegram antes de _commit_rotation rodar.
    # Portanto erro ao persistir cursor NUNCA pode subir como erro de envio.
    async def safe_commit_rotation(key, next_cursor):
        try:
            await original_commit_rotation(key, next_cursor)
        except Exception as error:
            print(
                f"[Strict Routing:{session_key}] cursor da rotacao nao persistiu "
                f"automacao={key}; envio ja concluido:",
                type(error).__name__,
                str(error),
            )

    base._commit_rotation = safe_commit_rotation

    async def strict_try_publish_with_inline_buttons(
        message,
        destination,
        processed_text,
        processed_entities,
        preserve_media,
        preserve_caption,
        preserve_formatting,
        automation,
    ):
        # Sem botoes validos: devolve None e deixa o worker seguir pelo usuario.
        if not _has_buttons(worker, automation):
            return None

        automation_id = str(automation.get("id") or "").strip() or "unknown"
        bot_key = ""
        try:
            bot_key = worker.button_publisher._automation_bot_key(automation)
        except Exception:
            pass

        entities = processed_entities if preserve_formatting else []

        try:
            if getattr(message, "media", None) and preserve_media:
                temp_dir = None
                try:
                    temp_dir, file_path = await _download_media_checked(
                        worker,
                        message,
                        session_key,
                        attempts=2,
                    )

                    sent = await worker.button_publisher.send_file(
                        destination_chat_id=destination,
                        file_path=file_path,
                        caption=(processed_text if preserve_caption else ""),
                        entities=(entities if preserve_caption else []),
                        automation=automation,
                    )
                finally:
                    if temp_dir:
                        shutil.rmtree(temp_dir, ignore_errors=True)

            else:
                if not processed_text:
                    raise ButtonPublicationError(
                        "automacao possui botoes, mas a mensagem nao possui texto nem midia publicavel"
                    )

                sent = await worker.button_publisher.send_text(
                    destination_chat_id=destination,
                    text=processed_text,
                    entities=entities,
                    automation=automation,
                )

            if sent is None:
                raise ButtonPublicationError("bot publisher retornou resultado vazio")

            print(
                f"[Strict Routing:{session_key}] BOT OK "
                f"automacao={automation_id} bot={bot_key or '-'} "
                f"source={getattr(message, 'id', None)} dest_msg={sent.id}"
            )
            return sent

        except Exception as error:
            # CRITICO: nao retornar None aqui. None faria publish_single_message
            # cair no envio humano e criaria exatamente a duplicidade observada.
            print(
                f"[Strict Routing:{session_key}] BOT FAIL SEM FALLBACK HUMANO "
                f"automacao={automation_id} bot={bot_key or '-'} "
                f"source={getattr(message, 'id', None)}:",
                type(error).__name__,
                str(error),
            )

            try:
                await worker.send_log(
                    automation_id=automation.get("id"),
                    source_message_id=getattr(message, "id", None),
                    status="error",
                    original_text=getattr(message, "message", "") or "",
                    processed_text=processed_text or "",
                    error_message=(
                        "button_publication_failed_no_human_fallback: "
                        f"{type(error).__name__}: {error}"
                    ),
                )
            except Exception:
                pass

            raise ButtonPublicationError(str(error)) from error

    worker.try_publish_with_inline_buttons = strict_try_publish_with_inline_buttons

    print(
        f"[Strict Routing:{session_key}] ativo: "
        "com botoes=bot apenas; sem botoes=sessao humana"
    )

    return original_try_publish
