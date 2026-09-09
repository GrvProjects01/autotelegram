import time

import recurring_messages_addon as recurring
import recurring_session_transport_addon as session_transport


def test_session_transport_validation_accepts_session_without_bot_key():
    item = {
        "id": "rec-1",
        "enabled": True,
        "transport": "session",
        "worker_session_key": "primary",
        "destination_chat_id": "-100123",
        "message_text": "teste",
    }
    assert session_transport._is_valid(item) is True


def test_bot_transport_still_requires_bot_key():
    item = {
        "id": "rec-2",
        "enabled": True,
        "transport": "bot",
        "worker_session_key": "primary",
        "destination_chat_id": "-100123",
        "message_text": "teste",
    }
    assert session_transport._is_valid(item) is False


def test_transport_aliases_are_normalized():
    assert session_transport._transport({"transport": "telegram_session"}) == "session"
    assert session_transport._transport({"transport": "publisher_bot"}) == "bot"


def test_session_key_aliases_are_supported():
    assert session_transport._session_key({"session_key": "Marca_B"}) == "marca_b"
    assert session_transport._session_key({"publisher_session_key": "PRIMARY"}) == "primary"
    assert session_transport._session_key({"telegram_session": "marca_b"}) == "marca_b"


def test_session_belongs_uses_aliases():
    item = {
        "transport": "session",
        "session_key": "marca_b",
    }
    assert session_transport._belongs_to_session(item, "marca_b") is True
    assert session_transport._belongs_to_session(item, "primary") is False


def test_destination_normalization_supports_id_username_and_link():
    assert session_transport._normalize_destination("-100123456") == "-100123456"
    assert session_transport._normalize_destination("@meucanal") == "meucanal"
    assert session_transport._normalize_destination("https://t.me/meucanal") == "meucanal"
    assert session_transport._normalize_destination("t.me/meucanal/123") == "meucanal"


def test_signature_changes_between_bot_and_session():
    common = {
        "destination_chat_id": "-100123",
        "interval_minutes": 15,
        "delete_previous": True,
        "send_immediately": False,
    }
    bot = dict(common, transport="bot", telegram_bot_key="closeflix", worker_session_key="primary")
    session = dict(common, transport="session", telegram_bot_key="", worker_session_key="primary")
    assert session_transport._config_signature(bot) != session_transport._config_signature(session)


class _Store:
    def __init__(self):
        self.updated = None


def test_bot_to_session_send_immediately_releases_persisted_clock(monkeypatch):
    store = _Store()
    state = {
        "last_bot_key": "closeflix",
        "next_run_at": time.time() + 800,
    }
    item = {
        "id": "rec-switch",
        "transport": "session",
        "send_immediately": True,
    }

    def fake_defer(target_store, recurring_id, next_run_at):
        target_store.updated = (recurring_id, next_run_at)

    monkeypatch.setattr(session_transport, "_defer_state", fake_defer)
    now = time.time()
    session_transport._adopt_session_transport_now(store, item, state, "primary", now)

    assert store.updated is not None
    assert store.updated[0] == "rec-switch"
    assert state["next_run_at"] == now


def test_register_patches_single_scheduler():
    session_transport.register()
    assert recurring._is_valid is session_transport._is_valid
    assert recurring._process_one is session_transport._process_one
    assert recurring._belongs_to_session is session_transport._belongs_to_session
