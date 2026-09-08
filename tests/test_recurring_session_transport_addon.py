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


def test_register_patches_single_scheduler():
    session_transport.register()
    assert recurring._is_valid is session_transport._is_valid
    assert recurring._process_one is session_transport._process_one
