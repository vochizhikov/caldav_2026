import pytest

from bot.validators.settings import validate_login


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("calendar.user", "calendar.user"),
        ("  calendar.user  ", "calendar.user"),
        ("name@yandex.ru", "name@yandex.ru"),
        ("  Name@YANDEX.RU  ", "Name@yandex.ru"),
        ("name+calendar@example.org", "name+calendar@example.org"),
        ("name@gmail.com.example.org", "name@gmail.com.example.org"),
        ("name@sub.gmail.com", "name@sub.gmail.com"),
        ("name@notgmail.com", "name@notgmail.com"),
    ],
)
def test_login_preserves_short_logins_and_normalizes_email_domains(value, expected):
    assert validate_login(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "name@gmail.com",
        "name@GMAIL.COM",
        "  name+calendar@Gmail.Com  ",
    ],
)
def test_login_rejects_gmail(value):
    with pytest.raises(ValueError) as exc:
        validate_login(value)
    assert "gmail.com" in str(exc.value).lower()
    assert value.strip() not in str(exc.value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "name surname",
        "name\tsurname",
        "name\nsurname",
        "name:password",
        "x" * 321,
        "@yandex.ru",
        "name@",
        "name@@yandex.ru",
        "name@yandex",
        "name@.yandex.ru",
        "name@yandex..ru",
        "name..surname@yandex.ru",
        ".name@yandex.ru",
        "name.@yandex.ru",
        "name @yandex.ru",
        "name@ yandex.ru",
        "name@yandex.ru\nother@yandex.ru",
        "Name <name@yandex.ru>",
        "<name@yandex.ru>",
    ],
)
def test_login_rejects_malformed_text_and_email(value):
    with pytest.raises(ValueError) as exc:
        validate_login(value)
    assert str(exc.value)
    assert "validation error" not in str(exc.value).lower()
    assert "input_value" not in str(exc.value)


def test_login_error_does_not_echo_untrusted_text():
    value = "private-marker<script>@yandex.ru"
    with pytest.raises(ValueError) as exc:
        validate_login(value)
    assert "private-marker" not in str(exc.value)
    assert "<script>" not in str(exc.value)
