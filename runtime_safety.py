"""Protecoes de runtime para impedir dois workers usando a mesma sessao Telegram."""

import fcntl
import hashlib
import os
import tempfile


_LOCK_HANDLE = None


def register(worker, session_key="primary"):
    original = worker.acquire_process_lock

    def acquire_session_lock():
        global _LOCK_HANDLE

        # O lock antigo incluia WORKER_ID. Isso permitia um worker legado e um
        # worker novo abrirem a mesma sessao Telegram com nomes de worker distintos.
        # Aqui a identidade exclusiva e SOMENTE a sessao Telegram efetiva.
        session_name = os.path.abspath(str(worker.SESSION_NAME or "telegram_main"))
        digest = hashlib.sha256(session_name.encode("utf-8")).hexdigest()[:20]
        lock_path = os.path.join(
            tempfile.gettempdir(),
            f"telegram_session_{digest}.lock",
        )

        _LOCK_HANDLE = open(lock_path, "w")
        try:
            fcntl.flock(
                _LOCK_HANDLE.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            _LOCK_HANDLE.seek(0)
            _LOCK_HANDLE.truncate()
            _LOCK_HANDLE.write(
                f"pid={os.getpid()} session={session_name} key={session_key}\n"
            )
            _LOCK_HANDLE.flush()
            print(
                f"[Runtime Safety:{session_key}] lock exclusivo adquirido "
                f"session={session_name}"
            )
        except BlockingIOError as error:
            raise RuntimeError(
                "Ja existe outro processo usando esta MESMA sessao Telegram. "
                f"session={session_name}. Pare o worker duplicado antes de iniciar."
            ) from error

    worker.acquire_process_lock = acquire_session_lock
    print(f"[Runtime Safety:{session_key}] lock por sessao registrado")
    return original
