from time import sleep

import disnake
from disnake.ext import commands
import os
from dotenv import load_dotenv

load_dotenv("token.env")


def main():
    intents = disnake.Intents.all()
    bot = commands.Bot(
        command_prefix="!",
        intents=intents,
        help_command=None
    )

    # Загружаем коги при запуске
    @bot.event
    async def on_ready():

        # Загружаем ког статистики
        try:
            bot.load_extension('cogs.stats')
            print("\033[32m\033[1m[✅]\033[0m\033[0m \033[1mКог загружен:\033[0m \033[32m\033[1mstats\033[0m\033[0m")
        except Exception as e:
            print(f"\033[1m\033[31m[❌]\033[0m\033[0m \033[1mОшибка загрузки кога:\033[0m \033[1m\033[31m{e}\033[0m\033[0m")

        try:
            bot.load_extension('cogs.audit')
            print("\033[32m\033[1m[✅]\033[0m\033[0m \033[1mКог загружен:\033[0m \033[32m\033[1maudit\033[0m\033[0m")
        except Exception as e:
            print(f"\033[1m\033[31m[❌]\033[0m\033[0m \033[1mОшибка загрузки кога:\033[0m \033[1m\033[31m{e}\033[0m\033[0m")

        try:
            bot.load_extension('cogs.mod')
            print("\033[32m\033[1m[✅]\033[0m\033[0m \033[1mКог загружен:\033[0m \033[32m\033[1mmod\033[0m\033[0m")
        except Exception as e:
            print(f"\033[1m\033[31m[❌]\033[0m\033[0m \033[1mОшибка загрузки кога:\033[0m \033[1m\033[31m{e}\033[0m\033[0m")
        try:
            bot.load_extension('cogs.autorole')
            print("\033[32m\033[1m[✅]\033[0m\033[0m \033[1mКог загружен:\033[0m \033[32m\033[1mautorole\033[0m\033[0m")
        except Exception as e:
            print(f"\033[1m\033[31m[❌]\033[0m\033[0m \033[1mОшибка загрузки кога:\033[0m \033[1m\033[31m{e}\033[0m\033[0m")
        try:
            bot.load_extension('cogs.websocket')
            print("\033[32m\033[1m[✅]\033[0m\033[0m \033[1mКог загружен:\033[0m \033[32m\033[1mwebsocket\033[0m\033[0m")
        except Exception as e:
            print(f"\033[1m\033[31m[❌]\033[0m\033[0m \033[1mОшибка загрузки кога:\033[0m \033[1m\033[31m{e}\033[0m\033[0m")


        await bot.change_presence(
            activity=disnake.Activity(
                type=disnake.ActivityType.watching,
                name="статистику сервера"
            )
        )


    @bot.event
    async def on_command_error(error):
        if isinstance(error, commands.CommandNotFound):
            return
        print(f"Произошла ошибка: {error}")

    sleep(1)
    print(f"\033[1m\033[94m[🧊]\033[0m\033[0m \033[94mЗапуск служб...\033[0m")

    sleep(2)
    print(f"\033[1m[🤍]\033[0m \033[1mБот создан: lennivyy\033[0m" )

    token = os.getenv('BOT_TOKEN')
    if not token:
        print("Ошибка: BOT_TOKEN не найден в token.env файле!")
    else:
        bot.run(token)


if __name__ == "__main__":
    main()