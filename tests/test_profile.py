import json
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    GetMe,
    SetChatMenuButton,
    SetMyCommands,
    SetMyDescription,
    SetMyName,
    SetMyProfilePhoto,
    SetMyShortDescription,
)
from aiogram.types import FSInputFile, User

from bot import setup_profile
from bot.profile import (
    BOT_DESCRIPTION,
    BOT_NAME,
    BOT_SHORT_DESCRIPTION,
    COMMANDS,
    REQUEST_TIMEOUT_SECONDS,
)

TEST_TOKEN = "123456789:abcdefghijklmnopqrstuvwxyz123456789"


class ProfileTelegram(BaseSession):
    def __init__(self, *, fail=False):
        super().__init__()
        self.calls = []
        self.closed = False
        self.fail = fail

    async def close(self):
        self.closed = True

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
        self.calls.append(method)
        assert timeout == REQUEST_TIMEOUT_SECONDS
        if self.fail:
            raise RuntimeError(f"Request failed for secret token {TEST_TOKEN}")
        if isinstance(method, GetMe):
            return User(
                id=123456789, is_bot=True, first_name=BOT_NAME, username="calendar_test_bot"
            )
        return True

    async def stream_content(
        self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True  # noqa: ASYNC109
    ):
        raise AssertionError("The profile setup must not download anything")
        yield b""  # pragma: no cover


@pytest.fixture
def avatar(tmp_path):
    path = tmp_path / "avatar.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0offline-upload-fixture\xff\xd9")
    return path


def test_profile_fits_telegram_limits():
    assert 0 < len(BOT_NAME) <= 64
    assert 0 < len(BOT_SHORT_DESCRIPTION) <= 120
    assert 0 < len(BOT_DESCRIPTION) <= 512
    assert len(COMMANDS) <= 100
    assert len({command for command, _ in COMMANDS}) == len(COMMANDS)


def test_dry_run_needs_no_credentials_or_network(monkeypatch, tmp_path, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Dry-run must not load credentials or create a Telegram client")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(setup_profile, "ProfileSettings", forbidden)
    monkeypatch.setattr(setup_profile, "Bot", forbidden)
    assert setup_profile.main(["--dry-run"]) == 0
    output = capsys.readouterr().out
    assert BOT_NAME in output
    assert BOT_SHORT_DESCRIPTION in output
    assert BOT_DESCRIPTION in output
    assert str(setup_profile.AVATAR_PATH) in output
    assert "/menu — Главное меню" in output
    assert list(tmp_path.iterdir()) == []


def test_profile_settings_need_only_the_token(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    settings = setup_profile.ProfileSettings()
    assert settings.bot_token.get_secret_value() == TEST_TOKEN
    assert TEST_TOKEN not in repr(settings)


async def test_profile_payloads_and_multipart_photo(avatar):
    transport = ProfileTelegram()
    bot = Bot(TEST_TOKEN, session=transport)
    await setup_profile.apply_profile(bot, avatar)
    assert len(transport.calls) == 10
    for method_type, field, expected in (
        (SetMyName, "name", BOT_NAME),
        (SetMyShortDescription, "short_description", BOT_SHORT_DESCRIPTION),
        (SetMyDescription, "description", BOT_DESCRIPTION),
    ):
        calls = [call for call in transport.calls if isinstance(call, method_type)]
        assert [call.language_code for call in calls] == ["", "ru"]
        assert all(getattr(call, field) == expected for call in calls)

    command_calls = [call for call in transport.calls if isinstance(call, SetMyCommands)]
    assert [call.language_code for call in command_calls] == ["", "ru"]
    assert all(
        [(command.command, command.description) for command in call.commands] == list(COMMANDS)
        for call in command_calls
    )
    menu = next(call for call in transport.calls if isinstance(call, SetChatMenuButton))
    assert menu.chat_id is None and menu.menu_button.type == "commands"

    photo_calls = [call for call in transport.calls if isinstance(call, SetMyProfilePhoto)]
    assert len(photo_calls) == 1
    files = {}
    photo_payload = json.loads(transport.prepare_value(photo_calls[0].photo, bot=bot, files=files))
    assert photo_payload["type"] == "static"
    assert photo_payload["photo"].startswith("attach://")
    upload = files[photo_payload["photo"].removeprefix("attach://")]
    assert isinstance(upload, FSInputFile)
    assert Path(upload.path) == avatar
    chunks = [chunk async for chunk in upload.read(bot)]
    assert b"".join(chunks) == avatar.read_bytes()


@pytest.mark.parametrize("missing", [True, False])
async def test_invalid_avatar_fails_before_remote_writes(tmp_path, missing):
    transport = ProfileTelegram()
    bot = Bot(TEST_TOKEN, session=transport)
    path = tmp_path / "avatar.jpg"
    if not missing:
        path.write_text("This is not a JPEG")
    with pytest.raises(FileNotFoundError if missing else ValueError):
        await setup_profile.apply_profile(bot, path)
    assert transport.calls == []


@pytest.mark.parametrize("fail", [False, True])
async def test_session_closes_after_setup_success_or_failure(monkeypatch, avatar, fail):
    transport = ProfileTelegram(fail=fail)
    bot = Bot(TEST_TOKEN, session=transport)
    monkeypatch.setattr(setup_profile, "Bot", lambda token: bot)
    monkeypatch.setattr(
        setup_profile,
        "ProfileSettings",
        lambda: type("Settings", (), {"bot_token": setup_profile.SecretStr(TEST_TOKEN)})(),
    )
    if fail:
        with pytest.raises(RuntimeError):
            await setup_profile.run_setup(avatar)
    else:
        assert await setup_profile.run_setup(avatar) == "@calendar_test_bot"
        assert isinstance(transport.calls[0], GetMe)
    assert transport.closed


def test_cli_does_not_print_sensitive_api_error(monkeypatch, capsys):
    async def fail(avatar_path):
        raise RuntimeError(f"Sensitive API response includes {TEST_TOKEN}")

    monkeypatch.setattr(setup_profile, "run_setup", fail)
    assert setup_profile.main([]) == 1
    error = capsys.readouterr().err
    assert "RuntimeError" in error
    assert TEST_TOKEN not in error
    assert "Sensitive API response" not in error
