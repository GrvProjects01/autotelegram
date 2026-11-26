"""Endereco da API do painel usado pelo robo.

Sem WORKER_API_URL, vale o endereco publicado hoje. A chave do robo vai no
cabecalho de cada chamada, por isso HTTP sem criptografia so e aceito quando a
API esta no proprio servidor.
"""

from urllib.parse import urlsplit

DEFAULT_API_URL = "https://telegram-channel-automator-theta.vercel.app"
# Texto fixo: o valor recebido nunca entra na mensagem (poderia ter credenciais).
INVALID_MESSAGE = "WORKER_API_URL invalida: use https://dominio ou http://127.0.0.1:porta"
_LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def resolve_api_url(value=None):
    url = str(value or "").strip().rstrip("/")
    if not url:
        return DEFAULT_API_URL

    try:
        parts = urlsplit(url)
        parts.port  # porta invalida gera ValueError
    except ValueError:
        parts = None

    valid = (
        parts is not None
        and bool(parts.hostname)
        and "@" not in parts.netloc
        and not (parts.path or parts.query or parts.fragment)
        and (
            parts.scheme == "https"
            or (parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS)
        )
    )
    if not valid:
        raise ValueError(INVALID_MESSAGE)
    return url
