import time

import recurring_messages_addon as recurring


def test_interval_is_clamped():
    assert recurring._safe_interval("15") == 15
    assert recurring._safe_interval(0) == 1
    assert recurring._safe_interval(999999) == recurring.MAX_INTERVAL_MINUTES


def test_session_ownership_defaults_to_primary():
    item = {"id": "1"}
    assert recurring._belongs_to_session(item, "primary") is True
    assert recurring._belongs_to_session(item, "marca_b") is False


def test_valid_recurring_bot_message():
    item = {
        "id": "1",
        "enabled": True,
        "transport": "bot",
        "destination_chat_id": "-100123",
        "telegram_bot_key": "closeflix",
        "message_text": "Teste",
        "interval_minutes": 15,
    }
    assert recurring._is_valid(item) is True


def test_disabled_item_is_invalid():
    item = {
        "id": "1",
        "enabled": False,
        "transport": "bot",
        "destination_chat_id": "-100123",
        "telegram_bot_key": "closeflix",
        "message_text": "Teste",
    }
    assert recurring._is_valid(item) is False


def test_store_persists_schedule(tmp_path):
    store = recurring.RecurringStateStore(str(tmp_path / "state.sqlite3"))
    now = time.time()
    item = {
        "id": "abc",
        "enabled": True,
        "destination_chat_id": "-100123",
        "telegram_bot_key": "closeflix",
        "message_text": "Teste",
        "interval_minutes": 15,
        "send_immediately": False,
    }
    state = store.get_or_create(item, now)
    assert state["next_run_at"] >= now + 899

    store.commit_send("abc", 10, "closeflix", None, None, now + 1800, now)
    state2 = store.get_or_create(item, now + 1)
    assert state2["last_message_id"] == "10"


def test_default_state_path_is_isolated_per_session():
    primary = recurring._default_state_path("primary")
    marca_b = recurring._default_state_path("marca_b")
    assert primary != marca_b
    assert primary.endswith("recurring_messages_state_primary.sqlite3")
    assert marca_b.endswith("recurring_messages_state_marca_b.sqlite3")


def test_default_state_path_sanitizes_session_key():
    path = recurring._default_state_path("Marca B / VIP")
    assert path.endswith("recurring_messages_state_marca_b___vip.sqlite3")
