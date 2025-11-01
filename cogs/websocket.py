# cogs/websocket.py
import asyncio
import json
import os
import time
from typing import Optional, Dict, List

import aiohttp
from aiohttp import WSMsgType, ClientWebSocketResponse
import disnake
from disnake.ext import commands, tasks


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


class MinecraftCog(commands.Cog):
    """
    Подключение к локальному bridge через aiohttp.ws_connect.
    ДВА голосовых канала: ONLINE и TPS.
    Надёжный парсинг online (fallback: sum(worlds[].players)).
    autoping=OFF, idle-watchdog по любому кадру. Принудительное переименование при изменениях.
    """

    # ---------------- init / config ----------------

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # WS config
        self.WS_URL: str = (os.getenv("MC_WS_URL") or "ws://bridge:8765/ws").strip().rstrip("/")
        self.WS_TOKEN: str = (os.getenv("MC_WS_TOKEN") or "").replace("\r", "").replace("\n", "").strip()
        self.REALM: str = (os.getenv("MC_REALM") or "anarchy").strip()

        # Logs
        self.DEBUG: bool = (os.getenv("MC_WS_DEBUG") or "0").strip().lower() in {"1", "true", "yes"}
        self.LOG_JSON: bool = (os.getenv("MC_WS_LOG_JSON") or "0").strip().lower() in {"1", "true", "yes"}
        try:
            self.TRUNC: int = max(80, int(os.getenv("MC_WS_TRUNC") or "600"))
        except Exception:
            self.TRUNC = 600

        # Channel rename debounce + show mspt
        try:
            self.CHANNEL_UPDATE_MIN_SEC: int = max(3, int(os.getenv("MC_CHANNEL_UPDATE_MIN_SEC") or "10"))
        except Exception:
            self.CHANNEL_UPDATE_MIN_SEC = 10
        self.CHANNEL_SHOW_MSPT: bool = (os.getenv("MC_CHANNEL_SHOW_MSPT") or "1").strip().lower() in {"1", "true", "yes"}

        # Force immediate rename when values changed
        self.FORCE_ON_CHANGE: bool = (os.getenv("MC_FORCE_UPDATE_ON_CHANGE") or "1").strip().lower() in {"1", "true", "yes"}
        try:
            self.TPS_CHANGE_EPS: float = float(os.getenv("MC_TPS_CHANGE_EPS") or "0.05")
        except Exception:
            self.TPS_CHANGE_EPS = 0.05
        try:
            self.MSPT_CHANGE_EPS: float = float(os.getenv("MC_MSPT_CHANGE_EPS") or "0.05")
        except Exception:
            self.MSPT_CHANGE_EPS = 0.05

        # Optional pre-set IDs / category
        self.ENV_ONLINE_ID: Optional[int] = _to_id(os.getenv("MC_ONLINE_CHANNEL_ID"))
        self.ENV_TPS_ID: Optional[int] = _to_id(os.getenv("MC_TPS_CHANNEL_ID"))
        self.ENV_CATEGORY_ID: Optional[int] = _to_id(os.getenv("MC_CATEGORY_ID"))
        self.ENV_CATEGORY_NAME: Optional[str] = (os.getenv("MC_CATEGORY_NAME") or "").strip() or None

        # WS state
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[ClientWebSocketResponse] = None
        self._listener_task: Optional[asyncio.Task] = None
        self._connecting: bool = False
        self.connected: bool = False
        self._last_frame_ts: float = 0.0
        self._last_stats_ts: float = 0.0

        # Idle reconnect threshold (по любому кадру)
        try:
            self.IDLE_RECONNECT_SEC: int = max(20, int(os.getenv("MC_IDLE_RECONNECT_SEC") or "120"))
        except Exception:
            self.IDLE_RECONNECT_SEC = 120

        # Discord entities
        self.category_id: Optional[int] = self.ENV_CATEGORY_ID
        self.voice_channel_id_online: Optional[int] = self.ENV_ONLINE_ID
        self.voice_channel_id_tps: Optional[int] = self.ENV_TPS_ID

        # Debounce memory
        self._last_online_name: Optional[str] = None
        self._last_tps_name: Optional[str] = None
        self._last_online_rename_ts: float = 0.0
        self._last_tps_rename_ts: float = 0.0

        # Current status
        self.server_status: Dict[str, object] = {
            "realm": self.REALM,
            "online": False,
            "players": 0,
            "max_players": 0,
            "tps_1m": None,
            "tps_5m": None,
            "tps_15m": None,
            "mspt": None,
        }
        # Previous values (for change triggers)
        self._prev_players: Optional[int] = None
        self._prev_tps: Optional[float] = None
        self._prev_mspt: Optional[float] = None

        # Loops
        self.ensure_channels_once.start()
        self.connect_websocket.start()
        self.periodic_update.start()
        self.idle_watchdog.start()

        if self.DEBUG:
            print(f"[MinecraftCog] DEBUG on, trunc={self.TRUNC}, json_log={'on' if self.LOG_JSON else 'off'}, "
                  f"debounce={self.CHANNEL_UPDATE_MIN_SEC}s, show_mspt={self.CHANNEL_SHOW_MSPT}, "
                  f"idle_reconnect={self.IDLE_RECONNECT_SEC}s, force_on_change={self.FORCE_ON_CHANGE}, "
                  f"eps[tps]={self.TPS_CHANGE_EPS}, eps[mspt]={self.MSPT_CHANGE_EPS}")

    # ---------------- lifecycle helpers ----------------

    def cog_unload(self):
        for loop in (self.ensure_channels_once, self.connect_websocket, self.periodic_update, self.idle_watchdog):
            try:
                loop.cancel()
            except Exception:
                pass
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
        asyncio.create_task(self._close_ws())

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def _close_ws(self):
        try:
            if self._ws is not None and not self._ws.closed:
                await self._ws.close()
        except Exception:
            pass
        finally:
            self._ws = None
            self.connected = False

    # ---------------- one-shot channel ensure ----------------

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
            if self.DEBUG:
                print(f"[MinecraftCog] ensure[{kind}]: id {ch_id} не в кеше — пытаюсь найти/создать")

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
                        print(f"[MinecraftCog] ensure[{kind}]: создан канал id={vc.id}")
                    return True
                except Exception as e:
                    print(f"[MinecraftCog] ensure[{kind}] create ERROR: {e!r}")

        # by category name (fallback)
        if create and not self.category_id and self.ENV_CATEGORY_NAME:
            for g in self.bot.guilds:
                for c in g.channels:
                    if isinstance(c, disnake.CategoryChannel) and c.name == self.ENV_CATEGORY_NAME:
                        self.category_id = c.id
                        if self.DEBUG:
                            print(f"[MinecraftCog] ensure[{kind}]: найдена категория '{self.ENV_CATEGORY_NAME}' id={c.id}")
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
                    if self.DEBUG:
                        print(f"[MinecraftCog] ensure[{kind}]: найден по имени id={c.id}")
                    return True

        if self.DEBUG:
            print(f"[MinecraftCog] ensure[{kind}]: нет канала и не удалось создать — укажи MC_CATEGORY_ID/NAME или /setup_minecraft")
        return False

    # ---------------- connect & listen ----------------

    def _url_candidates(self) -> List[str]:
        base = self.WS_URL.rstrip("/")
        return [base, base[:-3]] if base.endswith("/ws") else [f"{base}/ws", base]

    @tasks.loop(seconds=5)
    async def connect_websocket(self):
        if self.connected or self._connecting:
            return

        self._connecting = True
        await self._ensure_session()
        assert self._session is not None

        headers = {"Authorization": f"Bearer {self.WS_TOKEN}"} if self.WS_TOKEN else None

        for url in self._url_candidates():
            try:
                ws = await self._session.ws_connect(
                    url,
                    headers=headers,
                    heartbeat=None,   # критично: не шлём ping
                    autoping=False,   # и не ждём pong
                    timeout=15.0,
                    receive_timeout=None,
                    max_msg_size=8 * 1024 * 1024,
                )
                self._ws = ws
                self.connected = True
                self._last_frame_ts = time.time()
                self._last_stats_ts = 0.0

                status = getattr(ws, "response", None).status if getattr(ws, "response", None) else "?"
                h = getattr(ws, "response", None).headers if getattr(ws, "response", None) else {}
                h_preview = {k: v for k, v in list(h.items())[:6]} if h else {}
                print(f"[MinecraftCog] WebSocket подключён: {url} (realm={self.REALM}) status={status} headers={h_preview} (autoping=OFF)")

                if self._listener_task and not self._listener_task.done():
                    self._listener_task.cancel()
                self._listener_task = asyncio.create_task(self._listen_loop())
                return

            except Exception as e:
                print(f"[MinecraftCog] Не удалось подключиться к {url}: {e!r}")

        self._connecting = False

    async def _listen_loop(self):
        assert self._ws is not None
        ws = self._ws
        self._connecting = False
        try:
            async for msg in ws:
                # любой кадр — сеть жива
                self._last_frame_ts = time.time()

                if msg.type == WSMsgType.TEXT:
                    raw = msg.data
                    if self.DEBUG:
                        preview = raw if len(raw) <= self.TRUNC else raw[: self.TRUNC] + "…"
                        print(f"[MinecraftCog] WS recv TEXT ({len(raw)}b): {preview}")
                    try:
                        payload = json.loads(raw)
                    except Exception as e:
                        if self.DEBUG:
                            print(f"[MinecraftCog] WS json error: {e!r}")
                        continue
                    await self._handle_message(payload)

                elif msg.type == WSMsgType.BINARY:
                    if self.DEBUG:
                        print(f"[MinecraftCog] WS recv BINARY ({len(msg.data)}b) — пропущено")
                    continue

                elif msg.type == WSMsgType.ERROR:
                    print(f"[MinecraftCog] WS error frame: {ws.exception()}")
                    break

                elif msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSING):
                    if self.DEBUG:
                        print("[MinecraftCog] WS closed by remote")
                    break

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[MinecraftCog] Ошибка чтения WS: {e!r}")
        finally:
            code = getattr(ws, "close_code", None)
            reason = getattr(ws, "close_reason", None)
            print(f"[MinecraftCog] WS закрыт. code={code} reason={reason!r}")
            await self._close_ws()

    # ---------------- idle watchdog ----------------

    @tasks.loop(seconds=5)
    async def idle_watchdog(self):
        if not self.connected or self._last_frame_ts <= 0:
            return
        idle = time.time() - self._last_frame_ts
        if idle > self.IDLE_RECONNECT_SEC:
            print(f"[MinecraftCog] ⚠️  Нет кадров {int(idle)}s (> {self.IDLE_RECONNECT_SEC}s) — реконнект")
            await self._close_ws()

    @idle_watchdog.before_loop
    async def _before_idle(self):
        await self.bot.wait_until_ready()

    # ---------------- channel names ----------------

    def _build_online_name(self) -> str:
        s = self.server_status
        realm = s.get("realm", "realm")
        if s.get("online"):
            p = _to_int(s.get("players", 0))
            m = _to_int(s.get("max_players", 0))
            base = f"🟢 Онлайн: {p}" if m else f"🟢 Онлайн: {p}"
        else:
            base = f"🔴 Выключен"
        return base[:95]

    def _build_tps_name(self) -> str:
        s = self.server_status
        realm = s.get("realm", "realm")
        tps_1m = _to_float(s.get("tps_1m"))
        mspt = _to_float(s.get("mspt"))
        if not s.get("online"):
            name = f"⚙️ TPS: Отсуствует"
        else:
            tps_part = f"{tps_1m:.1f}" if tps_1m is not None else "—"
            mspt_part = f" | {mspt:.2f} mspt" if (self.CHANNEL_SHOW_MSPT and mspt is not None) else ""
            name = f"⚙️ TPS Сервера: {tps_part}"
        return name[:95]

    async def _update_channel_name_now(self, kind: str, force: bool = False):
        ready = await self._ensure_channels_ready(kind, create=False) or await self._ensure_channels_ready(kind, create=True)
        if not ready:
            if self.DEBUG:
                print(f"[MinecraftCog] rename[{kind}] skip: канал не готов")
            return

        ch_id = self.voice_channel_id_online if kind == "online" else self.voice_channel_id_tps
        ch = self.bot.get_channel(ch_id) if ch_id else None
        if ch is None:
            if self.DEBUG:
                print(f"[MinecraftCog] rename[{kind}] skip: channel id {ch_id} not found in cache")
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

    # ---------------- message handling ----------------

    async def _handle_message(self, payload: Dict[str, object]):
        typ = payload.get("type")
        if typ != "server.stats":
            if typ == "error":
                d = payload.get("data") or {}
                print(f"[MinecraftCog] WS error: {d}")
            else:
                if self.DEBUG:
                    print(f"[MinecraftCog] WS ignore type={typ}")
            return

        realm = (payload.get("realm") or (payload.get("data") or {}).get("realm") or "").strip()
        if realm != self.REALM:
            if self.DEBUG:
                print(f"[MinecraftCog] WS stats for other realm='{realm}' — игнор")
            return

        data = payload.get("data") or {}
        players_obj = data.get("players") or {}
        tps_obj = data.get("tps") or {}
        worlds = data.get("worlds") or []

        # --- players: надежный расчёт ---
        # первичный: явные поля
        p_primary = _to_int(
            data.get("players_online"),
            data.get("players_count"),
            players_obj.get("online"),
            len(data.get("players_list") or []),
        )
        # fallback: суммируем игроков по мирам
        p_worlds = 0
        try:
            p_worlds = sum(_to_int(w.get("players", 0)) for w in worlds if isinstance(w, dict))
        except Exception:
            p_worlds = 0
        players = p_primary if p_primary > 0 else p_worlds

        # max players
        max_players = _to_int(
            data.get("players_max"),
            players_obj.get("max"),
        )

        # tps / mspt
        tps_1m = _to_float(tps_obj.get("1m"), data.get("tps_1m"))
        tps_5m = _to_float(tps_obj.get("5m"))
        tps_15m = _to_float(tps_obj.get("15m"))
        mspt = _to_float(tps_obj.get("mspt"), data.get("mspt"))

        # обновляем статус
        self.server_status.update({
            "realm": self.REALM,
            "online": True,
            "players": players,
            "max_players": max_players,
            "tps_1m": tps_1m,
            "tps_5m": tps_5m,
            "tps_15m": tps_15m,
            "mspt": mspt,
        })
        self._last_stats_ts = time.time()

        # лог
        if self.LOG_JSON or self.DEBUG:
            src = "primary" if p_primary > 0 else "worlds_sum"
            log_obj = {
                "ts": int(self._last_stats_ts),
                "type": "server.stats",
                "realm": self.REALM,
                "players": players,
                "players_src": src,
                "worlds_sum": p_worlds,
                "max": max_players,
                "tps_1m": tps_1m,
                "tps_5m": tps_5m,
                "tps_15m": tps_15m,
                "mspt": mspt,
            }
            line = json.dumps(log_obj, ensure_ascii=False)
            print(line if self.LOG_JSON else f"[MinecraftCog] JSON-STAT {line}")

        # --- триггеры обновления ---
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

        # обновляем имена (с обходом дебаунса при реальном изменении)
        await self._update_channel_name_now("online", force=self.FORCE_ON_CHANGE and players_changed)
        await self._update_channel_name_now("tps", force=self.FORCE_ON_CHANGE and (tps_changed or mspt_changed))

    # ---------------- periodic fallback ----------------

    @tasks.loop(seconds=30)
    async def periodic_update(self):
        await self._update_channel_name_now("online", force=True)
        await self._update_channel_name_now("tps", force=True)

    @connect_websocket.before_loop
    @periodic_update.before_loop
    @idle_watchdog.before_loop
    async def _before_tasks(self):
        await self.bot.wait_until_ready()

    # ---------------- slash commands ----------------

    @commands.slash_command(
        name="setup_minecraft",
        description="Создать/обновить ДВА голосовых канала (ONLINE и TPS) для выбранного realm.",
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
            emb = disnake.Embed(title="✅ Система Minecraft настроена (2 канала)", color=disnake.Color.green())
            emb.add_field(name="Realm", value=self.REALM, inline=True)
            emb.add_field(name="WebSocket", value="🟢 Подключен" if self.connected else "🔴 Отключен", inline=True)
            emb.add_field(name="Статус", value="🟢 Онлайн" if s.get("online") else "🔴 Оффлайн", inline=True)
            if s.get("online"):
                emb.add_field(name="Игроки", value=f"{_to_int(s.get('players',0))}/{_to_int(s.get('max_players',0))}", inline=True)
                if s.get("tps_1m") is not None:
                    emb.add_field(name="TPS (1m)", value=f"{float(s['tps_1m']):.2f}", inline=True)
                if s.get("mspt") is not None:
                    emb.add_field(name="MSPT", value=f"{float(s['mspt']):.2f}", inline=True)
            emb.add_field(name="Канал ONLINE", value=vc_online.mention, inline=False)
            emb.add_field(name="Канал TPS", value=vc_tps.mention, inline=False)
            await inter.edit_original_message(embed=emb)

        except Exception as e:
            emb = disnake.Embed(title="❌ Ошибка настройки", description=f"`{e}`", color=disnake.Color.red())
            await inter.edit_original_message(embed=emb)

    @commands.slash_command(name="minecraft_status", description="Показать текущий статус выбранного realm")
    async def minecraft_status(self, inter: disnake.ApplicationCommandInteraction):
        s = self.server_status
        color = disnake.Color.green() if s.get("online") else disnake.Color.red()
        emb = disnake.Embed(title=f"🟩 Minecraft — {self.REALM}", color=color)
        emb.add_field(name="WebSocket", value="🟢 Подключен" if self.connected else "🔴 Отключен", inline=True)
        emb.add_field(name="Статус", value="🟢 Онлайн" if s.get("online") else "🔴 Оффлайн", inline=True)
        if s.get("online"):
            emb.add_field(name="Игроки", value=f"**{_to_int(s.get('players',0))}/{_to_int(s.get('max_players',0))}**", inline=False)
            if s.get("tps_1m") is not None:
                emb.add_field(name="TPS (1m)", value=f"{float(s['tps_1m']):.2f}", inline=True)
            if s.get("tps_5m") is not None:
                emb.add_field(name="TPS (5m)", value=f"{float(s['tps_5m']):.2f}", inline=True)
            if s.get("tps_15m") is not None:
                emb.add_field(name="TPS (15m)", value=f"{float(s['tps_15m']):.2f}", inline=True)
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
