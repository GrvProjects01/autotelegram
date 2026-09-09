from telethon import types

import telegram_rich_text


def test_builds_basic_formatting_entities():
    item = {
        "message_entities": [
            {"type": "bold", "offset": 0, "length": 4},
            {"type": "underline", "offset": 5, "length": 4},
        ]
    }
    entities = telegram_rich_text.build_entities(item, "TEST TEXT")
    assert isinstance(entities[0], types.MessageEntityBold)
    assert isinstance(entities[1], types.MessageEntityUnderline)


def test_builds_custom_emoji_entity_with_utf16_offsets():
    # 😀 ocupa 2 unidades UTF-16; o contrato do painel usa o mesmo modelo.
    item = {
        "message_entities": [
            {
                "type": "custom_emoji",
                "offset": 0,
                "length": 2,
                "custom_emoji_id": "5368324170671202286",
            }
        ]
    }
    entities = telegram_rich_text.build_entities(item, "😀 oferta")
    assert len(entities) == 1
    assert isinstance(entities[0], types.MessageEntityCustomEmoji)
    assert entities[0].document_id == 5368324170671202286


def test_rejects_out_of_bounds_and_unsafe_text_url():
    item = {
        "message_entities": [
            {"type": "bold", "offset": 100, "length": 2},
            {"type": "text_url", "offset": 0, "length": 4, "url": "javascript:alert(1)"},
        ]
    }
    assert telegram_rich_text.build_entities(item, "TEST") == []
