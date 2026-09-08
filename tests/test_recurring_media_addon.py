import recurring_media_addon as media


def test_accepts_image_only_bot_post():
    item = {
        "id": "media-1",
        "enabled": True,
        "transport": "bot",
        "worker_session_key": "primary",
        "destination_chat_id": "-100123",
        "telegram_bot_key": "closeflix",
        "message_text": "",
        "media_url": "https://example.com/test.jpg",
        "media_type": "image",
    }
    assert media._is_valid(item) is True


def test_accepts_video_with_caption_session_post():
    item = {
        "id": "media-2",
        "enabled": True,
        "transport": "session",
        "worker_session_key": "primary",
        "destination_chat_id": "-100123",
        "message_text": "Legenda",
        "media_url": "https://example.com/test.mp4",
        "media_type": "video",
    }
    assert media._is_valid(item) is True


def test_rejects_empty_post():
    item = {
        "id": "media-3",
        "enabled": True,
        "transport": "session",
        "worker_session_key": "primary",
        "destination_chat_id": "-100123",
        "message_text": "",
    }
    assert media._is_valid(item) is False


def test_rejects_http_media_url():
    assert media._safe_media_url("http://example.com/file.jpg") is False


def test_rejects_private_ip_media_url():
    assert media._safe_media_url("https://127.0.0.1/file.jpg") is False
