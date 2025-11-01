import disnake
from disnake.ext import commands
import datetime
import json
import os
from typing import Optional


class ChatLogger(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config_file = "chat_logger_config.json"
        self.load_config()

    def load_config(self):
        """Загрузить настройки из файла"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)

                self.voice_log_channel_id = config.get('voice_log_channel_id')
                self.text_log_channel_id = config.get('text_log_channel_id')
                self.ignored_channels = config.get('ignored_channels', [])
            except Exception as e:
                print(f"❌ Ошибка загрузки настроек: {e}")
                self.set_default_config()
        else:
            self.set_default_config()

    def set_default_config(self):
        """Установить настройки по умолчанию"""
        self.voice_log_channel_id = None
        self.text_log_channel_id = None
        self.ignored_channels = []
        self.save_config()

    def save_config(self):
        """Сохранить настройки в файл"""
        try:
            config = {
                'voice_log_channel_id': self.voice_log_channel_id,
                'text_log_channel_id': self.text_log_channel_id,
                'ignored_channels': self.ignored_channels
            }

            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)

            print("✅ Настройки чат-логгера сохранены")
        except Exception as e:
            print(f"❌ Ошибка сохранения настроек: {e}")


    # ===== ЛОГИРОВАНИЕ ГОЛОСОВЫХ КАНАЛОВ =====

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if not self.voice_log_channel_id:
            return

        log_channel = self.bot.get_channel(self.voice_log_channel_id)
        if not log_channel:
            return

        # Игнорируем изменения, связанные с афком
        if before.afk != after.afk:
            return

        embed = disnake.Embed(
            color=disnake.Color.blue(),
            timestamp=datetime.datetime.now()
        )

        # Пользователь зашел в войс
        if before.channel is None and after.channel is not None:
            embed.title = "🎤 Пользователь зашел в войс"
            embed.color = disnake.Color.green()
            embed.description = f"**{member.display_name}** зашел в канал **{after.channel.name}**"
            embed.add_field(name="Канал", value=f"{after.channel.mention}", inline=True)

        # Пользователь вышел из войса
        elif before.channel is not None and after.channel is None:
            embed.title = "🚪 Пользователь вышел из войса"
            embed.color = disnake.Color.red()
            embed.description = f"**{member.display_name}** вышел из канала **{before.channel.name}**"
            embed.add_field(name="Канал", value=f"{before.channel.name}", inline=True)

        # Пользователь перешел в другой войс
        elif before.channel is not None and after.channel is not None and before.channel != after.channel:
            embed.title = "🔄 Пользователь перешел в другой войс"
            embed.color = disnake.Color.orange()
            embed.description = f"**{member.display_name}** перешел из **{before.channel.name}** в **{after.channel.name}**"
            embed.add_field(name="Из канала", value=f"{before.channel.mention}", inline=True)
            embed.add_field(name="В канал", value=f"{after.channel.mention}", inline=True)

        # Пользователь включил/выключил себе звук
        elif before.self_mute != after.self_mute:
            action = "🔇 заглушил" if after.self_mute else "🔊 включил"
            embed.title = "Изменение статуса микрофона"
            embed.color = disnake.Color.purple()
            embed.description = f"**{member.display_name}** {action} себе микрофон"

        # Пользователь включил/выключил звук другим
        elif before.self_deaf != after.self_deaf:
            action = "🎧 заглушил" if after.self_deaf else "🎧 включил"
            embed.title = "Изменение статуса звука"
            embed.color = disnake.Color.purple()
            embed.description = f"**{member.display_name}** {action} себе звук"

        else:
            return

        # Добавляем информацию о пользователе
        embed.add_field(name="Пользователь", value=f"{member.mention} (`{member.name}`)", inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"ID: {member.id}")

        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"Ошибка отправки голосового лога: {e}")

    # ===== ЛОГИРОВАНИЕ ТЕКСТОВЫХ СООБЩЕНИЙ =====

    @commands.Cog.listener()
    async def on_message(self, message):
        # Игнорируем сообщения ботов и если не настроен канал для логов
        if message.author.bot or not self.text_log_channel_id:
            return

        # Проверяем, не в игнор-листе ли канал
        if message.channel.id in self.ignored_channels:
            return

        log_channel = self.bot.get_channel(self.text_log_channel_id)
        if not log_channel:
            return

        # Игнорируем команды
        if message.content.startswith(tuple(self.bot.command_prefix)):
            return

        embed = disnake.Embed(
            title="💬 Новое сообщение",
            color=disnake.Color.blurple(),
            timestamp=message.created_at,
            description=f"```{message.content}```"
        )

        embed.add_field(name="Автор", value=f"{message.author.mention} (`{message.author.name}`)", inline=True)
        embed.add_field(name="Канал", value=f"{message.channel.mention}", inline=True)
        embed.add_field(name="ID сообщения", value=f"`{message.id}`", inline=True)

        # Добавляем вложения если есть
        if message.attachments:
            attachment_info = []
            for i, attachment in enumerate(message.attachments[:3]):  # Ограничиваем количество
                attachment_info.append(f"[Вложение {i + 1}]({attachment.url})")

            embed.add_field(name="📎 Вложения", value="\n".join(attachment_info), inline=False)

            # Показываем превью первого изображения
            if message.attachments[0].content_type and message.attachments[0].content_type.startswith('image/'):
                embed.set_image(url=message.attachments[0].url)

        embed.set_thumbnail(url=message.author.display_avatar.url)
        embed.set_footer(text=f"ID пользователя: {message.author.id}")

        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"Ошибка отправки текстового лога: {e}")

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        # Игнорируем если сообщение не изменилось или это бот
        if before.content == after.content or after.author.bot or not self.text_log_channel_id:
            return

        # Проверяем игнор-лист
        if after.channel.id in self.ignored_channels:
            return

        log_channel = self.bot.get_channel(self.text_log_channel_id)
        if not log_channel:
            return

        embed = disnake.Embed(
            title="✏️ Сообщение изменено",
            color=disnake.Color.gold(),
            timestamp=datetime.datetime.now()
        )

        embed.add_field(name="Автор", value=f"{after.author.mention} (`{after.author.name}`)", inline=True)
        embed.add_field(name="Канал", value=f"{after.channel.mention}", inline=True)
        embed.add_field(name="ID сообщения", value=f"`{after.id}`", inline=True)

        # Обрезаем длинные сообщения
        old_content = before.content[:1000] + "..." if len(before.content) > 1000 else before.content
        new_content = after.content[:1000] + "..." if len(after.content) > 1000 else after.content

        embed.add_field(name="Было", value=f"```{old_content}```" if not None else "*пусто*", inline=False)
        embed.add_field(name="Стало", value=f"```{new_content}```", inline=False)

        embed.set_thumbnail(url=after.author.display_avatar.url)
        embed.set_footer(text=f"ID пользователя: {after.author.id}")

        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"Ошибка отправки лога редактирования: {e}")

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        # Игнорируем ботов и если не настроен канал
        if message.author.bot or not self.text_log_channel_id:
            return

        # Проверяем игнор-лист
        if message.channel.id in self.ignored_channels:
            return

        log_channel = self.bot.get_channel(self.text_log_channel_id)
        if not log_channel:
            return

        embed = disnake.Embed(
            title="🗑️ Сообщение удалено",
            color=disnake.Color.dark_red(),
            timestamp=datetime.datetime.now()
        )

        embed.add_field(name="Автор", value=f"{message.author.mention} (`{message.author.name}`)", inline=True)
        embed.add_field(name="Канал", value=f"{message.channel.mention}", inline=True)
        embed.add_field(name="ID сообщения", value=f"`{message.id}`", inline=True)

        # Обрезаем длинное сообщение
        content = message.content[:1000] + "..." if len(message.content) > 1000 else message.content
        embed.add_field(name="Содержимое", value=content or "*пусто*", inline=False)

        # Информация о вложениях
        if message.attachments:
            embed.add_field(name="📎 Вложения", value=f"Удалено {len(message.attachments)} вложений", inline=False)

        embed.set_thumbnail(url=message.author.display_avatar.url)
        embed.set_footer(text=f"ID пользователя: {message.author.id}")

        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"Ошибка отправки лога удаления: {e}")

    # ===== ЛОГИРОВАНИЕ РЕАКЦИЙ =====

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        """Логирование добавления реакции"""
        if user.bot or not self.text_log_channel_id:
            return

        # Проверяем игнор-лист
        if reaction.message.channel.id in self.ignored_channels:
            return

        log_channel = self.bot.get_channel(self.text_log_channel_id)
        if not log_channel:
            return

        embed = disnake.Embed(
            title="✅ Реакция добавлена",
            color=disnake.Color.green(),
            timestamp=datetime.datetime.now()
        )

        embed.add_field(name="Пользователь", value=f"{user.mention} (`{user.name}`)", inline=True)
        embed.add_field(name="Канал", value=f"{reaction.message.channel.mention}", inline=True)
        embed.add_field(name="Реакция", value=f"{reaction.emoji}", inline=True)
        embed.add_field(name="ID сообщения", value=f"`{reaction.message.id}`", inline=True)

        # Добавляем ссылку на сообщение
        embed.add_field(
            name="Ссылка на сообщение",
            value=f"[Перейти к сообщению]({reaction.message.jump_url})",
            inline=False
        )

        # Добавляем текст сообщения (обрезанный)
        message_content = reaction.message.content[:500] + "..." if len(
            reaction.message.content) > 500 else reaction.message.content
        embed.add_field(
            name="Текст сообщения",
            value=f"```{message_content or 'Нет текста'}```",
            inline=False
        )

        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text=f"ID пользователя: {user.id}")

        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"Ошибка отправки лога добавления реакции: {e}")

    @commands.Cog.listener()
    async def on_reaction_remove(self, reaction, user):
        """Логирование удаления реакции"""
        if user.bot or not self.text_log_channel_id:
            return

        # Проверяем игнор-лист
        if reaction.message.channel.id in self.ignored_channels:
            return

        log_channel = self.bot.get_channel(self.text_log_channel_id)
        if not log_channel:
            return

        embed = disnake.Embed(
            title="❌ Реакция удалена",
            color=disnake.Color.red(),
            timestamp=datetime.datetime.now()
        )

        embed.add_field(name="Пользователь", value=f"{user.mention} (`{user.name}`)", inline=True)
        embed.add_field(name="Канал", value=f"{reaction.message.channel.mention}", inline=True)
        embed.add_field(name="Реакция", value=f"{reaction.emoji}", inline=True)
        embed.add_field(name="ID сообщения", value=f"`{reaction.message.id}`", inline=True)

        # Добавляем ссылку на сообщение
        embed.add_field(
            name="Ссылка на сообщение",
            value=f"[Перейти к сообщению]({reaction.message.jump_url})",
            inline=False
        )

        # Добавляем текст сообщения (обрезанный)
        message_content = reaction.message.content[:500] + "..." if len(
            reaction.message.content) > 500 else reaction.message.content
        embed.add_field(
            name="Текст сообщения",
            value=f"```{message_content or 'Нет текста'}```",
            inline=False
        )

        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text=f"ID пользователя: {user.id}")

        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"Ошибка отправки лога удаления реакции: {e}")

    @commands.Cog.listener()
    async def on_reaction_clear(self, message, reactions):
        """Логирование очистки всех реакций с сообщения"""
        if not self.text_log_channel_id:
            return

        # Проверяем игнор-лист
        if message.channel.id in self.ignored_channels:
            return

        log_channel = self.bot.get_channel(self.text_log_channel_id)
        if not log_channel:
            return

        embed = disnake.Embed(
            title="🧹 Все реакции очищены",
            color=disnake.Color.orange(),
            timestamp=datetime.datetime.now()
        )

        embed.add_field(name="Канал", value=f"{message.channel.mention}", inline=True)
        embed.add_field(name="ID сообщения", value=f"`{message.id}`", inline=True)
        embed.add_field(name="Количество реакций", value=f"`{len(reactions)}`", inline=True)

        # Добавляем список очищенных реакций
        if reactions:
            reactions_list = [str(reaction.emoji) for reaction in reactions[:10]]  # Ограничиваем количество
            reactions_text = ", ".join(reactions_list)
            if len(reactions) > 10:
                reactions_text += f" и еще {len(reactions) - 10}"

            embed.add_field(name="Очищенные реакции", value=reactions_text, inline=False)

        # Добавляем текст сообщения (обрезанный)
        message_content = message.content[:500] + "..." if len(message.content) > 500 else message.content
        embed.add_field(
            name="Текст сообщения",
            value=f"```{message_content or 'Нет текста'}```",
            inline=False
        )

        embed.add_field(
            name="Ссылка на сообщение",
            value=f"[Перейти к сообщению]({message.jump_url})",
            inline=False
        )

        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"Ошибка отправки лога очистки реакций: {e}")

def setup(bot):
    bot.add_cog(ChatLogger(bot))