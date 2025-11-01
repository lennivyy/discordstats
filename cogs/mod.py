from operator import truediv

import disnake
from disnake.ext import commands
import asyncio
from datetime import datetime, timedelta
import re


class ModerationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.user_message_count = {}
        self.muted_users = set()

        # Списки запрещенных слов (религия и политика)
        self.religious_keywords = [
            'аллах', 'бог', 'ислам', 'христианство', 'иудаизм', 'буддизм',
            'коран', 'библия', 'тора', 'мечеть', 'церковь', 'синагога',
            'мусульман', 'христиан', 'иудей', 'буддист', 'религи', 'харам',
            'haram', 'HARAM', 'бисмилях', 'бисмиля', 'БИСМИЛЯХ', 'БИСМИЛЯ'
        ]

        self.political_keywords = [
            'политик', 'президент', 'правительство', 'государство', 'власть',
            'выборы', 'партия', 'оппозиция', 'демократия', 'диктатура',
            'коммунизм', 'социализм', 'либерализм', 'консерватизм',
            'парламент', 'министр', 'депутат', 'голосование', 'сво', 'zvo', 'ZVO',
            'ZvO', '1488', 'гитлер', 'нигер', 'негр', 'negr', 'нegr', 'nеgr', 'nегr', 'neгr',
            'neгр', 'нeгр', 'раса', '!488', 'zVO', 'Zvo', 'СВО', 'сВО', 'Сво', 'дрон', 'камикадзе',
            'украина', 'россия', 'УКРАИНА', 'РОССИЯ', 'фронт', 'окоп'
        ]

    @commands.Cog.listener()
    async def on_message(self, message):
        # Игнорируем сообщения от ботов
        if message.author.bot:
            return

        # Проверка на спам
        await self.check_spam(message)

        # Проверка на запрещенные темы
        await self.check_prohibited_content(message)

    async def check_spam(self, message):
        user_id = message.author.id
        current_time = datetime.now()

        # Инициализация счетчика для пользователя
        if user_id not in self.user_message_count:
            self.user_message_count[user_id] = []

        # Добавляем время сообщения
        self.user_message_count[user_id].append(current_time)

        # Очищаем старые сообщения (старше 10 секунд)
        self.user_message_count[user_id] = [
            msg_time for msg_time in self.user_message_count[user_id]
            if current_time - msg_time < timedelta(seconds=2)
        ]

        # Если 3 или более сообщений за 10 секунд - мут
        if len(self.user_message_count[user_id]) >= 3:
            await self.mute_user(message.author, message.channel, 600, "Спам")  # 10 минут

    async def check_prohibited_content(self, message):
        content = message.content.lower()

        # Проверка на религиозные темы
        religious_found = any(keyword in content for keyword in self.religious_keywords)

        # Проверка на политические темы
        political_found = any(keyword in content for keyword in self.political_keywords)

        if religious_found or political_found:
            try:
                # Удаляем сообщение
                await message.delete()

                # Создаем ephemeral сообщение для нарушителя
                embed = disnake.Embed(
                    title="⚠️ Нарушение правил",
                    description="Обсуждение религиозных и политических тем запрещено!",
                    color=disnake.Color.red(),
                    timestamp=datetime.now()
                )

                if religious_found:
                    embed.add_field(name="Причина", value="Упоминание религиозных тем", inline=True)
                if political_found:
                    embed.add_field(name="Причина", value="Упоминание политических тем", inline=True)

                embed.set_footer(text="Сообщение было удалено")

                # Отправляем ephemeral предупреждение нарушителю
                try:
                    await message.author.send(embed=embed)
                except disnake.Forbidden:
                    # Если ЛС закрыты, отправляем ephemeral в канал (но это сработает только для slash команд)
                    pass

            except disnake.Forbidden:
                print(f"Недостаточно прав для удаления сообщения в канале {message.channel.name}")
            except Exception as e:
                print(f"Ошибка при обработке запрещенного контента: {e}")

    async def mute_user(self, user, channel, duration_seconds, reason):
        """Мут пользователя на указанное время"""
        if user.id in self.muted_users:
            return

        self.muted_users.add(user.id)

        try:
            # Создаем ephemeral сообщение для нарушителя
            embed = disnake.Embed(
                title="🔇 Вы получили мут",
                description=f"Вы получили мут на {duration_seconds // 60} минут",
                color=disnake.Color.orange(),
                timestamp=datetime.now()
            )
            embed.add_field(name="Причина", value=reason, inline=True)
            embed.add_field(name="Длительность", value=f"{duration_seconds // 60} минут", inline=True)
            embed.set_footer(text="Подумайте о своем поведении")

            # Пытаемся отправить в ЛС
            try:
                await user.send(embed=embed)
            except disnake.Forbidden:
                # Если ЛС закрыты, логируем это
                print(f"Не удалось отправить сообщение о муте пользователю {user.name}")

            # Ждем указанное время
            await asyncio.sleep(duration_seconds)

            # Размучиваем пользователя
            self.muted_users.remove(user.id)

            # Ephemeral уведомление о размуте
            embed = disnake.Embed(
                title="🔊 Мут снят",
                description="Вы снова можете писать в чат",
                color=disnake.Color.green(),
                timestamp=datetime.now()
            )

            # Пытаемся отправить в ЛС
            try:
                await user.send(embed=embed)
            except disnake.Forbidden:
                print(f"Не удалось отправить сообщение о размуте пользователю {user.name}")

        except Exception as e:
            print(f"Ошибка при муте пользователя {user.name}: {e}")

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        """Проверяем отредактированные сообщения на запрещенный контент"""
        if after.author.bot:
            return

        # Если содержание изменилось, проверяем на запрещенные темы
        if before.content != after.content:
            await self.check_prohibited_content(after)

    # Slash команды с ephemeral ответами

    @commands.has_permissions(manage_messages=True)
    async def warn_user(
            self,
            inter: disnake.ApplicationCommandInteraction,
            user: disnake.Member,
            reason: str = "Не указана"
    ):
        """Выдает предупреждение пользователю"""
        embed = disnake.Embed(
            title="⚠️ Предупреждение",
            description=f"Пользователь {user.mention} получил предупреждение",
            color=disnake.Color.yellow(),
            timestamp=datetime.now()
        )
        embed.add_field(name="Причина", value=reason, inline=True)
        embed.add_field(name="Модератор", value=inter.author.mention, inline=True)

        await inter.response.send_message(embed=embed, ephemeral=True)

        # Также отправляем предупреждение пользователю в ЛС
        try:
            user_embed = disnake.Embed(
                title="⚠️ Вы получили предупреждение",
                description=f"На сервере {inter.guild.name}",
                color=disnake.Color.yellow()
            )
            user_embed.add_field(name="Причина", value=reason, inline=True)
            user_embed.add_field(name="Модератор", value=inter.author.display_name, inline=True)
            await user.send(embed=user_embed)
        except disnake.Forbidden:
            pass

    @commands.has_permissions(manage_messages=True)
    async def clear_messages(
            self,
            inter: disnake.ApplicationCommandInteraction,
            amount: int = commands.Param(description="Количество сообщений для удаления", ge=1, le=100)
    ):
        """Очищает указанное количество сообщений"""
        await inter.response.defer(ephemeral=True)

        deleted = await inter.channel.purge(limit=amount)

        embed = disnake.Embed(
            title="🗑️ Очистка сообщений",
            description=f"Удалено {len(deleted)} сообщений",
            color=disnake.Color.green(),
            timestamp=datetime.now()
        )
        embed.add_field(name="Канал", value=inter.channel.mention, inline=True)
        embed.add_field(name="Модератор", value=inter.author.mention, inline=True)

        await inter.edit_original_response(embed=embed)


    async def mute_info(self, inter: disnake.ApplicationCommandInteraction):
        """Показывает информацию о муте пользователя"""
        if inter.author.id in self.muted_users:
            embed = disnake.Embed(
                title="🔇 Вы находитесь в муте",
                description="Вы не можете писать в чат до снятия ограничения",
                color=disnake.Color.red(),
                timestamp=datetime.now()
            )
        else:
            embed = disnake.Embed(
                title="🔊 Вы не в муте",
                description="Вы можете свободно писать в чат",
                color=disnake.Color.green(),
                timestamp=datetime.now()
            )

        await inter.response.send_message(embed=embed, ephemeral=True)


    def cog_unload(self):
        """Очистка при выгрузке кога"""
        self.user_message_count.clear()
        self.muted_users.clear()


def setup(bot):
    bot.add_cog(ModerationCog(bot))