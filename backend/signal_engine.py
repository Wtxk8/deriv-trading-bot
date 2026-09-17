"""Moteur de signaux de trading (option A du cahier des charges).

Lecture SEULE des données de marché sur le WebSocket public Deriv : aucun
compte, aucun token, aucun ordre. Le moteur :

  1. récupère `active_symbols` (nom lisible, pip_size) ;
  2. amorce l'historique de bougies 1 min (`ticks_history`, style candles,
     120 bougies, sans abonnement) ;
  3. s'abonne aux ticks ; les bougies en direct sont agrégées depuis ces
     ticks (source unique, testable) ;
  4. évalue les stratégies MA_CROSS et RSI à chaque clôture de bougie, SPIKE
     (Boom / Crash) en temps réel sur les ticks ;
  5. suit chaque signal actif tick par tick jusqu'à TP, SL ou expiration et
     persiste chaque changement, afin de publier un taux de réussite RÉEL.

Honnêteté produit : les indices synthétiques Deriv sont produits par un
générateur aléatoire. Un signal n'est jamais une promesse ; seul le
suivi TP/SL mesuré ici dit ce qu'il a réellement donné.

La persistance (SQLite, quelques écritures par minute au plus) est faite de
façon synchrone dans la boucle : les ticks eux-mêmes ne touchent pas la base.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import websockets
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import indicators as ind
from models_signals import Signal

logger = logging.getLogger("signal_engine")

PUBLIC_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"
DEFAULT_SYMBOLS: tuple[str, ...] = ("R_75", "R_100", "BOOM1000", "CRASH1000", "BOOM500", "CRASH500")
DEFAULT_SYMBOL_NAMES: dict[str, str] = {
    "R_75": "Volatility 75 Index",
    "R_100": "Volatility 100 Index",
    "BOOM1000": "Boom 1000 Index",
    "CRASH1000": "Crash 1000 Index",
    "BOOM500": "Boom 500 Index",
    "CRASH500": "Crash 500 Index",
}

STRATEGIES: tuple[str, ...] = ("MA_CROSS", "RSI", "SPIKE")
TIMEFRAME = "1m"

EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0
ATR_PERIOD = 14
SL_ATR_MULTIPLIER = 1.5
TP_ATR_MULTIPLIER = 2.0

SIGNAL_TTL = timedelta(minutes=15)
COOLDOWN = timedelta(minutes=10)

HISTORY_COUNT = 120
MAX_CANDLES = 300
DEFAULT_PIP_DECIMALS = 4

REQUEST_TIMEOUT_SECONDS = 15.0
IDLE_TIMEOUT_SECONDS = 60.0
QUEUE_MAXSIZE = 100
RECENT_MAX_LIMIT = 200
STATS_MAX_DAYS = 365

# Connexion WS : `ws_connect(url)` doit renvoyer un awaitable donnant un objet
# doté de `async send(str)`, `async recv() -> str` et `async close()`.
WsConnect = Callable[[str], Awaitable[Any]]
Clock = Callable[[], datetime]
SessionFactory = Callable[[], Session]


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite rend des datetimes naïfs : ils sont en UTC par construction."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    value = as_utc(value)
    return value.isoformat() if value is not None else None


def normalize_symbols(symbols: Iterable[str]) -> list[str]:
    """Majuscules, sans doublon ni vide, ordre conservé."""
    out: list[str] = []
    for raw in symbols:
        symbol = str(raw).strip().upper()
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def symbols_from_env(value: Optional[str] = None) -> list[str]:
    """Symboles suivis : variable SIGNAL_SYMBOLS (séparés par des virgules)."""
    raw = os.environ.get("SIGNAL_SYMBOLS", "") if value is None else value
    return normalize_symbols(raw.split(",")) or list(DEFAULT_SYMBOLS)


def is_spike_symbol(symbol: str) -> bool:
    return symbol.startswith("BOOM") or symbol.startswith("CRASH")


def pip_decimals(value: Any) -> Optional[int]:
    """Nombre de décimales à partir d'un pip_size Deriv.

    Accepte un nombre de décimales (4) ou une valeur de pip (0.0001).
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    if number >= 1:
        return int(round(number)) if float(number).is_integer() else None
    return max(0, int(round(-math.log10(number))))


