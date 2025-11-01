import disnake
from disnake.ext import commands, tasks
import asyncio
import websockets
import json
import os
from typing import Optional


class MinecraftCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.websocket = None
        self.connected = False
        self.voice_channel = None
        self.category_id = None  # ID категории где создавать канал
        self.server_status = {
            'online': False,
            'players': 0,
            'max_players': 0
        }

        # Конфигурация WebSocket
        self.WS_URL = "wss://websocket.teighto.net/ws"
        self.WS_TOKEN = "SUPER_SECRET"

        # Запускаем задачи
        self.connect_websocket.start()
        self.update_channel_name.start()

    def cog_unload(self):
        """Очистка при выгрузке кога"""
        self.connect_websocket.cancel()
        self.update_channel_name.cancel()
        if self.voice_channel:
            asyncio.create_task(self.cleanup_channel())

    @tasks.loop(seconds=30)
    async def connect_websocket(self):
        """Подключение к WebSocket серверу"""
        if self.connected:
            return

        try:
            self.websocket = await websockets.connect(self.WS_URL)
            await self.authenticate()
            self.connected = True
            print("Успешно подключен к WebSocket серверу Minecraft")

            # Слушаем сообщения
            asyncio.create_task(self.listen_websocket())

        except Exception as e:
            print(f"Ошибка подключения к WebSocket: {e}")
            self.connected = False

    @tasks.loop(seconds=60)
    async def update_channel_name(self):
        """Обновление названия голосового канала"""
        if not self.voice_channel or not self.category_id:
            return

        try:
            channel = self.bot.get_channel(self.voice_channel)
            if not channel:
                return

            status = self.server_status
            if status['online']:
                new_name = f"🟢 Minecraft: {status['players']}/{status['max_players']}"
            else:
                new_name = "🔴 Minecraft: Оффлайн"

            if channel.name != new_name:
                await channel.edit(name=new_name)

        except Exception as e:
            print(f"Ошибка обновления канала: {e}")

    async def authenticate(self):
        """Аутентификация на WebSocket сервере"""
        if self.websocket:
            auth_message = {
                "token": self.WS_TOKEN,
                "type": "auth"
            }
            await self.websocket.send(json.dumps(auth_message))

    async def listen_websocket(self):
        """Прослушивание сообщений от WebSocket сервера"""
        while self.connected:
            try:
                message = await self.websocket.recv()
                data = json.loads(message)
                await self.handle_websocket_message(data)

            except websockets.exceptions.ConnectionClosed:
                print("WebSocket соединение закрыто")
                self.connected = False
                break
            except Exception as e:
                print(f"Ошибка чтения WebSocket: {e}")
                self.connected = False
                break

    async def handle_websocket_message(self, data):
        """Обработка сообщений от WebSocket"""
        message_type = data.get('type')

        if message_type == 'status':
            self.server_status.update({
                'online': data.get('online', False),
                'players': data.get('players', 0),
                'max_players': data.get('max_players', 0)
            })
            print(f"Статус сервера обновлен: {self.server_status}")

    @commands.slash_command(name="setup_minecraft", description="Настройка Minecraft канала")
    @commands.has_permissions(administrator=True)
    async def setup_minecraft(self, inter: disnake.ApplicationCommandInteraction, category: disnake.CategoryChannel):
        """Настройка системы мониторинга Minecraft"""

        self.category_id = category.id

        # Создаем голосовой канал
        try:
            guild = inter.guild
            category_channel = guild.get_channel(self.category_id)

            # Удаляем старый канал если есть
            if self.voice_channel:
                old_channel = guild.get_channel(self.voice_channel)
                if old_channel:
                    await old_channel.delete()

            # Создаем новый канал
            voice_channel = await category_channel.create_voice_channel(
                name="🟡 Minecraft: Загрузка...",
                reason="Minecraft статус канал"
            )

            self.voice_channel = voice_channel.id

            embed = disnake.Embed(
                title="✅ Система Minecraft настроена",
                description=f"Канал создан в категории {category.name}",
                color=disnake.Color.green()
            )
            embed.add_field(
                name="Статус WebSocket",
                value="🟢 Подключен" if self.connected else "🔴 Отключен",
                inline=True
            )
            embed.add_field(
                name="Статус сервера",
                value="🟢 Онлайн" if self.server_status['online'] else "🔴 Оффлайн",
                inline=True
            )

            await inter.response.send_message(embed=embed, ephemeral=True)

        except Exception as e:
            embed = disnake.Embed(
                title="❌ Ошибка настройки",
                description=f"Произошла ошибка: {str(e)}",
                color=disnake.Color.red()
            )
            await inter.response.send_message(embed=embed, ephemeral=True)

    @commands.slash_command(name="minecraft_status", description="Показать текущий статус сервера Minecraft")
    async def minecraft_status(self, inter: disnake.ApplicationCommandInteraction):
        """Показать статус сервера Minecraft"""

        status = self.server_status

        embed = disnake.Embed(
            title="🟩 Статус сервера Minecraft",
            color=disnake.Color.green() if status['online'] else disnake.Color.red()
        )

        embed.add_field(
            name="Статус",
            value="🟢 **Онлайн**" if status['online'] else "🔴 **Оффлайн**",
            inline=True
        )

        embed.add_field(
            name="Игроки",
            value=f"**{status['players']}/{status['max_players']}**",
            inline=True
        )

        embed.add_field(
            name="WebSocket",
            value="🟢 Подключен" if self.connected else "🔴 Отключен",
            inline=True
        )

        if self.voice_channel:
            channel = self.bot.get_channel(self.voice_channel)
            if channel:
                embed.add_field(
                    name="Канал статуса",
                    value=channel.mention,
                    inline=False
                )

        await inter.response.send_message(embed=embed, ephemeral=True)

    async def cleanup_channel(self):
        """Очистка канала при выключении"""
        try:
            if self.voice_channel:
                channel = self.bot.get_channel(self.voice_channel)
                if channel:
                    await channel.delete()
        except:
            pass

    @connect_websocket.before_loop
    @update_channel_name.before_loop
    async def before_tasks(self):
        """Ожидание готовности бота перед запуском задач"""
        await self.bot.wait_until_ready()


def setup(bot):
    bot.add_cog(MinecraftCog(bot))