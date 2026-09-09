from types import SimpleNamespace

import historical_backfill as history


def test_empty_service_message_is_not_publishable():
    message = SimpleNamespace(message="", media=None)
    assert history._has_publishable_content(message) is False


def test_text_message_is_publishable():
    message = SimpleNamespace(message="Oferta ativa", media=None)
    assert history._has_publishable_content(message) is True


def test_photo_message_without_caption_is_publishable():
    media = SimpleNamespace(photo=object(), document=None)
    message = SimpleNamespace(message="", media=media)
    assert history._has_publishable_content(message) is True


def test_document_message_without_caption_is_publishable():
    media = SimpleNamespace(photo=None, document=object())
    message = SimpleNamespace(message="", media=media)
    assert history._has_publishable_content(message) is True


def test_unit_is_publishable_when_any_item_has_content():
    empty = SimpleNamespace(message="", media=None)
    text = SimpleNamespace(message="ok", media=None)
    assert history._unit_has_publishable_content([empty, text]) is True
    assert history._unit_has_publishable_content([empty]) is False
