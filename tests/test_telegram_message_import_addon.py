from telethon import types

import telegram_message_import_addon as importer


def test_parse_public_message_link():
    peer, message_id, kind = importer.parse_message_link("https://t.me/meucanal/321")
    assert peer == "meucanal"
    assert message_id == 321
    assert kind == "public"


def test_parse_private_message_link():
    peer, message_id, kind = importer.parse_message_link("https://t.me/c/1234567890/77/654")
    assert peer == -1001234567890
    assert message_id == 654
    assert kind == "private"


def test_custom_emoji_serialized_as_string():
    entity = types.MessageEntityCustomEmoji(
        offset=0,
        length=2,
        document_id=5368324170671202286,
    )
    payload = importer._entity_payload(entity)
    assert payload == {
        "type": "custom_emoji",
        "offset": 0,
        "length": 2,
        "custom_emoji_id": "5368324170671202286",
    }


def test_rich_format_entities_are_preserved():
    bold = importer._entity_payload(types.MessageEntityBold(offset=0, length=4))
    link = importer._entity_payload(
        types.MessageEntityTextUrl(offset=5, length=4, url="https://example.com")
    )
    assert bold["type"] == "bold"
    assert link["type"] == "text_url"
    assert link["url"] == "https://example.com"
