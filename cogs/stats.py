import disnake
from disnake.ext import commands, tasks
import asyncio
import datetime
import json
import os


class Stats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.server_stats = {}
        self.last_update = {}
        self.data_file = "stats_data.json"
        self.update_queue = asyncio.Queue()
        self.is_processing = False

        self.load_stats_data()
        self.auto_update.start()
        # Запускаем обработчик очереди обновлений
        self.bot.loop.create_task(self.process_update_queue())

    def cog_unload(self):
        self.auto_update.cancel()
        self.save_stats_data()

    # === ФУНКЦИОНАЛ СОХРАНЕНИЯ ДАННЫХ ===
    def load_stats_data(self):
        """Загружает сохраненные данные о каналах статистики"""
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.server_stats = {int(guild_id): channel_id for guild_id, channel_id in
                                         data.get('server_stats', {}).items()}
            else:
                self.server_stats = {}
        except Exception as e:
            print(f"❌ Ошибка при загрузке данных: {e}")
            self.server_stats = {}

    def save_stats_data(self):
        """Сохраняет данные о каналах статистики"""
        try:
            data = {
                'server_stats': self.server_stats,
                'last_save': datetime.datetime.now().isoformat()
            }
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"❌ Ошибка при сохранении данных: {e}")

    # === СИСТЕМА ОЧЕРЕДИ ОБНОВЛЕНИЙ ===
    async def process_update_queue(self):
        """Обрабатывает очередь обновлений для избежания спама API"""
        while True:
            try:
                guild_id = await self.update_queue.get()
                guild = self.bot.get_guild(guild_id)

                if guild and await self.is_stats_channel_exists(guild):
                    # Небольшая задержка для группировки быстрых обновлений
                    await asyncio.sleep(2)

                    # Пропускаем дублирующиеся обновления
                    while not self.update_queue.empty():
                        try:
                            next_guild_id = self.update_queue.get_nowait()
                            if next_guild_id == guild_id:
                                self.update_queue.task_done()
                            else:
                                # Возвращаем обратно если другой сервер
                                await self.update_queue.put(next_guild_id)
                                break
                        except asyncio.QueueEmpty:
                            break

                    await self.update_member_count(guild)
                    print(f"🔄 Обновлена статистика для {guild.name}")

                self.update_queue.task_done()

            except Exception as e:
                print(f"❌ Ошибка в обработчике очереди: {e}")

    async def schedule_update(self, guild):
        """Добавляет обновление в очередь"""
        try:
            await self.update_queue.put(guild.id)
        except Exception as e:
            print(f"❌ Ошибка при добавлении в очередь: {e}")

    # === АВТООБНОВЛЕНИЕ ===
    @tasks.loop(hours=1)
    async def auto_update(self):
        """Автообновление статистики раз в час"""
        print(f"\033[1m[🕐]\033[0m \033[1mСледующее обновление:\033[0m {datetime.datetime.now().strftime('%H:%M:%S')} по \033[1mМСК\033[0m")

        for guild in self.bot.guilds:
            try:
                if await self.is_stats_channel_exists(guild):
                    await self.update_member_count(guild)
                self.last_update[guild.id] = datetime.datetime.now()
            except Exception as e:
                print(f"❌ Ошибка при автообновлении на сервере {guild.name}: {e}")

        self.save_stats_data()

    @auto_update.before_loop
    async def before_auto_update(self):
        await self.bot.wait_until_ready()

    # === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
    async def has_existing_stats_channel(self, guild):
        """Проверяет, есть ли уже канал статистики на сервере"""
        category = disnake.utils.get(guild.categories, name="https://discord.moonrein.net")
        if not category:
            return False

        for channel in category.channels:
            if channel.name.startswith("👥 Всего участников:"):
                return True
        return False

    async def is_stats_channel_exists(self, guild):
        """Проверяет, существует ли уже канал статистики"""
        if guild.id in self.server_stats:
            channel = guild.get_channel(self.server_stats[guild.id])
            if channel and channel.name.startswith("👥 Всего участников:"):
                return True

        if await self.has_existing_stats_channel(guild):
            await self.restore_stats_channel(guild)
            return True

        return False

    async def check_bot_permissions(self, guild):
        """Проверяет, есть ли у бота необходимые права"""
        required_permissions = disnake.Permissions(
            manage_channels=True,
            view_channel=True,
            connect=True,
            manage_roles=True
        )

        bot_member = guild.get_member(self.bot.user.id)
        if not bot_member:
            return False

        missing_permissions = []
        for perm, value in required_permissions:
            if value and not getattr(bot_member.guild_permissions, perm):
                missing_permissions.append(perm)

        if missing_permissions:
            print(f"❌ На сервере {guild.name} у бота отсутствуют права: {', '.join(missing_permissions)}")
            return False

        return True

    # === ВОССТАНОВЛЕНИЕ КАНАЛОВ ===
    async def restore_stats_channel(self, guild):
        """Восстанавливает ссылку на существующий канал статистики"""
        try:
            category = disnake.utils.get(guild.categories, name="https://discord.moonrein.net")
            if not category:
                return False

            for channel in category.channels:
                if channel.name.startswith("👥 Всего участников:"):
                    self.server_stats[guild.id] = channel.id
                    self.save_stats_data()
                    return True
            return False
        except Exception as e:
            print(f"❌ Ошибка при восстановлении канала статистики на сервере {guild.name}: {e}")
            return False

    # === СОЗДАНИЕ КАНАЛА ===
    async def setup_stats_channel(self, guild):
        """Создает категорию и голосовой канал для статистики"""
        try:
            if await self.is_stats_channel_exists(guild):
                await self.restore_stats_channel(guild)
                return True

            if not await self.check_bot_permissions(guild):
                return False

            category = disnake.utils.get(guild.categories, name="https://discord.moonrein.net")

            if not category:
                category = await guild.create_category_channel(
                    "https://discord.moonrein.net",
                    reason="Создание категории для статистики сервера"
                )

            real_members = sum(1 for member in guild.members if not member.bot)
            total_members = guild.member_count

            voice_channel = await category.create_voice_channel(
                f"👥 Всего участников: {real_members}",
                reason="Создание канала статистики"
            )

            self.server_stats[guild.id] = voice_channel.id
            self.save_stats_data()

            await voice_channel.set_permissions(guild.default_role, connect=False, view_channel=True)

            admin_role = disnake.utils.get(guild.roles, permissions=disnake.Permissions(administrator=True))
            if admin_role:
                await voice_channel.set_permissions(admin_role, connect=True, view_channel=True)

            await voice_channel.set_permissions(guild.me, connect=True, view_channel=True, manage_channels=True)

            return True

        except Exception as e:
            print(f"❌ Ошибка при создании канала статистики на сервере {guild.name}: {e}")
            return False

    # === УДАЛЕНИЕ КАНАЛА ===
    async def delete_stats_channel(self, guild):
        """Удаляет канал и категорию статистики"""
        try:
            if not await self.check_bot_permissions(guild):
                return False

            category = disnake.utils.get(guild.categories, name="https://discord.moonrein.net")
            if not category:
                if guild.id in self.server_stats:
                    del self.server_stats[guild.id]
                    self.save_stats_data()
                return True

            for channel in category.channels:
                await channel.delete(reason="Удаление статистики сервера")

            await category.delete(reason="Удаление статистики сервера")

            if guild.id in self.server_stats:
                del self.server_stats[guild.id]
                self.save_stats_data()

            return True

        except Exception as e:
            print(f"❌ Ошибка при удалении статистики на сервере {guild.name}: {e}")
            return False

    # === ОБНОВЛЕНИЕ СТАТИСТИКИ ===
    async def update_member_count(self, guild):
        """Обновляет счетчик участников в голосовом канале"""
        try:
            if guild.id not in self.server_stats:
                if not await self.restore_stats_channel(guild):
                    return

            channel_id = self.server_stats[guild.id]
            voice_channel = guild.get_channel(channel_id)

            if not voice_channel:
                if not await self.restore_stats_channel(guild):
                    return
                voice_channel = guild.get_channel(self.server_stats[guild.id])
                if not voice_channel:
                    return

            real_members = sum(1 for member in guild.members if not member.bot)
            total_members = guild.member_count

            new_name = f"👥 Всего участников: {real_members}"

            if voice_channel.name != new_name:
                await voice_channel.edit(name=new_name)
                print(f"📊 Обновлена статистика {guild.name}: {real_members} участников")

        except Exception as e:
            print(f"❌ Ошибка при обновлении статистики на сервере {guild.name}: {e}")

    # === АВТОМАТИЧЕСКОЕ СОЗДАНИЕ ПРИ ЗАПУСКЕ ===
    async def auto_setup_on_startup(self):
        """Автоматически создает каналы статистики при первом запуске на всех серверах"""
        print("🚀 Запуск автоматического создания каналов статистики...")

        created_count = 0
        existing_count = 0
        error_count = 0

        for guild in self.bot.guilds:
            try:
                print(f"🔧 Обработка сервера: {guild.name}")

                # Проверяем, есть ли уже канал статистики
                if await self.is_stats_channel_exists(guild):
                    print(f"✅ Канал статистики уже существует на {guild.name}")
                    existing_count += 1
                    await self.update_member_count(guild)
                else:
                    # Если нет - создаем автоматически
                    print(f"📝 Создаем канал статистики на {guild.name}")
                    success = await self.setup_stats_channel(guild)
                    if success:
                        print(f"✅ Канал статистики создан на {guild.name}")
                        created_count += 1
                        await self.update_member_count(guild)
                    else:
                        print(f"❌ Не удалось создать канал на {guild.name} - проверьте права бота")
                        error_count += 1

            except Exception as e:
                print(f"❌ Ошибка при обработке сервера {guild.name}: {e}")
                error_count += 1

        print(
            f"🎯 Автоматическое создание завершено: ✅ {created_count} создано, 🔄 {existing_count} уже существовало, ❌ {error_count} ошибок")

    # === EVENT HANDLERS ===
    @commands.Cog.listener()
    async def on_ready(self):
        """Автоматически создает и восстанавливает каналы статистики при запуске бота"""
        print("🔍 Инициализация каналов статистики...")

        # Ждем полной готовности бота
        await asyncio.sleep(2)

        # Запускаем автоматическое создание
        await self.auto_setup_on_startup()

        print("🎯 Инициализация статистики завершена")

    @commands.Cog.listener()
    async def on_member_join(self, member):
        """Обновляет статистику когда участник заходит на сервер"""
        if await self.is_stats_channel_exists(member.guild):
            print(f"👤 {member.name} присоединился к {member.guild.name}")
            await self.schedule_update(member.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        """Обновляет статистику когда участник выходит с сервера"""
        if await self.is_stats_channel_exists(member.guild):
            print(f"👤 {member.name} покинул {member.guild.name}")
            await self.schedule_update(member.guild)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        """Обновляет статистику когда участник меняет статус бота"""
        if before.bot != after.bot and await self.is_stats_channel_exists(after.guild):
            print(f"🤖 Изменен статус бота для {after.name} на {after.guild.name}")
            await self.schedule_update(after.guild)

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        """Обновляет статистику когда участник забанен"""
        if await self.is_stats_channel_exists(guild):
            print(f"🚫 {user.name} забанен на {guild.name}")
            await self.schedule_update(guild)

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        """Обновляет статистику когда участник разбанен"""
        if await self.is_stats_channel_exists(guild):
            print(f"✅ {user.name} разбанен на {guild.name}")
            await self.schedule_update(guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        """Удаляет данные когда бота удаляют с сервера"""
        if guild.id in self.server_stats:
            del self.server_stats[guild.id]
            self.save_stats_data()
            print(f"🗑️ Удалены данные статистики для {guild.name}")

def setup(bot):
    bot.add_cog(Stats(bot))