def _error_text(message: dict[str, Any]) -> str:
    error = message.get("error") or {}
    if isinstance(error, dict):
        return f"{error.get('code', '?')}: {error.get('message', '')}"
    return str(error)


def signal_to_out(row: Signal) -> dict[str, Any]:
    """Sérialisation conforme au contrat SignalOut."""
    return {
        "id": row.id,
        "symbol": row.symbol,
        "symbol_name": row.symbol_name or row.symbol,
        "strategy": row.strategy,
        "direction": row.direction,
        "entry": float(row.entry),
        "stop_loss": float(row.stop_loss),
        "take_profit": float(row.take_profit),
        "timeframe": row.timeframe,
        "created_at": _iso(row.created_at),
        "expires_at": _iso(row.expires_at),
        "status": row.status,
        "closed_at": _iso(row.closed_at),
        "close_price": float(row.close_price) if row.close_price is not None else None,
        "note": row.note or "",
    }


# ---------------------------------------------------------------------------
# Stratégies (fonctions pures)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Trigger:
    strategy: str
    direction: str  # BUY | SELL
    note: str


def ma_cross_trigger(closes: Sequence[float]) -> Optional[Trigger]:
    """EMA 9 croise EMA 21 entre les deux dernières clôtures."""
    if len(closes) < EMA_SLOW + 1:
        return None
    fast = ind.ema(closes, EMA_FAST)
    slow = ind.ema(closes, EMA_SLOW)
    if fast[-2] is None or slow[-2] is None or fast[-1] is None or slow[-1] is None:
        return None
    previous = fast[-2] - slow[-2]
    current = fast[-1] - slow[-1]
    if previous <= 0 < current:
        return Trigger("MA_CROSS", "BUY", "EMA 9 a croisé l'EMA 21 à la hausse (clôture 1 min).")
    if previous >= 0 > current:
        return Trigger("MA_CROSS", "SELL", "EMA 9 a croisé l'EMA 21 à la baisse (clôture 1 min).")
    return None


def rsi_trigger(closes: Sequence[float]) -> Optional[Trigger]:
    """RSI 14 qui ressort de la zone de survente (BUY) ou de surachat (SELL)."""
    if len(closes) < RSI_PERIOD + 2:
        return None
    values = ind.rsi(closes, RSI_PERIOD)
    previous, current = values[-2], values[-1]
    if previous is None or current is None:
        return None
    if previous < RSI_OVERSOLD <= current:
        return Trigger(
            "RSI", "BUY",
            f"RSI 14 repassé au-dessus de 30 ({previous:.1f} → {current:.1f}) : sortie de survente.",
        )
    if previous > RSI_OVERBOUGHT >= current:
        return Trigger(
            "RSI", "SELL",
            f"RSI 14 repassé sous 70 ({previous:.1f} → {current:.1f}) : sortie de surachat.",
        )
    return None


def spike_trigger(symbol: str, symbol_name: str, direction: Optional[str], change: float,
                  decimals: int = DEFAULT_PIP_DECIMALS) -> Optional[Trigger]:
    """Spike baissier sur Crash -> BUY ; spike haussier sur Boom -> SELL."""
    if direction is None:
        return None
    amount = f"{change:+.{decimals}f}"
    if symbol.startswith("CRASH") and direction == "down":
        return Trigger("SPIKE", "BUY", f"Spike baissier sur {symbol_name} ({amount} en un tick) : pari sur un rebond.")
    if symbol.startswith("BOOM") and direction == "up":
        return Trigger("SPIKE", "SELL", f"Spike haussier sur {symbol_name} ({amount} en un tick) : pari sur un repli.")
    return None


