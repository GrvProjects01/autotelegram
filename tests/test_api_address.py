import pytest

from api_address import DEFAULT_API_URL, INVALID_MESSAGE, resolve_api_url


@pytest.mark.parametrize("value", [None, "", "   "])
def test_default_is_the_current_published_panel(value):
    assert resolve_api_url(value) == DEFAULT_API_URL
    assert DEFAULT_API_URL == "https://telegram-channel-automator-theta.vercel.app"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("http://127.0.0.1:8793", "http://127.0.0.1:8793"),
        (" http://127.0.0.1:8793/ ", "http://127.0.0.1:8793"),
        ("http://localhost:8793", "http://localhost:8793"),
        ("https://painel.example.com", "https://painel.example.com"),
    ],
)
def test_accepted_addresses(value, expected):
    assert resolve_api_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://painel.example.com",
        "http://127.0.0.1.example.com",
        "http://127.0.0.1@example.com",
        "https://user:senha@painel.example.com",
        "https://painel.example.com/api",
        "https://painel.example.com?x=1",
        "http://127.0.0.1:porta",
        "127.0.0.1:8793",
        "ftp://127.0.0.1",
        "https://",
    ],
)
def test_rejected_addresses_do_not_echo_the_value(value):
    with pytest.raises(ValueError) as error:
        resolve_api_url(value)
    assert str(error.value) == INVALID_MESSAGE
