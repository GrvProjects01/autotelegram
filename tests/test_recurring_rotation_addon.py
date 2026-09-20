import os
import sqlite3
import tempfile
import time
from types import SimpleNamespace

import recurring_media_addon as media
import recurring_rotation_addon as rotation


class Store:
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute(
                """
                CREATE TABLE recurring_message_state (
                    recurring_id TEXT PRIMARY KEY,
                    config_signature TEXT NOT NULL DEFAULT '',
                    last_message_id TEXT,
                    last_bot_key TEXT,
                    pending_delete_message_id TEXT,
                    pending_delete_bot_key TEXT,
                    next_run_at REAL NOT NULL,
                    last_sent_at REAL,
                    updated_at REAL NOT NULL
                )
                """
            )
            db.execute(
                """
                INSERT INTO recurring_message_state
                (recurring_id, config_signature, next_run_at, updated_at)
                VALUES ('r1', '', 0, 0)
                """
            )
            db.commit()


def test_rotation_items_are_sorted_and_audio_becomes_voice():
    item = {
        "rotation_enabled": True,
        "rotation_items": [
            {"id": "b", "sort_order": 2, "kind": "audio", "media_url": "https://cdn.example/a.mp3"},
            {"id": "a", "sort_order": 1, "kind": "text", "message_text": "oi"},
        ],
    }
    variants = rotation._rotation_items(item)
    assert [v["id"] for v in variants] == ["a", "b"]

    merged = rotation._merge_variant(item, variants[1], 1)
    assert merged["media_type"] == "voice"
    assert merged["send_as_voice_note"] is True


def test_voice_aliases_are_native_voice_notes():
    assert media._media_type({"media_type": "voice_note"}) == "voice"
    assert media._send_as_voice_note({"media_type": "voice"}) is True
    assert media._send_as_voice_note({"media_type": "audio"}) is True
    assert media._send_as_voice_note({"media_type": "video"}) is False


def test_rotation_advances_only_after_confirmed_send(monkeypatch):
    rotation._registered = False

    async def valid(_item):
        return True

    # _is_valid precisa ser sync no contrato real.
    def sync_valid(_item):
        return True

    calls = []

    async def fake_process(_worker, store, item, _session_key):
        calls.append(item["_rotation_variant_key"])
        with sqlite3.connect(store.path) as db:
            db.execute(
                "UPDATE recurring_message_state SET last_sent_at=?, updated_at=? WHERE recurring_id='r1'",
                (time.time(), time.time()),
            )
            db.commit()

    rotation._original_is_valid = sync_valid
    rotation._original_process_one = fake_process
    rotation._original_config_signature = lambda item: "base"

    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.remove(path)
    try:
        store = Store(path)
        item = {
            "id": "r1",
            "rotation_enabled": True,
            "rotation_items": [
                {"id": "m1", "message_text": "um"},
                {"id": "m2", "message_text": "dois"},
            ],
        }

        import asyncio
        asyncio.run(rotation._process_one(SimpleNamespace(), store, item, "primary"))
        cursor, key, _ = rotation._read_rotation_state(store, "r1")
        assert calls == ["m1"]
        assert cursor == 1
        assert key == "m1"

        asyncio.run(rotation._process_one(SimpleNamespace(), store, item, "primary"))
        cursor, key, _ = rotation._read_rotation_state(store, "r1")
        assert calls == ["m1", "m2"]
        assert cursor == 0
        assert key == "m2"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def test_rotation_does_not_advance_when_send_not_committed():
    def sync_valid(_item):
        return True

    async def no_send(_worker, _store, _item, _session_key):
        return None

    rotation._original_is_valid = sync_valid
    rotation._original_process_one = no_send
    rotation._original_config_signature = lambda item: "base"

    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.remove(path)
    try:
        store = Store(path)
        item = {
            "id": "r1",
            "rotation_enabled": True,
            "rotation_items": [
                {"id": "m1", "message_text": "um"},
                {"id": "m2", "message_text": "dois"},
            ],
        }

        import asyncio
        asyncio.run(rotation._process_one(SimpleNamespace(), store, item, "primary"))
        cursor, key, _ = rotation._read_rotation_state(store, "r1")
        assert cursor == 0
        assert key is None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