def compute_levels(direction: str, entry: float, atr_value: Optional[float],
                   decimals: int) -> Optional[tuple[float, float, float]]:
    """(entrée, SL, TP) arrondis au pip ; None si l'ATR ne permet pas de niveaux distincts."""
    if atr_value is None or not math.isfinite(atr_value) or atr_value <= 0:
        return None
    entry_r = round(entry, decimals)
    if direction == "BUY":
        stop_loss = entry_r - SL_ATR_MULTIPLIER * atr_value
        take_profit = entry_r + TP_ATR_MULTIPLIER * atr_value
    else:
        stop_loss = entry_r + SL_ATR_MULTIPLIER * atr_value
        take_profit = entry_r - TP_ATR_MULTIPLIER * atr_value
    stop_loss = round(stop_loss, decimals)
    take_profit = round(take_profit, decimals)
    if stop_loss == entry_r or take_profit == entry_r:
        return None
    return entry_r, stop_loss, take_profit


def resolve_status(direction: str, stop_loss: float, take_profit: float, quote: float) -> Optional[str]:
    """hit_tp / hit_sl si le cours atteint un niveau, sinon None."""
    if direction == "BUY":
        if quote >= take_profit:
            return "hit_tp"
        if quote <= stop_loss:
            return "hit_sl"
    else:
        if quote <= take_profit:
            return "hit_tp"
        if quote >= stop_loss:
            return "hit_sl"
    return None


def _stats_dict(counts: dict[str, int]) -> dict[str, Any]:
    hit_tp = counts.get("hit_tp", 0)
    hit_sl = counts.get("hit_sl", 0)
    decided = hit_tp + hit_sl
    return {
        "total": sum(counts.values()),
        "hit_tp": hit_tp,
        "hit_sl": hit_sl,
        "expired": counts.get("expired", 0),
        "win_rate": round(hit_tp / decided, 4) if decided else None,
    }


def empty_stats(days: int) -> dict[str, Any]:
    """Statistiques vides (fonction désactivée)."""
    return {"window_days": days, "overall": _stats_dict({}), "by_strategy": [], "by_symbol": []}


# ---------------------------------------------------------------------------
# Diffusion en mémoire
# ---------------------------------------------------------------------------

