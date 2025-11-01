import asyncio
import json
import os
import time
from typing import Optional, Dict, List

import aiohttp
from aiohttp import WSMsgType, ClientWebSocketResponse, ClientTimeout
import disnake
from disnake.ext import commands, tasks


# -------------------- простые утилиты --------------------

def _to_int(*vals: object) -> int:
    for v in vals:
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            try:
                return int(v)
            except Exception:
                pass
        if isinstance(v, str):
            try:
                return int(float(v))
            except Exception:
                pass
    return 0


def _to_float(*vals: object) -> Optional[float]:
    for v in vals:
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except Exception:
                pass
    return None


def _to_id(env_val: Optional[str]) -> Optional[int]:
    if not env_val:
        return None
    try:
        return int(str(env_val).strip())
    except Exception:
        return None


# ========================== COG ===========================

class MinecraftCog(commands.Cog):
    """
    WebSocket-клиент к bridge: только ONLINE / TPS / MSPT для выбранного REALM.
    Устойчивое соединение: свой ping/pong, без лишнего парсинга и без ложных реконнектов.
    """

    # ---------------- init / config ----------------

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # WS config
        self.WS_URL: str = (os.getenv("MC_WS_URL") or "ws://bridge:8765/ws").strip().rstrip("/")
        self.WS_TOKEN: str = (os.getenv("MC_WS_TOKEN") or "").replace("\r", "").replace("\n", "").strip()
        self.REALM: str = (os.getenv("MC_REALM") or "anarchy").strip()

        # Heartbeat (наши собственные ping/pong)
        try:
            self.WS_HEARTBEAT_SEC: int = max(10, int(os.getenv("MC_WS_HEARTBEAT_SEC") or "30"))
        except Exception:
            self.WS_HEARTBEAT_SEC = 30
        try:
            self.WS_PING_TIMEOUT_SEC: int = max(5, int(os.getenv("MC_WS_PING_TIMEOUT_SEC") or "10"))
        except Exception:
            self.WS_PING_TIMEOUT_SEC = 10

        # Logs / дебаг
        self.DEBUG: bool = (os.getenv("MC_WS_DEBUG") or "0").strip().lower() in {"1", "true", "yes"}
        try:
            self.TRUNC: int = max(80, int(os.getenv("MC_WS_TRUNC") or "400"))
        except Exception:
            self.TRUNC = 400

        # Channel rename debounce + show mspt
        try:
            self.CHANNEL_UPDATE_MIN_SEC: int = max(3, int(os.getenv("MC_CHANNEL_UPDATE_MIN_SEC") or "10"))
        except Exception:
            self.CHANNEL_UPDATE_MIN_SEC = 10
        self.CHANNEL_SHOW_MSPT: bool = True  # нужно по ТЗ

        # Триггеры на изменение
        try:
            self.TPS_CHANGE_EPS: float = float(os.getenv("MC_TPS_CHANGE_EPS") or "0.05")
        except Exception:
            self.TPS_CHANGE_EPS = 0.05
        try:
            self.MSPT_CHANGE_EPS: float = float(os.getenv("MC_MSPT_CHANGE_EPS") or "0.05")
        except Exception:
            self.MSPT_CHANGE_EPS = 0.05

        # Optional IDs / category
        self.ENV_ONLINE_ID: Optional[int] = _to_id(os.getenv("MC_ONLINE_CHANNEL_ID"))
        self.ENV_TPS_ID: Optional[int] = _to_id(os.getenv("MC_TPS_CHANNEL_ID"))
        self.ENV_CATEGORY_ID: Optional[int] = _to_id(os.getenv("MC_CATEGORY_ID"))
        self.ENV_CATEGORY_NAME: Optional[str] = (os.getenv("MC_CATEGORY_NAME") or "").strip() or None

        # WS state
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[ClientWebSocketResponse] = None
        self._conn_id: int = 0  # поколение соединения
        self._connecting: bool = False

        # Активность
        self._last_activity_ts: float = 0.0   # любое сообщение или успешный pong
        self._last_stats_ts: float = 0.0
        self._last_ping_sent: float = 0.0

        # Discord entities
        self.category_id: Optional[int] = self.ENV_CATEGORY_ID
        self.voice_channel_id_online: Optional[int] = self.ENV_ONLINE_ID
        self.voice_channel_id_tps: Optional[int] = self.ENV_TPS_ID

        # Debounce memory
        self._last_online_name: Optional[str] = None
        self._last_tps_name: Optional[str] = None
        self._last_online_rename_ts: float = 0.0
        self._last_tps_rename_ts: float = 0.0

        # Текущий статус (минимум полей)
        self.server_status: Dict[str, object] = {
            "realm": self.REALM,
            "online": False,
            "players": 0,
            "max_players": 0,
            "tps_1m": None,
            "mspt": None,
        }
        # Предыдущие значения (для триггеров)
        self._prev_players: Optional[int] = None
        self._prev_tps: Optional[float] = None
        self._prev_mspt: Optional[float] = None

        # Loops
        self.ensure_channels_once.start()
        self.connect_manager.start()
        self.ping_loop.start()
        self.periodic_update.start()

        if self.DEBUG:
            print(
                f"[MinecraftCog] init: realm={self.REALM}, hb={self.WS_HEARTBEAT_SEC}s, "
                f"ping_timeout={self.WS_PING_TIMEOUT_SEC}s, debounce={self.CHANNEL_UPDATE_MIN_SEC}s"
            )

    # ---------------- lifecycle helpers ----------------

    def cog_unload(self):
        for loop in (self.ensure_channels_once, self.connect_manager, self.ping_loop, self.periodic_update):
            try:
                loop.cancel()
            except Exception:
                pass
        asyncio.create_task(self._close_ws())  # мягко закроет текущий сокет
        if self._session and not self._session.closed:
            asyncio.create_task(self._session.close())

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            timeout = ClientTimeout(total=None)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def _close_ws(self, ws: Optional[ClientWebSocketResponse] = None):
        target = ws or self._ws
        try:
            if target is not None and not target.closed:
                await target.close()
        except Exception:
            pass
        finally:
            if target is self._ws:
                self._ws = None

    # ---------------- channels ----------------

    @tasks.loop(count=1)
    async def ensure_channels_once(self):
        await self._ensure_channels_ready("online", create=True)
        await self._ensure_channels_ready("tps", create=True)
        await self._update_channel_name_now("online", force=True)
        await self._update_channel_name_now("tps", force=True)

    @ensure_channels_once.before_loop
    async def _before_ensure(self):
        await self.bot.wait_until_ready()

    async def _ensure_channels_ready(self, kind: str, create: bool = False) -> bool:
        ch_id = self.voice_channel_id_online if kind == "online" else self.voice_channel_id_tps
        if ch_id:
            ch = self.bot.get_channel(ch_id)
            if isinstance(ch, disnake.VoiceChannel):
                return True

        # by category id
        if create and self.category_id:
            cat: Optional[disnake.CategoryChannel] = None
            for g in self.bot.guilds:
                c = g.get_channel(self.category_id)
                if isinstance(c, disnake.CategoryChannel):
                    cat = c
                    break
            if cat:
                try:
                    name = (self._build_online_name() if kind == "online" else self._build_tps_name()) or \
                           (f"🟡 MC {self.REALM}: загрузка..." if kind == "online" else f"⚙️ TPS {self.REALM}: загрузка...")
                    vc = await cat.create_voice_channel(name=name, reason=f"Автосоздание канала ({kind})")
                    if kind == "online":
                        self.voice_channel_id_online = vc.id
                    else:
                        self.voice_channel_id_tps = vc.id
                    if self.DEBUG:
                        print(f"[MinecraftCog] ensure[{kind}]: создан id={vc.id}")
                    return True
                except Exception as e:
                    print(f"[MinecraftCog] ensure[{kind}] create ERROR: {e!r}")

        # by category name (fallback)
        if create and not self.category_id and self.ENV_CATEGORY_NAME:
            for g in self.bot.guilds:
                for c in g.channels:
                    if isinstance(c, disnake.CategoryChannel) and c.name == self.ENV_CATEGORY_NAME:
                        self.category_id = c.id
                        return await self._ensure_channels_ready(kind, create=True)

        # find by name prefix (if IDs lost)
        wanted_prefix = "🟢 MC " if kind == "online" else "⚙️ TPS "
        for g in self.bot.guilds:
            for c in g.channels:
                if isinstance(c, disnake.VoiceChannel) and c.name.startswith(wanted_prefix + self.REALM):
                    if kind == "online":
                        self.voice_channel_id_online = c.id
                    else:
                        self.voice_channel_id_tps = c.id
                    return True

        return False

    # ---------------- connect/manage ----------------

    def _url_candidates(self) -> List[str]:
        base = self.WS_URL.rstrip("/")
        return [base, base[:-3]] if base.endswith("/ws") else [f"{base}/ws", base]

    @tasks.loop(seconds=5)
    async def connect_manager(self):
        """
        Подключаемся только если сокета нет или он закрыт.
        Не трогаем активное соединение.
        """
        if self._ws is not None and not self._ws.closed:
            return

        if self._connecting:
            return

        await self._ensure_session()
        assert self._session is not None
        self._connecting = True

        headers = {"Authorization": f"Bearer {self.WS_TOKEN}"} if self.WS_TOKEN else None

        for url in self._url_candidates():
            try:
                ws = await self._session.ws_connect(
                    url,
                    headers=headers,
                    autoping=False,          # свой ping-loop
                    heartbeat=None,
                    timeout=15.0,
                    receive_timeout=None,    # сами следим пингом
                    max_msg_size=4 * 1024 * 1024,
                )
                # успех — фиксируем поколение и сокет
                self._conn_id += 1
                conn_id = self._conn_id
                self._ws = ws
                now = time.time()
                self._last_activity_ts = now
                self._last_ping_sent = 0.0
                self._last_stats_ts = 0.0

                status = getattr(ws, "response", None).status if getattr(ws, "response", None) else "?"
                if self.DEBUG:
                    print(f"[MinecraftCog] WS connected #{conn_id}: {url} status={status} (autoping=OFF, our ping every {self.WS_HEARTBEAT_SEC}s)")

                # слушатель строго привязан к этому сокету/поколению
                asyncio.create_task(self._listen_loop(ws, conn_id))
                break

            except Exception as e:
                if self.DEBUG:
                    print(f"[MinecraftCog] connect fail: {url}: {e!r}")
                # пробуем следующий
                continue

        self._connecting = False

    async def _listen_loop(self, ws: ClientWebSocketResponse, conn_id: int):
        try:
            async for msg in ws:
                # Любой кадр = активность
                self._last_activity_ts = time.time()

                if msg.type == WSMsgType.TEXT:
                    raw = msg.data
                    if self.DEBUG:
                        preview = raw if len(raw) <= self.TRUNC else raw[: self.TRUNC] + "…"
                        print(f"[MinecraftCog] WS TEXT#{conn_id} ({len(raw)}b): {preview}")
                    try:
                        payload = json.loads(raw)
                    except Exception as e:
                        if self.DEBUG:
                            print(f"[MinecraftCog] json error: {e!r}")
                        continue
                    await self._handle_message(payload)

                elif msg.type == WSMsgType.ERROR:
                    if self.DEBUG:
                        print(f"[MinecraftCog] WS ERROR#{conn_id}: {ws.exception()}")
                    break

                elif msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSING):
                    if self.DEBUG:
                        print(f"[MinecraftCog] WS CLOSED by remote #{conn_id}")
                    break

                else:
                    # BINARY/PING/PONG не обрабатываем отдельно (PING/PONG в autoping=False нам не приходят)
                    pass

        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self.DEBUG:
                print(f"[MinecraftCog] listen exception#{conn_id}: {e!r}")
        finally:
            code = getattr(ws, "close_code", None)
            if self.DEBUG:
                print(f"[MinecraftCog] WS finished#{conn_id} code={code}")

            # Закрываем ИМЕННО этот сокет (не трогаем новый)
            await self._close_ws(ws)

            # Помечаем оффлайн и обновляем каналы (мягко)
            await self._go_offline()

    # ---------------- ping loop ----------------

    @tasks.loop(seconds=1)
    async def ping_loop(self):
        """
        Наш keepalive: раз в HEARTBEAT_SEC шлём ws.ping() и ждём PONG.
        Успех -> обновляем _last_activity_ts. Таймаут -> закрываем сокет (даём шанc connect_manager).
        """
        ws = self._ws
        if ws is None or ws.closed:
            return

        now = time.time()
        if now - self._last_ping_sent < self.WS_HEARTBEAT_SEC:
            return

        self._last_ping_sent = now
        try:
            waiter = ws.ping()
        except Exception as e:
            if self.DEBUG:
                print(f"[MinecraftCog] ping send failed: {e!r}")
            await self._close_ws(ws)
            return

        try:
            await asyncio.wait_for(waiter, timeout=self.WS_PING_TIMEOUT_SEC)
            # получили PONG -> считаем активностью
            self._last_activity_ts = time.time()
            if self.DEBUG:
                print(f"[MinecraftCog] pong ok")
        except asyncio.TimeoutError:
            if self.DEBUG:
                print(f"[MinecraftCog] pong TIMEOUT (> {self.WS_PING_TIMEOUT_SEC}s) — close")
            await self._close_ws(ws)
        except Exception as e:
            if self.DEBUG:
                print(f"[MinecraftCog] pong error: {e!r}")
            await self._close_ws(ws)

    @ping_loop.before_loop
    async def _before_ping(self):
        await self.bot.wait_until_ready()

    # ---------------- names ----------------

    def _build_online_name(self) -> str:
        s = self.server_status
        realm = s.get("realm", self.REALM)
        if s.get("online"):
            p = _to_int(s.get("players", 0))
            m = _to_int(s.get("max_players", 0))
            base = f"🟢 MC {realm}: {p}/{m}" if m else f"🟢 MC {realm}: {p}"
        else:
            base = f"🔴 MC {realm}: выключен"
        return base[:95]

    def _build_tps_name(self) -> str:
        s = self.server_status
        realm = s.get("realm", self.REALM)
        tps_1m = _to_float(s.get("tps_1m"))
        mspt = _to_float(s.get("mspt"))
        if not s.get("online"):
            name = f"⚙️ TPS {realm}: отсутствует"
        else:
            tps_part = f"{tps_1m:.1f}" if tps_1m is not None else "—"
            mspt_part = f" | {mspt:.2f} mspt" if (self.CHANNEL_SHOW_MSPT and mspt is not None) else ""
            name = f"⚙️ TPS {realm}: {tps_part}{mspt_part}"
        return name[:95]

    async def _update_channel_name_now(self, kind: str, force: bool = False):
        ready = await self._ensure_channels_ready(kind, create=False) or await self._ensure_channels_ready(kind, create=True)
        if not ready:
            return

        ch_id = self.voice_channel_id_online if kind == "online" else self.voice_channel_id_tps
        ch = self.bot.get_channel(ch_id) if ch_id else None
        if ch is None:
            return

        new_name = self._build_online_name() if kind == "online" else self._build_tps_name()
        now = time.time()

        if not force:
            last_name = self._last_online_name if kind == "online" else self._last_tps_name
            last_ts = self._last_online_rename_ts if kind == "online" else self._last_tps_rename_ts

            if last_name == new_name:
                if self.DEBUG:
                    print(f"[MinecraftCog] rename[{kind}] skip: same name")
                return
            if now - last_ts < self.CHANNEL_UPDATE_MIN_SEC:
                if self.DEBUG:
                    print(f"[MinecraftCog] rename[{kind}] skip: debounced ({now - last_ts:.1f}s < {self.CHANNEL_UPDATE_MIN_SEC}s)")
                return

        try:
            await ch.edit(name=new_name, reason=f"Обновление статуса Minecraft ({kind})")
            if kind == "online":
                self._last_online_name = new_name
                self._last_online_rename_ts = now
            else:
                self._last_tps_name = new_name
                self._last_tps_rename_ts = now
            if self.DEBUG:
                print(f"[MinecraftCog] channel rename [{kind}] -> {new_name}")
        except Exception as e:
            print(f"[MinecraftCog] Ошибка переименования канала [{kind}]: {e!r}")

    # ---------------- offline/online helpers ----------------

    async def _go_offline(self):
        # Минимальный оффлайн
        self.server_status.update({
            "online": False,
            "players": 0,
            "tps_1m": None,
            "mspt": None,
        })
        # Сбросить прошлые значения
        self._prev_players = None
        self._prev_tps = None
        self._prev_mspt = None
        await self._update_channel_name_now("online", force=True)
        await self._update_channel_name_now("tps", force=True)

    # ---------------- message handling ----------------

    async def _handle_message(self, payload: Dict[str, object]):
        if payload.get("type") != "server.stats":
            return

        # realm
        realm = (payload.get("realm") or (payload.get("data") or {}).get("realm") or "").strip()
        if realm and realm != self.REALM:
            # не наш мир — игнор статуса (кадр уже учтён как активность)
            return

        data = payload.get("data") or {}
        # players
        players_section = data.get("players") or {}
        players = _to_int(
            data.get("players_online"),
            data.get("players_count"),
            players_section.get("online"),
            len(data.get("players_list") or []),
        )
        max_players = _to_int(
            data.get("players_max"),
            players_section.get("max"),
        )
        # tps/mspt
        tps_section = data.get("tps") or {}
        tps_1m = _to_float(tps_section.get("1m"), data.get("tps_1m"))
        mspt = _to_float(tps_section.get("mspt"), data.get("mspt"))

        # обновляем только нужные поля
        self.server_status.update({
            "realm": self.REALM,
            "online": True,
            "players": players,
            "max_players": max_players,
            "tps_1m": tps_1m,
            "mspt": mspt,
        })
        self._last_stats_ts = time.time()

        # триггеры обновления имён
        players_changed = (self._prev_players is None) or (players != self._prev_players)
        self._prev_players = players

        def _changed(prev: Optional[float], cur: Optional[float], eps: float) -> bool:
            if prev is None and cur is None:
                return False
            if prev is None or cur is None:
                return True
            return abs(prev - cur) >= eps

        tps_changed = _changed(self._prev_tps, tps_1m, self.TPS_CHANGE_EPS)
        mspt_changed = _changed(self._prev_mspt, mspt, self.MSPT_CHANGE_EPS)
        self._prev_tps = tps_1m
        self._prev_mspt = mspt

        await self._update_channel_name_now("online", force=players_changed)
        await self._update_channel_name_now("tps", force=(tps_changed or mspt_changed))

    # ---------------- periodic fallback (не грузим: раз в 60с) ----------------

    @tasks.loop(seconds=60)
    async def periodic_update(self):
        try:
            await self._update_channel_name_now("online", force=False)
            await self._update_channel_name_now("tps", force=False)
        except Exception as e:
            if self.DEBUG:
                print(f"[MinecraftCog] periodic_update error: {e!r}")

    @connect_manager.before_loop
    @ping_loop.before_loop
    @periodic_update.before_loop
    async def _before_tasks(self):
        await self.bot.wait_until_ready()

    # ---------------- slash commands ----------------

    @commands.slash_command(
        name="setup_minecraft",
        description="Создать/обновить два голосовых канала (ONLINE и TPS) для выбранного realm.",
    )
    @commands.has_permissions(administrator=True)
    async def setup_minecraft(self, inter: disnake.ApplicationCommandInteraction, category: disnake.CategoryChannel):
        await inter.response.defer(ephemeral=True)
        guild = inter.guild
        if guild is None:
            return

        try:
            # удалить старые каналы, если есть
            for ch_id in (self.voice_channel_id_online, self.voice_channel_id_tps):
                if ch_id:
                    old = guild.get_channel(ch_id)
                    if old is not None:
                        try:
                            await old.delete(reason="Пересоздание каналов статуса Minecraft")
                        except Exception:
                            pass

            vc_online = await category.create_voice_channel(
                name=f"🟡 MC {self.REALM}: загрузка (онлайн)...",
                reason="Инициализация канала онлайна Minecraft",
            )
            self.voice_channel_id_online = vc_online.id

            vc_tps = await category.create_voice_channel(
                name=f"⚙️ TPS {self.REALM}: загрузка...",
                reason="Инициализация канала TPS Minecraft",
            )
            self.voice_channel_id_tps = vc_tps.id

            self.category_id = category.id

            await self._update_channel_name_now("online", force=True)
            await self._update_channel_name_now("tps", force=True)

            s = self.server_status
            emb = disnake.Embed(title="✅ Каналы созданы", color=disnake.Color.green())
            emb.add_field(name="Realm", value=self.REALM, inline=True)
            emb.add_field(name="Статус", value="🟢 Онлайн" if s.get("online") else "🔴 Оффлайн", inline=True)
            await inter.edit_original_message(embed=emb)

        except Exception as e:
            emb = disnake.Embed(title="❌ Ошибка настройки", description=f"`{e}`", color=disnake.Color.red())
            await inter.edit_original_message(embed=emb)

    @commands.slash_command(name="minecraft_status", description="Показать текущий статус выбранного realm")
    async def minecraft_status(self, inter: disnake.ApplicationCommandInteraction):
        s = self.server_status
        color = disnake.Color.green() if s.get("online") else disnake.Color.red()
        emb = disnake.Embed(title=f"🟩 Minecraft — {self.REALM}", color=color)
        emb.add_field(name="Статус", value="🟢 Онлайн" if s.get("online") else "🔴 Оффлайн", inline=True)
        if s.get("online"):
            emb.add_field(name="Игроки", value=f"{_to_int(s.get('players',0))}/{_to_int(s.get('max_players',0))}", inline=True)
            if s.get("tps_1m") is not None:
                emb.add_field(name="TPS (1m)", value=f"{float(s['tps_1m']):.2f}", inline=True)
            if s.get("mspt") is not None:
                emb.add_field(name="MSPT", value=f"{float(s['mspt']):.2f}", inline=True)
        if self.voice_channel_id_online:
            ch = self.bot.get_channel(self.voice_channel_id_online)
            if ch:
                emb.add_field(name="Канал ONLINE", value=ch.mention, inline=False)
        if self.voice_channel_id_tps:
            ch = self.bot.get_channel(self.voice_channel_id_tps)
            if ch:
                emb.add_field(name="Канал TPS", value=ch.mention, inline=False)
        await inter.response.send_message(embed=emb, ephemeral=True)


def setup(bot: commands.Bot):
    bot.add_cog(MinecraftCog(bot))
