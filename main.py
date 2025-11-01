from time import sleep
import os

import disnake
from disnake.ext import commands
from dotenv import load_dotenv

# Загружаем переменные окружения из локального файла внутри контейнера
# (файл проброшен docker compose'ом)
load_dotenv("token.env")


def clean_secret(val: str | None) -> str:
    """
    Убирает переносы строк и \r, подрезает пробелы.
    Discord-токены всегда однострочные.
    """
    if not val:
        return ""
    return val.replace("\r", "").replace("\n", "").strip()


def main():
    intents = disnake.Intents.all()
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    # Загружаем коги при запуске
    @bot.event
    async def on_ready():
        for ext in ("cogs.stats", "cogs.audit", "cogs.mod", "cogs.autorole", "cogs.websocket"):
            try:
                bot.load_extension(ext)
                print(f"\033[32m\033[1m[✅]\033[0m Загружен ког: \033[32m{ext}\033[0m")
            except Exception as e:
                print(f"\033[31m\033[1m[❌]\033[0m Ошибка загрузки {ext}: {e}")

        await bot.change_presence(
            activity=disnake.Activity(type=disnake.ActivityType.watching, name="статистику сервера")
        )

    # Исправленная сигнатура обработчика ошибок
    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        print(f"Произошла ошибка команды: {error}")

    sleep(1)
    print("\033[94m\033[1m[🧊]\033[0m Запуск служб...")

    sleep(1)
    print("\033[1m[🤍]\033[0m Бот: lennivyy")

    # === ВАЖНО: достаём токен и очищаем его от CR/LF ===
    token_raw = os.getenv("BOT_TOKEN")  # приходит из env или из token.env
    token = clean_secret(token_raw)

    if not token:
        print("Ошибка: BOT_TOKEN не найден (проверьте token.env или env_file в docker compose).")
        raise SystemExit(1)

    # Мини-диагностика без утечки секрета
    print(f"Токен прочитан: длина={len(token)}, маска={token[:4]}...{token[-4:]}")

    try:
        bot.run(token)
    except Exception as e:
        print(f"Критическая ошибка запуска бота: {e}")
        raise


if __name__ == "__main__":
    main()