class SignalHub:
    """Diffuse les messages vers des files bornées, sans jamais bloquer le moteur.

    Un client dont la file est pleine est retiré ; on lui laisse la sentinelle
    None pour que son relais ferme la connexion (le client se reconnectera).
    """

    def __init__(self, maxsize: int = QUEUE_MAXSIZE) -> None:
        self._maxsize = maxsize
        self._queues: set[asyncio.Queue] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)

    def register(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._queues.add(queue)
        return queue

    def unregister(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    def publish(self, message: dict[str, Any]) -> None:
        for queue in list(self._queues):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self._queues.discard(queue)
                try:
                    queue.get_nowait()  # libère une place pour la sentinelle
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
                logger.info("Client du flux signaux trop lent : retiré.")


# ---------------------------------------------------------------------------
# Moteur
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _ActiveSignal:
    id: int
    direction: str
    stop_loss: float
    take_profit: float
    expires_at: datetime


@dataclass(slots=True)
class _SymbolState:
    symbol: str
    name: str
    decimals: Optional[int] = None
    candles: deque = field(default_factory=lambda: deque(maxlen=MAX_CANDLES))
    aggregator: ind.CandleAggregator = field(default_factory=ind.CandleAggregator)
    spike: Optional[ind.SpikeDetector] = None
    last_quote: Optional[float] = None

    @property
    def pip(self) -> int:
        return self.decimals if self.decimals is not None else DEFAULT_PIP_DECIMALS


def _default_ws_connect(url: str) -> Awaitable[Any]:
    return websockets.connect(
        url, ping_interval=20, ping_timeout=20, close_timeout=5, open_timeout=15, max_size=2**22,
    )


def _parse_candles(raw: Any) -> list[ind.Candle]:
    candles: list[ind.Candle] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            candle = ind.Candle(
                int(item["epoch"]), float(item["open"]), float(item["high"]),
                float(item["low"]), float(item["close"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        candles.append(candle)
    candles.sort(key=lambda c: c.epoch)
    return candles


class SignalEngine:
    """Génère, suit et diffuse les signaux sur les symboles configurés."""

    def __init__(
        self,
        session_factory: SessionFactory,
        symbols: Optional[Iterable[str]] = None,
        *,
        ws_url: str = PUBLIC_WS_URL,
        ws_connect: Optional[WsConnect] = None,
        clock: Optional[Clock] = None,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 60.0,
        sweep_interval: float = 5.0,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
        queue_maxsize: int = QUEUE_MAXSIZE,
    ) -> None:
        self._session_factory = session_factory
        self.symbols = normalize_symbols(symbols) if symbols is not None else symbols_from_env()
        self._ws_url = ws_url
        self._ws_connect: WsConnect = ws_connect or _default_ws_connect
        self._clock: Clock = clock or _utcnow
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._sweep_interval = sweep_interval
        self._request_timeout = request_timeout
        self._idle_timeout = idle_timeout

        self.hub = SignalHub(queue_maxsize)
        self._states: dict[str, _SymbolState] = {
            symbol: _SymbolState(
                symbol=symbol,
                name=symbol,
                spike=ind.SpikeDetector() if is_spike_symbol(symbol) else None,
            )
            for symbol in self.symbols
        }
        self._active: dict[str, list[_ActiveSignal]] = {symbol: [] for symbol in self.symbols}
        self._cooldowns: dict[tuple[str, str], datetime] = {}
        self._run_task: Optional[asyncio.Task] = None
        self._sweep_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._req_seq = 0
        self.connected = False

    def symbol_catalog(self) -> list[dict[str, str]]:
        """Symboles suivis et leur nom lisible (nom par défaut avant active_symbols)."""
        catalog: list[dict[str, str]] = []
        for symbol in self.symbols:
            name = self._states[symbol].name
            if not name or name == symbol:
                name = DEFAULT_SYMBOL_NAMES.get(symbol, symbol)
            catalog.append({"symbol": symbol, "name": name})
        return catalog

    # ----- diffusion -----

    def register(self) -> asyncio.Queue:
        return self.hub.register()

    def unregister(self, queue: asyncio.Queue) -> None:
        self.hub.unregister(queue)

    # ----- cycle de vie -----

    async def start(self) -> None:
        if self._run_task is not None:
            return
        self._stopping = False
        self._expire_leftovers()
        self._load_cooldowns()
        self._run_task = asyncio.create_task(self._run(), name="signal-engine")
        self._sweep_task = asyncio.create_task(self._sweep_loop(), name="signal-engine-sweep")
        logger.info("Moteur de signaux démarré (%s).", ", ".join(self.symbols))

    async def stop(self) -> None:
        self._stopping = True
        tasks = [task for task in (self._run_task, self._sweep_task) if task is not None]
        self._run_task = None
        self._sweep_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.connected = False
        logger.info("Moteur de signaux arrêté.")

    def _now(self) -> datetime:
        return as_utc(self._clock())

    def _expire_leftovers(self) -> None:
        """Signaux restés actifs d'une exécution précédente : expirés (suivi perdu)."""
        now = self._now()
        for active in self._active.values():
            active.clear()
        try:
            with self._session_factory() as db:
                result = db.execute(
                    update(Signal).where(Signal.status == "active").values(status="expired", closed_at=now)
                )
                db.commit()
        except SQLAlchemyError:
            logger.exception("Impossible d'expirer les signaux restés actifs")
            return
        if result.rowcount:
            logger.info("%d signal(s) resté(s) actif(s) passé(s) en expiré.", result.rowcount)

    def _load_cooldowns(self) -> None:
        since = self._now() - COOLDOWN
        try:
            with self._session_factory() as db:
                rows = db.execute(
                    select(Signal.symbol, Signal.strategy, func.max(Signal.created_at))
                    .where(Signal.created_at >= since)
                    .group_by(Signal.symbol, Signal.strategy)
                ).all()
        except SQLAlchemyError:
            logger.exception("Lecture des cooldowns impossible")
            return
        for symbol, strategy, created_at in rows:
            if created_at is not None:
                self._cooldowns[(symbol, strategy)] = as_utc(created_at)

    # ----- connexion au flux public -----

    async def _run(self) -> None:
        delay = self._reconnect_delay
        while not self._stopping:
            ws = None
            try:
                ws = await self._ws_connect(self._ws_url)
                await self._bootstrap(ws)
                delay = self._reconnect_delay
                self.connected = True
                logger.info("Flux Deriv public connecté (%d symboles).", len(self.symbols))
                await self._listen(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - toute panne réseau mène à la reconnexion
                logger.warning("Flux Deriv public interrompu (%s: %s)", type(exc).__name__, exc)
            finally:
                self.connected = False
                if ws is not None:
                    await self._close_quietly(ws)
            if self._stopping:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_reconnect_delay)

    @staticmethod
    async def _close_quietly(ws: Any) -> None:
        try:
            await asyncio.wait_for(ws.close(), timeout=5.0)
        except Exception:  # noqa: BLE001
            pass

    def _next_req_id(self) -> int:
        self._req_seq += 1
        return self._req_seq

    @staticmethod
    def _parse(raw: Any) -> Optional[dict[str, Any]]:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return message if isinstance(message, dict) else None

    async def _request(self, ws: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """Envoie une requête et attend la réponse de même req_id."""
        req_id = self._next_req_id()
        await ws.send(json.dumps({**payload, "req_id": req_id}))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._request_timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"pas de réponse Deriv à {next(iter(payload))}")
            message = self._parse(await asyncio.wait_for(ws.recv(), timeout=remaining))
            if message is None:
                continue
            if message.get("req_id") == req_id:
                return message
            self._route(message)

    async def _bootstrap(self, ws: Any) -> None:
        await self._load_active_symbols(ws)
        for symbol in self.symbols:
            await self._load_history(ws, symbol)
        for symbol in self.symbols:
            await ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": self._next_req_id()}))

    async def _load_active_symbols(self, ws: Any) -> None:
        message = await self._request(ws, {"active_symbols": "brief"})
        if "error" in message:
            logger.warning("active_symbols refusé (%s)", _error_text(message))
            return
        known: set[str] = set()
        for item in message.get("active_symbols") or []:
            if not isinstance(item, dict):
                continue
            code = item.get("underlying_symbol") or item.get("symbol")
            state = self._states.get(code)
            if state is None:
                continue
            known.add(code)
            name = item.get("underlying_symbol_name") or item.get("display_name")
            if name:
                state.name = str(name)
            decimals = pip_decimals(item.get("pip_size", item.get("pip")))
            if decimals is not None:
                state.decimals = decimals
        missing = [symbol for symbol in self.symbols if symbol not in known]
        if missing:
            logger.warning("Symboles absents d'active_symbols : %s", ", ".join(missing))

    async def _load_history(self, ws: Any, symbol: str) -> None:
        message = await self._request(ws, {
            "ticks_history": symbol,
            "style": "candles",
            "granularity": ind.CANDLE_GRANULARITY_SECONDS,
            "count": HISTORY_COUNT,
            "end": "latest",
        })
        state = self._states[symbol]
        if state.spike is not None:
            state.spike.reset()  # un trou de flux fausserait la variation tick-à-tick
        if "error" in message:
            logger.warning("Historique %s indisponible (%s)", symbol, _error_text(message))
            return
        decimals = pip_decimals(message.get("pip_size"))
        if decimals is not None:
            state.decimals = decimals
        candles = _parse_candles(message.get("candles"))
        state.candles.clear()
        if not candles:
            state.aggregator.seed(None)
            return
        # La dernière bougie peut être celle de la minute en cours : on la
        # reprend comme bougie ouverte, les ticks la compléteront.
        state.candles.extend(candles[:-1])
        state.aggregator.seed(candles[-1])

    async def _listen(self, ws: Any) -> None:
        while not self._stopping:
            raw = await asyncio.wait_for(ws.recv(), timeout=self._idle_timeout)
            message = self._parse(raw)
            if message is not None:
                self._route(message)

    def _route(self, message: dict[str, Any]) -> None:
        if message.get("msg_type") != "tick":
            return
        if "error" in message:
            logger.warning("Abonnement ticks refusé (%s)", _error_text(message))
            return
        tick = message.get("tick")
        if not isinstance(tick, dict):
            return
        state = self._states.get(tick.get("symbol"))
        if state is None:
            return
        try:
            quote = float(tick["quote"])
            epoch = int(tick["epoch"])
        except (KeyError, TypeError, ValueError):
            return
        if not math.isfinite(quote):
            return
        decimals = pip_decimals(tick.get("pip_size"))
        if decimals is not None:
            state.decimals = decimals
        self._on_tick(state, quote, epoch)

    # ----- traitement d'un tick -----

    def _on_tick(self, state: _SymbolState, quote: float, epoch: int) -> None:
        now = self._now()
        # 1. Échéances : un tick arrivé après l'expiration ne peut plus valider
        #    TP ni SL ; on retient le dernier cours connu AVANT ce tick.
        previous = state.last_quote if state.last_quote is not None else quote
        self._expire_symbol(state, now, previous)
        # 2. TP / SL des signaux encore actifs.
        for signal in list(self._active.get(state.symbol, ())):
            status = resolve_status(signal.direction, signal.stop_loss, signal.take_profit, quote)
            if status is not None:
                self._close(state, signal, status, quote, now)
        state.last_quote = quote
        # 3. Bougies 1 min : stratégies MA_CROSS et RSI à la clôture.
        closed = state.aggregator.add_tick(epoch, quote)
        if closed is not None:
            state.candles.append(closed)
            closes = [candle.close for candle in state.candles]
            for trigger in (ma_cross_trigger(closes), rsi_trigger(closes)):
                if trigger is not None:
                    self._emit(state, trigger, quote, now)
        # 4. Spike (Boom / Crash), en temps réel.
        if state.spike is not None:
            direction, change = state.spike.update(quote)
            trigger = spike_trigger(state.symbol, state.name, direction, change, state.pip)
            if trigger is not None:
                self._emit(state, trigger, quote, now)

    def _emit(self, state: _SymbolState, trigger: Trigger, quote: float, now: datetime) -> Optional[dict[str, Any]]:
        key = (state.symbol, trigger.strategy)
        last = self._cooldowns.get(key)
        if last is not None and now - last < COOLDOWN:
            return None
        atr_values = ind.atr(list(state.candles), ATR_PERIOD)
        levels = compute_levels(trigger.direction, quote, atr_values[-1] if atr_values else None, state.pip)
        if levels is None:
            return None
        entry, stop_loss, take_profit = levels
        expires_at = now + SIGNAL_TTL
        try:
            with self._session_factory() as db:
                row = Signal(
                    symbol=state.symbol,
                    symbol_name=state.name,
                    strategy=trigger.strategy,
                    direction=trigger.direction,
                    entry=entry,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    timeframe=TIMEFRAME,
                    created_at=now,
                    expires_at=expires_at,
                    status="active",
                    note=trigger.note[:255],
                )
                db.add(row)
                db.commit()
                out = signal_to_out(row)
        except SQLAlchemyError:
            logger.exception("Enregistrement du signal %s %s impossible", state.symbol, trigger.strategy)
            return None
        self._cooldowns[key] = now
        self._active.setdefault(state.symbol, []).append(
            _ActiveSignal(out["id"], trigger.direction, stop_loss, take_profit, expires_at)
        )
        self.hub.publish({"type": "signal", "signal": out})
        logger.info("Signal #%s %s %s %s @ %s", out["id"], state.symbol, trigger.strategy, trigger.direction, entry)
        return out

    def _expire_symbol(self, state: _SymbolState, now: datetime, price: Optional[float]) -> int:
        expired = 0
        for signal in list(self._active.get(state.symbol, ())):
            if now >= signal.expires_at:
                if self._close(state, signal, "expired", price, signal.expires_at):
                    expired += 1
        return expired

    def _close(self, state: _SymbolState, signal: _ActiveSignal, status: str,
               price: Optional[float], closed_at: datetime) -> bool:
        """Clôture persistée puis diffusée ; en cas d'erreur base, nouvel essai au tick suivant."""
        active = self._active.get(state.symbol, [])
        try:
            with self._session_factory() as db:
                row = db.get(Signal, signal.id)
                if row is None or row.status != "active":
                    if signal in active:
                        active.remove(signal)
                    return False
                row.status = status
                row.closed_at = closed_at
                row.close_price = round(price, state.pip) if price is not None else None
                db.commit()
                out = signal_to_out(row)
        except SQLAlchemyError:
            logger.exception("Mise à jour du signal #%s impossible", signal.id)
            return False
        if signal in active:
            active.remove(signal)
        self.hub.publish({"type": "update", "signal": out})
        logger.info("Signal #%s clos : %s", signal.id, status)
        return True

    async def _sweep_loop(self) -> None:
        """Expire aussi les signaux d'un symbole dont le flux s'est tu."""
        while True:
            await asyncio.sleep(self._sweep_interval)
            try:
                self.check_expirations()
            except Exception:  # noqa: BLE001
                logger.exception("Balayage des expirations en échec")

    def check_expirations(self) -> int:
        """Expire les signaux échus (cours de clôture = dernier cours connu)."""
        now = self._now()
        return sum(self._expire_symbol(state, now, state.last_quote) for state in self._states.values())

    # ----- lecture -----

    def recent(self, limit: int = 50, closed_only: bool = False) -> list[dict[str, Any]]:
        """Derniers signaux, du plus récent au plus ancien."""
        limit = max(1, min(int(limit), RECENT_MAX_LIMIT))
        stmt = select(Signal)
        if closed_only:
            stmt = stmt.where(Signal.status != "active")
        stmt = stmt.order_by(Signal.created_at.desc(), Signal.id.desc()).limit(limit)
        with self._session_factory() as db:
            return [signal_to_out(row) for row in db.scalars(stmt)]

    def stats(self, days: int = 7) -> dict[str, Any]:
        """Résultats réels des signaux émis sur la fenêtre.

        `total` compte tous les signaux émis (y compris encore actifs) ;
        win_rate = hit_tp / (hit_tp + hit_sl), None s'il n'y en a aucun.
        """
        days = max(1, min(int(days), STATS_MAX_DAYS))
        since = self._now() - timedelta(days=days)
        with self._session_factory() as db:
            rows = db.execute(
                select(Signal.strategy, Signal.symbol, Signal.status, func.count())
                .where(Signal.created_at >= since)
                .group_by(Signal.strategy, Signal.symbol, Signal.status)
            ).all()
        overall: dict[str, int] = {}
        by_strategy: dict[str, dict[str, int]] = {strategy: {} for strategy in STRATEGIES}
        by_symbol: dict[str, dict[str, int]] = {symbol: {} for symbol in self.symbols}
        for strategy, symbol, status, count in rows:
            for bucket in (overall, by_strategy.setdefault(strategy, {}), by_symbol.setdefault(symbol, {})):
                bucket[status] = bucket.get(status, 0) + int(count)
        return {
            "window_days": days,
            "overall": _stats_dict(overall),
            "by_strategy": [{"strategy": key, **_stats_dict(value)} for key, value in by_strategy.items()],
            "by_symbol": [{"symbol": key, **_stats_dict(value)} for key, value in by_symbol.items()],
        }
