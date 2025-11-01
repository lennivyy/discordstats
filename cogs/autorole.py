import disnake
import json
import os
from disnake.ext import commands


class ReactionRoleCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config_file = "reactionrole_config.json"
        self.load_config()

    def load_config(self):
        """Загружает конфигурацию из JSON файла"""
        if os.path.exists(self.config_file):
            with open(self.config_file, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
        else:
            self.config = {}
            self.save_config()

    def save_config(self):
        """Сохраняет конфигурацию в JSON файл"""
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=4, ensure_ascii=False)

    def get_guild_config(self, guild_id):
        """Получает конфигурацию для сервера"""
        return self.config.get(str(guild_id), {})

    def set_guild_config(self, guild_id, channel_id, role_id, message_id=None):
        """Устанавливает конфигурацию для сервера"""
        guild_id_str = str(guild_id)
        self.config[guild_id_str] = {
            "channel_id": channel_id,
            "role_id": role_id,
            "message_id": message_id
        }
        self.save_config()

    async def setup_reaction_role(self, inter: disnake.ApplicationCommandInteraction,
                                  channel: disnake.TextChannel,
                                  role: disnake.Role):
        """Команда для настройки системы ролей через реакции"""

        # Создаем сообщение с кнопкой
        embed = disnake.Embed(
            title="Получить доступ",
            description="Нажмите на реакцию ниже чтобы получить доступ к контенту",
            color=0x00ff00
        )

        # Создаем кнопку
        components = [
            disnake.ui.Button(
                style=disnake.ButtonStyle.primary,
                label="Получить доступ",
                custom_id="get_role_button"
            )
        ]

        message = await channel.send(embed=embed, components=components)

        # Сохраняем конфигурацию
        self.set_guild_config(inter.guild_id, channel.id, role.id, message.id)

        await inter.response.send_message(
            f"Система ролей настроена! Сообщение отправлено в канал {channel.mention}",
            ephemeral=True
        )

    @commands.Cog.listener()
    async def on_button_click(self, inter: disnake.MessageInteraction):
        """Обработка нажатия на кнопку"""

        if inter.component.custom_id != "get_role_button":
            return

        # Получаем конфигурацию сервера
        guild_config = self.get_guild_config(inter.guild.id)

        if not guild_config:
            await inter.response.send_message("Система ролей не настроена на этом сервере.", ephemeral=True)
            return

        role_id = guild_config.get("role_id")
        if not role_id:
            await inter.response.send_message("Роль не настроена.", ephemeral=True)
            return

        # Получаем роль
        role = inter.guild.get_role(role_id)
        if not role:
            await inter.response.send_message("Роль не найдена.", ephemeral=True)
            return

        # Проверяем есть ли уже роль у пользователя
        if role in inter.author.roles:
            # Убираем роль если она уже есть
            await inter.author.remove_roles(role)
            await inter.response.send_message("Доступ убран!", ephemeral=True)
        else:
            # Выдаем роль
            await inter.author.add_roles(role)
            await inter.response.send_message("Доступ получен!", ephemeral=True)

    # Альтернативная версия с реакциями (если предпочитаете эмодзи)
    async def setup_reaction_emoji(self, inter: disnake.ApplicationCommandInteraction,
                                   channel: disnake.TextChannel,
                                   role: disnake.Role):
        """Команда для настройки системы ролей через эмодзи"""

        embed = disnake.Embed(
            title="Нажмите на реакцию чтобы увидеть фурри порно",
            description="Нажмите на реакцию 🔞 ниже чтобы получить доступ",
            color=0xff0000
        )

        message = await channel.send(embed=embed)
        await message.add_reaction("🔞")

        # Сохраняем конфигурацию
        self.set_guild_config(inter.guild_id, channel.id, role.id, message.id)

        await inter.response.send_message(
            f"Система ролей с реакциями настроена! Сообщение отправлено в канал {channel.mention}",
            ephemeral=True
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: disnake.RawReactionActionEvent):
        """Обработка добавления реакции"""

        # Игнорируем реакции бота
        if payload.member and payload.member.bot:
            return

        # Получаем конфигурацию сервера
        guild_config = self.get_guild_config(payload.guild_id)

        if not guild_config:
            return

        # Проверяем что реакция добавлена к правильному сообщению
        if payload.message_id != guild_config.get("message_id"):
            return

        # Проверяем что это нужная реакция
        if str(payload.emoji) != "🔞":
            return

        role_id = guild_config.get("role_id")
        if not role_id:
            return

        # Получаем гильдию и роль
        guild = self.bot.get_guild(payload.guild_id)
        role = guild.get_role(role_id)

        if not role:
            return

        # Получаем участника
        member = guild.get_member(payload.user_id)
        if not member:
            return

        # Выдаем роль
        try:
            await member.add_roles(role)
            print(f"Роль {role.name} выдана пользователю {member.display_name}")
        except disnake.Forbidden:
            print(f"Недостаточно прав для выдачи роли {role.name}")
        except disnake.HTTPException as e:
            print(f"Ошибка при выдаче роли: {e}")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: disnake.RawReactionActionEvent):
        """Обработка удаления реакции (опционально - убираем роль)"""

        # Получаем конфигурацию сервера
        guild_config = self.get_guild_config(payload.guild_id)

        if not guild_config:
            return

        # Проверяем что реакция удалена с правильного сообщения
        if payload.message_id != guild_config.get("message_id"):
            return

        # Проверяем что это нужная реакция
        if str(payload.emoji) != "🔞":
            return

        role_id = guild_config.get("role_id")
        if not role_id:
            return

        # Получаем гильдию и роль
        guild = self.bot.get_guild(payload.guild_id)
        role = guild.get_role(role_id)

        if not role:
            return

        # Получаем участника
        member = guild.get_member(payload.user_id)
        if not member:
            return

        # Убираем роль
        try:
            await member.remove_roles(role)
            print(f"Роль {role.name} убрана у пользователя {member.display_name}")
        except disnake.Forbidden:
            print(f"Недостаточно прав для удаления роли {role.name}")
        except disnake.HTTPException as e:
            print(f"Ошибка при удалении роли: {e}")


def setup(bot):
    bot.add_cog(ReactionRoleCog(bot))