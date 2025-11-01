# cogs/websocket.py
import asyncio
import json
import os
import time
from typing import Optional, Dict, List

import aiohttp
from aiohttp import WSMsgType, ClientWebSocketResponse, ClientTimeout
import disnake
from disnake.ext import commands, tasks


# -------------------- утилиты --------------------

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
    Минимальный и устойчивый WS‑клиент к bridge:
    - только ONLINE / TPS(1m) / MSPT для выбранного REALM;
    - autoping=ON (автоподтверждение PING серверу);
    - без собственных пингов/idle‑watchdog (не дёргаем соединение);
    - мягкий реконнект при реальном разрыве;
    - дебаунс переименования голосовых каналов.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Конфигурация
        self.WS_URL: str = (os.getenv("MC_WS_URL") or "ws://bridge:8765/ws").strip().rstrip("/")
        self.WS_TOKEN: str = (os.getenv("MC_WS_TOKEN") or "").strip()
        self.REALM: str = (os.getenv("MC_REALM") or "anarchy").strip()

        # Логи/тримминг
        self.DEBUG: bool = (os.getenv("MC_WS_DEBUG") or "0").strip().lower() in {"1", "true", "yes"}
        try:
            self.TRUNC: int = max(80, int(os.getenv("MC_WS_TRUNC") or "400"))
        except Exception:
            self.TRUNC = 400

        # Обновление имён каналов
        try:
            self.CHANNEL_UPDATE_MIN_SEC: int = max(3, int(os.getenv("MC_CHANNEL_UPDATE_MIN_SEC") or "10"))
        except Exception:
            self.CHANNEL_UPDATE_MIN_SEC = 10
        try:
            self.TPS_CHANGE_EPS: float = float(os.getenv("MC_TPS_CHANGE_EPS") or "0.05")
        except Exception:
            self.TPS_CHANGE_EPS = 0.05
        try:
            self.MSPT_CHANGE_EPS: float = float(os.getenv("MC_MSPT_CHANGE_EPS") or "0.05")
        except Exception:
            self.MSPT_CHANGE_EPS = 0.05

        # Каналы/категория
        self.category_id: Optional[int] = _to_id(os.getenv("MC_CATEGORY_ID"))
        self.category_name: Optional[str] = (os.getenv("MC_CATEGORY_NAME") or "").strip() or None
        self.voice_channel_id_online: Optional[int] = _to_id(os.getenv("MC_ONLINE_CHANNEL_ID"))
        self.voice_channel_id_tps: Optional[int] = _to_id(os.getenv("MC_TPS_CHANNEL_ID"))

        # Состояние WS
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[ClientWebSocketResponse] = None
        self._conn_id: int = 0  # поколение соединения

        # Активность / значения
        self._last_stats_ts: float = 0.0
        self.server_status: Dict[str, object] = {
            "realm": self.REALM,
            "online": False,
            "players": 0,
            "max_players": 0,
            "tps_1m": None,
            "mspt": None,
        }
        self._prev_players: Optional[int] = None
        self._prev_tps: Optional[float] = None
        self._prev_mspt: Optional[float] = None

        # Дебаунс имён
        self._last_online_name: Optional[str] = None
        self._last_tps_name: Optional[str] = None
        self._last_online_rename_ts: float = 0.0
        self._last_tps_rename_ts: float = 0.0

        # Запускаем фоновые циклы
        self.ensure_channels_once.start()
        self.connect_loop.start()
        self.periodic_update.start()

    # ---------------- lifecycle ----------------

    def cog_unload(self):
        for loop in (self.ensure_channels_once, self.connect_loop, self.periodic_update):
            with contextlib.suppress(Exception):
                loop.cancel()
        if self._ws is not None and not self._ws.closed:
            asyncio.create_task(self._ws.close())
        if self._session and not self._session.closed:
            asyncio.create_task(self._session.close())

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=ClientTimeout(total=None))

    # ---------------- channels helpers ----------------

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
                    name = self._build_online_name() if kind == "online" else self._build_tps_name()
                    vc = await cat.create_voice_channel(name=name or "loading...", reason=f"init {kind}")
                    if kind == "online":
                        self.voice_channel_id_online = vc.id
                    else:
                        self.voice_channel_id_tps = vc.id
                    return True
                except Exception:
                    pass

        # by category name (fallback)
        if create and not self.category_id and self.category_name:
            for g in self.bot.guilds:
                for c in g.channels:
                    if isinstance(c, disnake.CategoryChannel) and c.name == self.category_name:
                        self.category_id = c.id
                        return await self._ensure_channels_ready(kind, create=True)

        return False

    # ---------------- connect / listen ----------------

    def _url_candidates(self) -> List[str]:
        base = self.WS_URL.rstrip("/")
        return [base, base[:-3]] if base.endswith("/ws") else [f"{base}/ws", base]

    @tasks.loop(seconds=5)
    async def connect_loop(self):
        """
        Подключаемся, только если сокета нет/он закрыт.
        Без автопингов: autoping=ON — клиент автоматически отвечает PONG на серверные PING.
        """
        if self._ws is not None and not self._ws.closed:
            return

        await self._ensure_session()
        assert self._session is not None

        headers = {"Authorization": f"Bearer {self.WS_TOKEN}"} if self.WS_TOKEN else None

        for url in self._url_candidates():
            try:
                ws = await self._session.ws_connect(
                    url,
                    headers=headers,
                    autoping=True,      # <-- ВАЖНО: отвечаем на серверные PING
                    heartbeat=None,     # свой ping не шлём; сервер сам пингует
                    timeout=15.0,
                    receive_timeout=None,
                    max_msg_size=4 * 1024 * 1024,
                )
                self._conn_id += 1
                conn_id = self._conn_id
                self._ws = ws
                self._last_stats_ts = 0.0

                if self.DEBUG:
                    status = getattr(ws, "response", None).status if getattr(ws, "response", None) else "?"
                    print(f"[MinecraftCog] WS connected #{conn_id}: {url} (autoping=ON, heartbeat=None) status={status}")

                asyncio.create_task(self._listen_loop(ws, conn_id))
                return
            except Exception as e:
                if self.DEBUG:
                    print(f"[MinecraftCog] connect fail: {url}: {e!r}")
                # пробуем следующий вариант URL

    async def _listen_loop(self, ws: ClientWebSocketResponse, conn_id: int):
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    raw = msg.data
                    if self.DEBUG:
                        preview = raw if len(raw) <= self.TRUNC else raw[: self.TRUNC] + "…"
                        print(f"[MinecraftCog] WS TEXT#{conn_id} ({len(raw)}b): {preview}")
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue
                    await self._handle_message(payload)

                elif msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR):
                    break
                # PING/PONG обрабатывает autoping, BINARY игнорируем
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self.DEBUG:
                print(f"[MinecraftCog] listen exception#{conn_id}: {e!r}")
        finally:
            code = getattr(ws, "close_code", None)
            if self.DEBUG:
                print(f"[MinecraftCog] WS finished#{conn_id} code={code}")
            # Закрываем именно этот сокет
            try:
                if not ws.closed:
                    await ws.close()
            except Exception:
                pass
            # Если за это время уже подключились новым conn_id — оффлайн не трогаем
            if conn_id == self._conn_id:
                await self._go_offline()

    # ---------------- имена каналов ----------------

    def _build_online_name(self) -> str:
        s = self.server_status
        realm = s.get("realm", self.REALM)
        if s.get("online"):
            p = _to_int(s.get("players", 0))
            m = _to_int(s.get("max_players", 0))
            base = f"🟢 MC {realm}: {p}/{m}" if m else f"🟢 MC {realm}: {p}"
        else:
            base = f"🔴 MC {realm}: оффлайн"
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
            mspt_part = f" | {mspt:.2f} mspt" if mspt is not None else ""
            name = f"⚙️ TPS {realm}: {tps_part}{mspt_part}"
        return name[:95]

    async def _update_channel_name_now(self, kind: str, force: bool = False):
        ch_id = self.voice_channel_id_online if kind == "online" else self.voice_channel_id_tps
        if not ch_id:
            return
        ch = self.bot.get_channel(ch_id)
        if ch is None:
            return

        new_name = self._build_online_name() if kind == "online" else self._build_tps_name()
        now = time.time()

        if not force:
            last_name = self._last_online_name if kind == "online" else self._last_tps_name
            last_ts = self._last_online_rename_ts if kind == "online" else self._last_tps_rename_ts
            if last_name == new_name:
                if self.DEBUG: print(f"[MinecraftCog] rename[{kind}] skip: same name")
                return
            if now - last_ts < self.CHANNEL_UPDATE_MIN_SEC:
                if self.DEBUG: print(f"[MinecraftCog] rename[{kind}] skip: debounced ({now - last_ts:.1f}s < {self.CHANNEL_UPDATE_MIN_SEC}s)")
                return

        try:
            await ch.edit(name=new_name, reason=f"MC status ({kind})")
            if kind == "online":
                self._last_online_name = new_name
                self._last_online_rename_ts = now
            else:
                self._last_tps_name = new_name
                self._last_tps_rename_ts = now
            if self.DEBUG: print(f"[MinecraftCog] channel rename [{kind}] -> {new_name}")
        except Exception as e:
            print(f"[MinecraftCog] rename[{kind}] error: {e!r}")

    # ---------------- оффлайн/онлайн ----------------

    async def _go_offline(self):
        # помечаем оффлайн минимально
        self.server_status.update({
            "online": False,
            "players": 0,
            "tps_1m": None,
            "mspt": None,
        })
        self._prev_players = None
        self._prev_tps = None
        self._prev_mspt = None
        await self._update_channel_name_now("online", force=True)
        await self._update_channel_name_now("tps", force=True)

    # ---------------- обработка кадров ----------------

    async def _handle_message(self, payload: Dict[str, object]):
        t = payload.get("type")
        if t not in ("server.stats", "stats.report"):
            return

        # realm
        data = payload.get("data") or payload.get("payload") or {}
        realm = (payload.get("realm") or data.get("realm") or "").strip()
        if realm and realm != self.REALM:
            return

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

        # обновление статуса
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

    # ---------------- периодический страховочный апдейт ----------------

    @tasks.loop(seconds=60)
    async def periodic_update(self):
        try:
            await self._update_channel_name_now("online", force=False)
            await self._update_channel_name_now("tps", force=False)
        except Exception as e:
            if self.DEBUG:
                print(f"[MinecraftCog] periodic_update error: {e!r}")

    @connect_loop.before_loop
    @periodic_update.before_loop
    async def _before_tasks(self):
        await self.bot.wait_until_ready()


def setup(bot: commands.Bot):
    bot.add_cog(MinecraftCog(bot))
