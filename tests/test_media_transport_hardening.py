from types import SimpleNamespace

import media_transport_hardening as hardening


def _document_message(size=100, mime="video/mp4"):
    document = SimpleNamespace(
        size=size,
        mime_type=mime,
        attributes=[],
    )
    media = SimpleNamespace(document=document, photo=None)
    return SimpleNamespace(media=media, id=123)


def _photo_message():
    media = SimpleNamespace(document=None, photo=SimpleNamespace(id=1))
    return SimpleNamespace(media=media, id=123)


def test_integrity_accepts_matching_document_size():
    expected = {"size": 100}
    assert hardening._integrity_problem(expected, _document_message(100)) is None


def test_integrity_rejects_missing_media():
    expected = {"size": 100}
    assert hardening._integrity_problem(expected, SimpleNamespace(media=None)) == "telegram_refetch_sem_midia"


def test_integrity_rejects_document_size_mismatch():
    expected = {"size": 100}
    problem = hardening._integrity_problem(expected, _document_message(99))
    assert problem == "tamanho_remoto_divergente:99!=100"


def test_integrity_accepts_photo_without_document_size():
    assert hardening._integrity_problem(None, _photo_message()) is None


def test_metadata_diff_includes_size():
    diff = hardening._metadata_diff({"size": 100}, {"size": 99})
    assert "size:100!=99" in diff
