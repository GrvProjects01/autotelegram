from types import SimpleNamespace

import video_preview_addon as preview


def _thumb(name="PhotoSize", w=320, h=180):
    cls = type(name, (), {})
    value = cls()
    value.w = w
    value.h = h
    return value


def _video(thumbs):
    attr_cls = type("DocumentAttributeVideo", (), {})
    attr = attr_cls()
    document = SimpleNamespace(
        mime_type="video/mp4",
        attributes=[attr],
        thumbs=thumbs,
    )
    return SimpleNamespace(document=document)


def test_selects_largest_photo_thumbnail_up_to_320():
    source = _video([
        _thumb(w=90, h=90),
        _thumb(w=320, h=180),
        _thumb(w=640, h=360),
        _thumb(name="VideoSize", w=320, h=180),
    ])
    selected = preview._select_photo_thumb(source)
    assert selected.w == 320
    assert selected.h == 180


def test_detects_video_by_document_attribute():
    source = _video([_thumb()])
    source.document.mime_type = "application/octet-stream"
    assert preview._is_video(source) is True


def test_ignores_non_video_documents():
    source = SimpleNamespace(
        document=SimpleNamespace(
            mime_type="application/pdf",
            attributes=[],
            thumbs=[_thumb()],
        )
    )
    assert preview._is_video(source) is False
