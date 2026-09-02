"""Ledger local duravel para idempotencia de publicacoes Telegram.

Objetivos:
- impedir duplicidade entre realtime, recovery, historico e processos concorrentes;
- nao depender exclusivamente do Lovable/message-links para saber se algo ja saiu;
- persistir o vinculo source -> destination localmente antes da tentativa remota;
- usar SQLite (transacional, lock entre processos e persistente apos restart).
"""

import os
import sqlite3
import tempfile
import time


DEFAULT_CLAIM_TTL = int(os.getenv("TELEGRAM_PUBLICATION_CLAIM_TTL", "300"))


def _db_path():
    configured = os.getenv("TELEGRAM_PUBLICATION_LEDGER_DB", "").strip()
    if configured:
        return configured
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "publication_ledger.sqlite3",
    )


def _connect(path):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS published_links (
            source_chat_id TEXT NOT NULL,
            source_message_id INTEGER NOT NULL,
            automation_id TEXT NOT NULL,
            source_grouped_id TEXT,
            destination_chat_id TEXT NOT NULL,
            destination_message_id INTEGER NOT NULL,
            published_at INTEGER NOT NULL,
            PRIMARY KEY (source_chat_id, source_message_id, automation_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fingerprint_claims (
            fingerprint TEXT PRIMARY KEY,
            expires_at INTEGER NOT NULL
        )
        """
    )
    return conn


def _local_find(conn, source_chat_id, source_message_id, automation_id=None):
    source_chat_id = str(source_chat_id).strip()
    source_message_id = int(source_message_id)
    if automation_id is not None:
        rows = conn.execute(
            """
            SELECT source_chat_id, source_message_id, automation_id,
                   source_grouped_id, destination_chat_id, destination_message_id
            FROM published_links
            WHERE source_chat_id=? AND source_message_id=? AND automation_id=?
            """,
            (source_chat_id, source_message_id, str(automation_id)),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT source_chat_id, source_message_id, automation_id,
                   source_grouped_id, destination_chat_id, destination_message_id
            FROM published_links
            WHERE source_chat_id=? AND source_message_id=?
            """,
            (source_chat_id, source_message_id),
        ).fetchall()

    return [
        {
            "source_chat_id": row[0],
            "source_message_id": int(row[1]),
            "automation_id": row[2],
            "source_grouped_id": row[3],
            "destination_chat_id": row[4],
            "destination_message_id": int(row[5]),
        }
        for row in rows
    ]


def _local_save(
    conn,
    automation_id,
    source_chat_id,
    source_message_id,
    destination_chat_id,
    destination_message_id,
    source_grouped_id=None,
):
    conn.execute(
        """
        INSERT INTO published_links (
            source_chat_id, source_message_id, automation_id,
            source_grouped_id, destination_chat_id, destination_message_id,
            published_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_chat_id, source_message_id, automation_id)
        DO UPDATE SET
            source_grouped_id=excluded.source_grouped_id,
            destination_chat_id=excluded.destination_chat_id,
            destination_message_id=excluded.destination_message_id,
            published_at=excluded.published_at
        """,
        (
            str(source_chat_id).strip(),
            int(source_message_id),
            str(automation_id),
            str(source_grouped_id) if source_grouped_id else None,
            str(destination_chat_id).strip(),
            int(destination_message_id),
            int(time.time()),
        ),
    )


def register(worker, session_key="primary"):
    path = _db_path()
    conn = _connect(path)

    original_find = worker.find_message_links
    original_save = worker.save_message_link
    original_remove = worker.remove_message_links
    original_claim_fingerprint = worker.claim_fingerprint

    async def durable_find_message_links(
        source_chat_id,
        source_message_id,
        automation_id=None,
    ):
        local = _local_find(
            conn,
            source_chat_id,
            source_message_id,
            automation_id,
        )
        if local:
            print(
                f"[Ledger:{session_key}] HIT source={source_message_id} "
                f"automation={automation_id or '*'}"
            )
            return local

        remote = await original_find(
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            automation_id=automation_id,
        )

        # Reidrata o ledger local quando o Lovable conhece o link.
        for link in remote or []:
            try:
                _local_save(
                    conn,
                    automation_id=link["automation_id"],
                    source_chat_id=link["source_chat_id"],
                    source_message_id=link["source_message_id"],
                    source_grouped_id=link.get("source_grouped_id"),
                    destination_chat_id=link["destination_chat_id"],
                    destination_message_id=link["destination_message_id"],
                )
            except Exception as error:
                print(
                    f"[Ledger:{session_key}] falha ao reidratar:",
                    type(error).__name__,
                    str(error),
                )
        return remote

    async def durable_save_message_link(
        automation_id,
        source_chat_id,
        source_message_id,
        destination_chat_id,
        destination_message_id,
        source_grouped_id=None,
    ):
        # CRITICO: persiste localmente PRIMEIRO. Se Lovable estiver fora,
        # recovery/restart ainda sabe que a mensagem ja foi publicada.
        _local_save(
            conn,
            automation_id=automation_id,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            source_grouped_id=source_grouped_id,
            destination_chat_id=destination_chat_id,
            destination_message_id=destination_message_id,
        )
        print(
            f"[Ledger:{session_key}] COMMIT source={source_message_id} "
            f"automation={automation_id} dest={destination_message_id}"
        )
        return await original_save(
            automation_id=automation_id,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            source_grouped_id=source_grouped_id,
            destination_chat_id=destination_chat_id,
            destination_message_id=destination_message_id,
        )

    async def durable_remove_message_links(
        source_chat_id,
        source_message_id,
        automation_id=None,
    ):
        if automation_id is None:
            conn.execute(
                "DELETE FROM published_links WHERE source_chat_id=? AND source_message_id=?",
                (str(source_chat_id).strip(), int(source_message_id)),
            )
        else:
            conn.execute(
                """
                DELETE FROM published_links
                WHERE source_chat_id=? AND source_message_id=? AND automation_id=?
                """,
                (
                    str(source_chat_id).strip(),
                    int(source_message_id),
                    str(automation_id),
                ),
            )
        return await original_remove(
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            automation_id=automation_id,
        )

    async def durable_claim_fingerprint(fingerprint):
        now = int(time.time())
        expires_at = now + max(30, DEFAULT_CLAIM_TTL)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT expires_at FROM fingerprint_claims WHERE fingerprint=?",
                (str(fingerprint),),
            ).fetchone()
            if row and int(row[0]) > now:
                conn.execute("COMMIT")
                return False

            conn.execute(
                """
                INSERT INTO fingerprint_claims (fingerprint, expires_at)
                VALUES (?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET expires_at=excluded.expires_at
                """,
                (str(fingerprint), expires_at),
            )
            conn.execute(
                "DELETE FROM fingerprint_claims WHERE expires_at < ?",
                (now - 86400,),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            # Em caso de falha do SQLite, ainda usa o mecanismo original.
            return await original_claim_fingerprint(fingerprint)

    worker.find_message_links = durable_find_message_links
    worker.save_message_link = durable_save_message_link
    worker.remove_message_links = durable_remove_message_links
    worker.claim_fingerprint = durable_claim_fingerprint

    print(
        f"[Ledger:{session_key}] idempotencia duravel ativa db={path} "
        f"claim_ttl={DEFAULT_CLAIM_TTL}s"
    )
    return path
