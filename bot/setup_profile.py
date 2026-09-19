"""Preview or apply the bot profile without starting polling or accessing the database."""

import argparse
import asyncio
import sys
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InputProfilePhotoStatic
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from bot.profile import (
    AVATAR_PATH,
    BOT_DESCRIPTION,
    BOT_NAME,
    BOT_SHORT_DESCRIPTION,
    COMMANDS,
    PROFILE_LANGUAGES,
    REQUEST_TIMEOUT_SECONDS,
    configure_commands,
)

SETUP_TIMEOUT_SECONDS = 120


class ProfileSettings(BaseSettings):
    """Profile maintenance needs only the bot token, independent of runtime settings."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr


def validate_avatar(avatar_path: Path) -> None:
    """Read the upload before changing remote state, catching a missing or unreadable asset."""
    with avatar_path.open("rb") as avatar:
        if avatar.read(3) != b"\xff\xd8\xff":
            raise ValueError("Avatar must be a JPEG image")


async def apply_profile(bot: Bot, avatar_path: Path) -> None:
    validate_avatar(avatar_path)
    for language_code in PROFILE_LANGUAGES:
        await bot.set_my_name(
            name=BOT_NAME,
            language_code=language_code,
            request_timeout=REQUEST_TIMEOUT_SECONDS,
        )
        await bot.set_my_short_description(
            short_description=BOT_SHORT_DESCRIPTION,
            language_code=language_code,
            request_timeout=REQUEST_TIMEOUT_SECONDS,
        )
        await bot.set_my_description(
            description=BOT_DESCRIPTION,
            language_code=language_code,
            request_timeout=REQUEST_TIMEOUT_SECONDS,
        )
    await configure_commands(bot)
    await bot.set_my_profile_photo(
        photo=InputProfilePhotoStatic(photo=FSInputFile(avatar_path)),
        request_timeout=REQUEST_TIMEOUT_SECONDS,
    )


async def run_setup(avatar_path: Path) -> str:
    validate_avatar(avatar_path)
    settings = ProfileSettings()
    bot = Bot(settings.bot_token.get_secret_value())
    try:
        async with asyncio.timeout(SETUP_TIMEOUT_SECONDS):
            identity = await bot.get_me(request_timeout=REQUEST_TIMEOUT_SECONDS)
            await apply_profile(bot, avatar_path)
            return f"@{identity.username}" if identity.username else str(identity.id)
    finally:
        await bot.session.close()


def print_preview(avatar_path: Path) -> None:
    print(f"Название: {BOT_NAME}")
    print(f"\nКраткое описание ({len(BOT_SHORT_DESCRIPTION)}/120):\n{BOT_SHORT_DESCRIPTION}")
    print(f"\nОписание ({len(BOT_DESCRIPTION)}/512):\n{BOT_DESCRIPTION}")
    print(f"\nАватар: {avatar_path}")
    print("\nЯзыки: по умолчанию и русский (ru)")
    print("Кнопка меню: команды")
    print("\nКоманды:")
    for command, description in COMMANDS:
        print(f"/{command} — {description}")
    print("\nПредпросмотр: запросы в Telegram не отправлялись.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Оформление профиля Telegram-бота")
    parser.add_argument(
        "--dry-run", action="store_true", help="Показать профиль без токена и запросов в Telegram"
    )
    args = parser.parse_args(argv)
    if args.dry_run:
        print_preview(AVATAR_PATH)
        return 0
    try:
        identity = asyncio.run(run_setup(AVATAR_PATH))
    except ValidationError:
        print("Укажите BOT_TOKEN в переменных окружения или файле .env.", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"Аватар не найден: {AVATAR_PATH}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Настройка профиля прервана.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(
            f"Не удалось настроить профиль ({type(exc).__name__}). "
            "Проверьте BOT_TOKEN, JPEG-аватар и соединение с Telegram. "
            "Часть настроек могла примениться; команду можно запустить повторно.",
            file=sys.stderr,
        )
        return 1
    print(f"Профиль {identity} обновлён: название, описания, команды, меню и аватар.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
