"""Tests du moteur de signaux : indicateurs, stratégies (faux WebSocket injecté),
cooldown, suivi TP/SL/expiration, persistance, diffusion, REST et WebSocket.

Aucun accès réseau : le flux public Deriv est remplacé par un faux serveur.
L'environnement (base SQLite temporaire, JWT_SECRET) est fixé par conftest.py.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from starlette.websockets import WebSocketDisconnect

import auth
import database
import indicators as ind
import signal_engine as se
from models_signals import Signal
from routers import signals as signals_router

database.Base.metadata.create_all(bind=database.engine)

UTC = timezone.utc
T0 = 1_789_400_040 - (1_789_400_040 % 60)          # début de la 1re bougie d'historique
LIVE_MINUTE = T0 + 60 * se.HISTORY_COUNT            # 1re minute après l'historique
TEST_ERA = datetime(2030, 1, 1, tzinfo=UTC)         # horloges fictives de ce fichier : >= 2030
NAMES = {
    "R_75": "Volatility 75 Index",
    "R_100": "Volatility 100 Index",
    "BOOM1000": "Boom 1000 Index",
    "CRASH1000": "Crash 1000 Index",
    "BOOM500": "Boom 500 Index",
    "CRASH500": "Crash 500 Index",
}
SIGNAL_KEYS = {
    "id", "symbol", "symbol_name", "strategy", "direction", "entry", "stop_loss", "take_profit",
    "timeframe", "created_at", "expires_at", "status", "closed_at", "close_price", "note",
}
STATS_KEYS = {"total", "hit_tp", "hit_sl", "expired", "win_rate"}
WARM_UP_PATTERN = (0.02, -0.01, 0.0, 0.01, -0.02)   # |variations| : écart-type ≈ 0.0075


@pytest.fixture(autouse=True)
def _purge_signaux_fictifs():
    """Les horloges fictives (>= 2030) ne doivent pas se voir d'un test à l'autre (cooldowns)."""
    with database.SessionLocal() as db:
        db.execute(delete(Signal).where(Signal.created_at >= TEST_ERA))
        db.commit()
    yield


# ---------------------------------------------------------------------------
# Outils de test
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def trend_candles(start_price: float, step: float, count: int = se.HISTORY_COUNT) -> list[dict]:
    """Tendance régulière ; chaque bougie a un True Range de |step| + 1 (ATR = 2 pour step = ±1)."""
    candles = []
    for i in range(count):
        close = start_price + step * i
        open_ = close - step
        candles.append({
            "open": open_,
            "high": max(open_, close) + 0.5,
            "low": min(open_, close) - 0.5,
            "close": close,
            "epoch": T0 + 60 * i,
        })
    return candles


_CLOSED = object()


class FakeDerivWs:
    """Faux WebSocket public Deriv : répond à active_symbols / ticks_history et pousse des ticks."""

    def __init__(self, history: dict[str, list[dict]]) -> None:
        self.history = history
        self.sent: list[dict] = []
        self.subscribed: set[str] = set()
        self.closed = False
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._idle = asyncio.Event()

    async def send(self, text: str) -> None:
        message = json.loads(text)
        self.sent.append(message)
        req_id = message.get("req_id")
        if "active_symbols" in message:
            self._push({
                "msg_type": "active_symbols",
                "req_id": req_id,
                "active_symbols": [
                    {
                        "underlying_symbol": symbol,
                        "underlying_symbol_name": NAMES.get(symbol, symbol),
                        "pip_size": 4,
                        "exchange_is_open": 1,
                        "market": "synthetic_index",
                        "submarket": "random_index",
                    }
                    for symbol in self.history
                ],
            })
        elif "ticks_history" in message:
            symbol = message["ticks_history"]
            if symbol in self.history:
                self._push({"msg_type": "candles", "req_id": req_id,
                            "candles": self.history[symbol], "pip_size": 4})
            else:
                self._push({"msg_type": "candles", "req_id": req_id,
                            "error": {"code": "InvalidSymbol", "message": "Symbole inconnu"}})
        elif "ticks" in message:
            self.subscribed.add(message["ticks"])

    def _push(self, item) -> None:
        self._idle.clear()
        self._incoming.put_nowait(item if item is _CLOSED else json.dumps(item))

    def tick(self, symbol: str, quote: float, epoch: int) -> None:
        self._push({
            "msg_type": "tick",
            "req_id": 1000,
            "tick": {"symbol": symbol, "quote": quote, "epoch": epoch, "pip_size": 4,
                     "ask": quote, "bid": quote, "id": "t"},
            "subscription": {"id": "s"},
        })

    def drop(self) -> None:
        """Simule une coupure réseau."""
        self._push(_CLOSED)

    async def recv(self) -> str:
        if self._incoming.empty():
            self._idle.set()
        item = await self._incoming.get()
        if item is _CLOSED:
            raise ConnectionError("connexion coupée (test)")
        return item

    async def close(self) -> None:
        self.closed = True

    async def settle(self, timeout: float = 3.0) -> None:
        """Attend que le moteur ait traité tout ce qui a été poussé."""
        await asyncio.wait_for(self._idle.wait(), timeout)


def make_connect(*fakes: FakeDerivWs):
    pending = list(fakes)
    urls: list[str] = []

    async def connect(url: str):
        urls.append(url)
        if not pending:
            raise ConnectionError("aucun faux serveur disponible")
        return pending.pop(0)

    return connect, urls


async def start_engine(symbols, history, clock, *extra_fakes):
    fake = FakeDerivWs(history)
    connect, urls = make_connect(fake, *extra_fakes)
    engine = se.SignalEngine(
        database.SessionLocal, symbols, ws_connect=connect, clock=clock,
        reconnect_delay=0.01, sweep_interval=3600,
    )
    await engine.start()
    await fake.settle()
    return engine, fake, urls


def drain(queue: asyncio.Queue) -> list:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def warm_up(fake: FakeDerivWs, symbol: str, quote: float, n: int = 100) -> float:
    """100 petites variations : remplit la fenêtre du détecteur de spike."""
    for i in range(n):
        quote = round(quote + WARM_UP_PATTERN[i % len(WARM_UP_PATTERN)], 4)
        fake.tick(symbol, quote, LIVE_MINUTE + 1)
    return quote


def load_signal(signal_id: int) -> dict:
    with database.SessionLocal() as db:
        return se.signal_to_out(db.get(Signal, signal_id))


def insert_signal(*, symbol: str = "R_75", strategy: str = "MA_CROSS", direction: str = "BUY",
                  status: str = "active", created_at: datetime | None = None) -> int:
    created_at = created_at or datetime.now(UTC)
    closed = status != "active"
    with database.SessionLocal() as db:
        row = Signal(
            symbol=symbol, symbol_name=NAMES.get(symbol, symbol), strategy=strategy,
            direction=direction, entry=100.0, stop_loss=97.0, take_profit=104.0, timeframe="1m",
            created_at=created_at, expires_at=created_at + timedelta(minutes=15), status=status,
            closed_at=created_at + timedelta(minutes=5) if closed else None,
            close_price={"hit_tp": 104.0, "hit_sl": 97.0, "expired": 100.5}.get(status),
            note="Signal de test.",
        )
        db.add(row)
        db.commit()
        return row.id


def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition non atteinte à temps")
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# Indicateurs (valeurs de référence calculées à la main)
# ---------------------------------------------------------------------------

def test_sma_et_ema():
    assert ind.sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]
    # alpha = 0.5 ; amorce = SMA(2, 4, 6) = 4 ; 4 + 0.5 x (8 - 4) = 6 ; 6 + 0.5 x (4 - 6) = 5
    assert ind.ema([2, 4, 6, 8, 4], 3) == [None, None, 4.0, 6.0, 5.0]
    assert ind.ema([1, 2], 3) == [None, None]


def test_rsi_wilder():
    # Variations : +1, -0.5, +1, -0.5, +1 ; période 3.
    values = ind.rsi([10, 11, 10.5, 11.5, 11, 12], 3)
    assert values[:3] == [None, None, None]
    assert values[3] == pytest.approx(80.0)               # moy. gains 2/3, pertes 1/6 : RS = 4
    assert values[4] == pytest.approx(100 - 100 / 2.6)    # 4/9 et 5/18 : RS = 1.6
    assert values[5] == pytest.approx(100 - 100 / 4.4)    # 17/27 et 5/27 : RS = 3.4
    assert ind.rsi([float(i) for i in range(20)])[-1] == 100.0
    assert ind.rsi([float(-i) for i in range(20)])[-1] == 0.0
    assert ind.rsi([5.0] * 20)[-1] == 50.0
    assert ind.rsi([1.0] * 14) == [None] * 14             # il faut period + 1 cours


def test_atr_wilder():
    candles = [
        ind.Candle(0, 9, 10, 8, 9),
        ind.Candle(60, 10, 11, 10, 10.5),       # TR = |11 - 9| = 2
        ind.Candle(120, 11, 12, 11, 11.5),      # TR = |12 - 10.5| = 1.5
        ind.Candle(180, 11.6, 11.8, 11.6, 11.7),  # TR = |11.8 - 11.5| = 0.3
        ind.Candle(240, 12, 13, 11, 12),        # TR = 13 - 11 = 2
    ]
    values = ind.atr(candles, 3)
    assert values[:3] == [None, None, None]
    assert values[3] == pytest.approx(3.8 / 3)
    assert values[4] == pytest.approx((3.8 / 3 * 2 + 2) / 3)


def test_agregation_ticks_en_bougies_1_minute():
    closed, current = ind.aggregate_ticks(
        [(120, 1.0), (130, 2.0), (179, 0.5), (180, 1.5), (185, 1.7), (250, 1.1)]
    )
    assert closed == [ind.Candle(120, 1.0, 2.0, 0.5, 0.5), ind.Candle(180, 1.5, 1.7, 1.5, 1.7)]
    assert current == ind.Candle(240, 1.1, 1.1, 1.1, 1.1)

    aggregator = ind.CandleAggregator()
    aggregator.seed(ind.Candle(60, 5.0, 6.0, 4.0, 5.5))
    assert aggregator.add_tick(90, 7.0) is None             # même minute : la bougie amorcée continue
    assert aggregator.add_tick(125, 6.5) == ind.Candle(60, 5.0, 7.0, 4.0, 7.0)
    assert aggregator.current == ind.Candle(120, 6.5, 6.5, 6.5, 6.5)


def test_detection_de_spike():
    window = [0.02, 0.01, 0.0, 0.01, 0.02] * 20            # écart-type ≈ 0.00748 : seuil ≈ 0.0449
    assert ind.detect_spike(window, -0.05) == "down"
    assert ind.detect_spike(window, 0.05) == "up"
    assert ind.detect_spike(window, 0.04) is None
    assert ind.detect_spike(window[:99], -5.0) is None     # fenêtre incomplète
    assert ind.detect_spike([0.01] * 100, 5.0) is None     # écart-type nul

    detector = ind.SpikeDetector()
    quote = 100.0
    assert detector.update(quote) == (None, 0.0)
    for i in range(100):
        quote += WARM_UP_PATTERN[i % 5]
        assert detector.update(quote)[0] is None
    direction, change = detector.update(quote + 3.0)
    assert direction == "up" and change == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Stratégies, niveaux, utilitaires (fonctions pures)
# ---------------------------------------------------------------------------

def test_strategies_pures():
    down = [2000.0 - i for i in range(60)]
    up = [1000.0 + i for i in range(60)]
    assert se.ma_cross_trigger(down) is None and se.rsi_trigger(down) is None
    assert se.ma_cross_trigger(up) is None and se.rsi_trigger(up) is None

    bullish = down + [down[-1] + 69]
    assert se.ma_cross_trigger(bullish).direction == "BUY"
    rsi_buy = se.rsi_trigger(bullish)
    assert rsi_buy.strategy == "RSI" and rsi_buy.direction == "BUY" and "30" in rsi_buy.note

    bearish = up + [up[-1] - 69]
    assert se.ma_cross_trigger(bearish).direction == "SELL"
    assert se.rsi_trigger(bearish).direction == "SELL"

    # Rebond trop faible : le RSI reste en survente, pas de signal.
    assert se.rsi_trigger(down + [down[-1] + 0.1]) is None
    assert se.ma_cross_trigger(down[:21]) is None          # historique insuffisant

    assert se.spike_trigger("CRASH1000", "Crash 1000 Index", "down", -20.0).direction == "BUY"
    assert se.spike_trigger("BOOM1000", "Boom 1000 Index", "up", 20.0).direction == "SELL"
    assert se.spike_trigger("BOOM1000", "Boom 1000 Index", "down", -20.0) is None
    assert se.spike_trigger("CRASH500", "Crash 500 Index", "up", 20.0) is None
    assert se.spike_trigger("R_75", "Volatility 75 Index", "down", -20.0) is None


def test_niveaux_resolution_et_utilitaires():
    assert se.compute_levels("BUY", 100.0, 2.0, 2) == (100.0, 97.0, 104.0)
    assert se.compute_levels("SELL", 100.0, 2.0, 2) == (100.0, 103.0, 96.0)
    assert se.compute_levels("BUY", 100.123456, 1.23456, 3) == (100.123, 98.271, 102.592)
    assert se.compute_levels("BUY", 100.0, 0.0, 2) is None
    assert se.compute_levels("BUY", 100.0, None, 2) is None
    assert se.compute_levels("BUY", 100.0, 0.001, 2) is None   # SL confondu avec l'entrée au pip

    assert se.resolve_status("BUY", 97.0, 104.0, 104.0) == "hit_tp"
    assert se.resolve_status("BUY", 97.0, 104.0, 97.0) == "hit_sl"
    assert se.resolve_status("BUY", 97.0, 104.0, 100.0) is None
    assert se.resolve_status("SELL", 103.0, 96.0, 96.0) == "hit_tp"
    assert se.resolve_status("SELL", 103.0, 96.0, 103.5) == "hit_sl"
    assert se.resolve_status("SELL", 103.0, 96.0, 99.0) is None

    assert se.pip_decimals(4) == 4
    assert se.pip_decimals(0.0001) == 4
    assert se.pip_decimals("0.01") == 2
    assert se.pip_decimals(None) is None

    assert se.symbols_from_env("r_75, BOOM1000,,R_75") == ["R_75", "BOOM1000"]
    assert se.symbols_from_env("") == list(se.DEFAULT_SYMBOLS)


def test_hub_retire_un_client_trop_lent():
    async def scenario():
        hub = se.SignalHub(maxsize=2)
        slow = hub.register()
        fast = hub.register()
        received = []
        for i in range(3):
            hub.publish({"n": i})                # ne bloque jamais
            received.append(fast.get_nowait())
        assert received == [{"n": 0}, {"n": 1}, {"n": 2}]
        assert hub.subscriber_count == 1         # le client lent a été retiré
        assert drain(slow) == [{"n": 1}, None]   # None : sentinelle de fermeture
        hub.unregister(fast)
        assert hub.subscriber_count == 0

    run(scenario())


# ---------------------------------------------------------------------------
# Moteur complet sur faux WebSocket
# ---------------------------------------------------------------------------

def test_moteur_ma_cross_et_rsi_via_faux_ws():
    async def scenario():
        clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
        history = {"R_75": trend_candles(2000.0, -1.0), "R_100": trend_candles(1000.0, 1.0)}
        engine, fake, _ = await start_engine(["R_75", "R_100"], history, clock)
        queue = engine.register()
        try:
            # 1er tick de la minute suivante : clôt la dernière bougie d'historique (tendance intacte).
            fake.tick("R_75", 1881.0, LIVE_MINUTE)
            fake.tick("R_100", 1119.0, LIVE_MINUTE)
            fake.tick("R_75", 1950.0, LIVE_MINUTE + 30)
            fake.tick("R_100", 1050.0, LIVE_MINUTE + 30)
            await fake.settle()
            assert drain(queue) == []
            # Clôture de la bougie de retournement : croisement EMA et sortie de zone RSI.
            fake.tick("R_75", 1950.5, LIVE_MINUTE + 60)
            fake.tick("R_100", 1049.5, LIVE_MINUTE + 60)
            await fake.settle()
            return drain(queue)
        finally:
            await engine.stop()

    messages = run(scenario())
    assert all(message["type"] == "signal" for message in messages)
    signals = {(m["signal"]["symbol"], m["signal"]["strategy"]): m["signal"] for m in messages}
    assert set(signals) == {("R_75", "MA_CROSS"), ("R_75", "RSI"), ("R_100", "MA_CROSS"), ("R_100", "RSI")}
    assert signals[("R_75", "MA_CROSS")]["direction"] == "BUY"
    assert signals[("R_75", "RSI")]["direction"] == "BUY"
    assert signals[("R_100", "MA_CROSS")]["direction"] == "SELL"
    assert signals[("R_100", "RSI")]["direction"] == "SELL"

    # ATR 14 de Wilder : ATR précédent 2, puis bougie de retournement de TR 69.
    atr = (13 * 2 + 69) / 14
    buy = signals[("R_75", "MA_CROSS")]
    assert buy["entry"] == 1950.5
    assert buy["stop_loss"] == pytest.approx(1950.5 - 1.5 * atr, abs=1e-4)
    assert buy["take_profit"] == pytest.approx(1950.5 + 2 * atr, abs=1e-4)
    sell = signals[("R_100", "MA_CROSS")]
    assert sell["entry"] == 1049.5
    assert sell["stop_loss"] == pytest.approx(1049.5 + 1.5 * atr, abs=1e-4)
    assert sell["take_profit"] == pytest.approx(1049.5 - 2 * atr, abs=1e-4)

    assert set(buy) == SIGNAL_KEYS
    assert buy["symbol_name"] == "Volatility 75 Index"
    assert buy["timeframe"] == "1m" and buy["status"] == "active"
    assert buy["closed_at"] is None and buy["close_price"] is None
    assert buy["note"]
    created = datetime.fromisoformat(buy["created_at"])
    assert created == datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    assert datetime.fromisoformat(buy["expires_at"]) - created == timedelta(minutes=15)
    assert load_signal(buy["id"]) == buy                  # persisté tel que diffusé


def test_moteur_spike_boom_crash_via_faux_ws():
    async def scenario():
        clock = FakeClock(datetime(2030, 1, 2, 12, 0, tzinfo=UTC))
        history = {symbol: trend_candles(1000.0, -1.0) for symbol in ("BOOM1000", "CRASH1000", "R_75")}
        engine, fake, _ = await start_engine(list(history), history, clock)
        queue = engine.register()
        try:
            quotes = {}
            for symbol in history:
                fake.tick(symbol, 881.0, LIVE_MINUTE)
                quotes[symbol] = warm_up(fake, symbol, 881.0)
            await fake.settle()
            assert drain(queue) == []

            fake.tick("BOOM1000", round(quotes["BOOM1000"] - 20, 4), LIVE_MINUTE + 2)  # spike baissier : ignoré sur Boom
            fake.tick("R_75", round(quotes["R_75"] - 20, 4), LIVE_MINUTE + 2)          # pas de SPIKE hors Boom/Crash
            await fake.settle()
            assert drain(queue) == []

            boom_quote = round(quotes["BOOM1000"] + 5, 4)       # +25 : spike haussier
            crash_quote = round(quotes["CRASH1000"] - 20, 4)    # -20 : spike baissier
            fake.tick("BOOM1000", boom_quote, LIVE_MINUTE + 3)
            fake.tick("CRASH1000", crash_quote, LIVE_MINUTE + 3)
            await fake.settle()
            return drain(queue), boom_quote, crash_quote
        finally:
            await engine.stop()

    messages, boom_quote, crash_quote = run(scenario())
    assert all(m["type"] == "signal" and m["signal"]["strategy"] == "SPIKE" for m in messages)
    signals = {m["signal"]["symbol"]: m["signal"] for m in messages}
    assert set(signals) == {"BOOM1000", "CRASH1000"}

    boom, crash = signals["BOOM1000"], signals["CRASH1000"]
    # ATR des bougies 1 min d'historique = 2 : SL à 1.5 x 2, TP à 2 x 2.
    assert boom["direction"] == "SELL" and boom["entry"] == boom_quote
    assert boom["stop_loss"] == pytest.approx(boom_quote + 3.0)
    assert boom["take_profit"] == pytest.approx(boom_quote - 4.0)
    assert crash["direction"] == "BUY" and crash["entry"] == crash_quote
    assert crash["stop_loss"] == pytest.approx(crash_quote - 3.0)
    assert crash["take_profit"] == pytest.approx(crash_quote + 4.0)
    assert "Crash 1000 Index" in crash["note"] and "baissier" in crash["note"]
    assert "haussier" in boom["note"]


def test_cooldown_par_symbole_et_strategie():
    async def scenario():
        clock = FakeClock(datetime(2030, 1, 3, 12, 0, tzinfo=UTC))
        engine, fake, _ = await start_engine(["CRASH1000"], {"CRASH1000": trend_candles(1000.0, -1.0)}, clock)
        queue = engine.register()
        try:
            fake.tick("CRASH1000", 881.0, LIVE_MINUTE)
            quote = warm_up(fake, "CRASH1000", 881.0)
            fake.tick("CRASH1000", round(quote - 20, 4), LIVE_MINUTE + 2)
            await fake.settle()
            first = [m for m in drain(queue) if m["type"] == "signal"]

            clock.advance(minutes=9, seconds=59)
            fake.tick("CRASH1000", round(quote - 50, 4), LIVE_MINUTE + 3)    # nouveau spike, en cooldown
            await fake.settle()
            during = [m for m in drain(queue) if m["type"] == "signal"]

            clock.advance(seconds=1)                                         # 10 minutes pile
            fake.tick("CRASH1000", round(quote - 110, 4), LIVE_MINUTE + 4)
            await fake.settle()
            after = [m for m in drain(queue) if m["type"] == "signal"]
            return first, during, after
        finally:
            await engine.stop()

    first, during, after = run(scenario())
    assert len(first) == 1 and first[0]["signal"]["strategy"] == "SPIKE"
    assert during == []
    assert len(after) == 1 and after[0]["signal"]["id"] != first[0]["signal"]["id"]


def test_resolution_tp_sl_expiration_et_persistance():
    start = datetime(2030, 1, 4, 12, 0, tzinfo=UTC)

    async def scenario():
        clock = FakeClock(start)
        symbols = ["CRASH1000", "BOOM1000", "CRASH500", "BOOM500"]
        engine, fake, _ = await start_engine(
            symbols, {symbol: trend_candles(1000.0, -1.0) for symbol in symbols}, clock
        )
        queue = engine.register()
        try:
            quotes = {}
            for symbol in symbols:
                fake.tick(symbol, 881.0, LIVE_MINUTE)
                quotes[symbol] = warm_up(fake, symbol, 881.0)

            # CRASH1000 : BUY (SL -3, TP +4) ; BOOM1000 : SELL (SL +3, TP -4).
            crash_entry = round(quotes["CRASH1000"] - 20, 4)
            boom_entry = round(quotes["BOOM1000"] + 20, 4)
            fake.tick("CRASH1000", crash_entry, LIVE_MINUTE + 2)
            fake.tick("BOOM1000", boom_entry, LIVE_MINUTE + 2)
            await fake.settle()
            opened = {m["signal"]["symbol"]: m["signal"] for m in drain(queue)}

            clock.advance(minutes=1)
            fake.tick("CRASH1000", round(crash_entry + 3.9, 4), LIVE_MINUTE + 3)  # TP pas encore atteint
            await fake.settle()
            assert drain(queue) == []
            fake.tick("CRASH1000", round(crash_entry + 4.0, 4), LIVE_MINUTE + 3)  # TP
            fake.tick("BOOM1000", round(boom_entry + 3.0, 4), LIVE_MINUTE + 3)    # SL
            await fake.settle()
            resolved = {m["signal"]["symbol"]: m for m in drain(queue)}

            # Deux signaux de plus, laissés sans atteindre leurs niveaux.
            crash5_entry = round(quotes["CRASH500"] - 20, 4)
            boom5_entry = round(quotes["BOOM500"] + 20, 4)
            fake.tick("CRASH500", crash5_entry, LIVE_MINUTE + 4)
            fake.tick("BOOM500", boom5_entry, LIVE_MINUTE + 4)
            fake.tick("BOOM500", round(boom5_entry - 0.5, 4), LIVE_MINUTE + 5)   # dernier cours connu
            await fake.settle()
            pending = {m["signal"]["symbol"]: m["signal"] for m in drain(queue)}

            clock.advance(minutes=15)
            # Tick postérieur à l'échéance : il atteindrait le TP mais ne compte plus.
            fake.tick("CRASH500", round(crash5_entry + 10, 4), LIVE_MINUTE + 6)
            await fake.settle()
            expired_by_tick = drain(queue)
            swept = engine.check_expirations()
            expired_by_sweep = drain(queue)
            return (opened, resolved, pending, expired_by_tick, swept, expired_by_sweep,
                    crash_entry, boom_entry, crash5_entry, boom5_entry)
        finally:
            await engine.stop()

    (opened, resolved, pending, expired_by_tick, swept, expired_by_sweep,
     crash_entry, boom_entry, crash5_entry, boom5_entry) = run(scenario())

    assert set(opened) == {"CRASH1000", "BOOM1000"}
    tp = resolved["CRASH1000"]
    assert tp["type"] == "update" and tp["signal"]["status"] == "hit_tp"
    assert tp["signal"]["close_price"] == round(crash_entry + 4.0, 4)
    assert datetime.fromisoformat(tp["signal"]["closed_at"]) == start + timedelta(minutes=1)
    sl = resolved["BOOM1000"]
    assert sl["type"] == "update" and sl["signal"]["status"] == "hit_sl"
    assert sl["signal"]["close_price"] == round(boom_entry + 3.0, 4)

    assert set(pending) == {"CRASH500", "BOOM500"}
    assert len(expired_by_tick) == 1
    late = expired_by_tick[0]["signal"]
    assert late["symbol"] == "CRASH500" and late["status"] == "expired"
    assert late["close_price"] == crash5_entry           # dernier cours AVANT l'échéance
    assert late["closed_at"] == late["expires_at"]

    assert swept == 1
    swept_signal = expired_by_sweep[0]["signal"]
    assert swept_signal["symbol"] == "BOOM500" and swept_signal["status"] == "expired"
    assert swept_signal["close_price"] == round(boom5_entry - 0.5, 4)

    # Persistance : la base reflète chaque changement diffusé.
    assert load_signal(tp["signal"]["id"]) == tp["signal"]
    assert load_signal(sl["signal"]["id"]) == sl["signal"]
    assert load_signal(late["id"]) == late
    assert load_signal(swept_signal["id"]) == swept_signal


def test_demarrage_expire_les_restes_et_reconnexion():
    leftover = insert_signal(status="active", created_at=datetime(2030, 1, 5, 11, 0, tzinfo=UTC))

    async def scenario():
        clock = FakeClock(datetime(2030, 1, 5, 12, 0, tzinfo=UTC))
        history = {"R_75": trend_candles(2000.0, -1.0)}
        second = FakeDerivWs(history)
        engine, first, urls = await start_engine(["R_75", "FAKE_X"], history, clock, second)
        try:
            assert load_signal(leftover)["status"] == "expired"
            # Historique en erreur pour FAKE_X : consigné, le moteur continue.
            assert first.subscribed == {"R_75", "FAKE_X"}
            history_requests = [m for m in first.sent if m.get("ticks_history") == "R_75"]
            assert history_requests and history_requests[0]["style"] == "candles"
            assert history_requests[0]["count"] == 120 and "subscribe" not in history_requests[0]

            first.drop()
            await second.settle()
            assert first.closed
            assert any("active_symbols" in m for m in second.sent)
            assert second.subscribed == {"R_75", "FAKE_X"}
            assert engine.connected
            return urls
        finally:
            await engine.stop()

    urls = run(scenario())
    assert urls == [se.PUBLIC_WS_URL, se.PUBLIC_WS_URL]


def test_stats_et_recent():
    now = datetime(2040, 1, 10, 12, 0, tzinfo=UTC)
    insert_signal(symbol="R_75", strategy="MA_CROSS", status="hit_tp", created_at=now - timedelta(days=1))
    insert_signal(symbol="R_75", strategy="MA_CROSS", status="hit_sl", created_at=now - timedelta(days=2))
    insert_signal(symbol="R_75", strategy="RSI", status="hit_tp", created_at=now - timedelta(days=2))
    insert_signal(symbol="CRASH500", strategy="SPIKE", status="expired", created_at=now - timedelta(days=3))
    active_id = insert_signal(symbol="R_75", strategy="RSI", status="active", created_at=now - timedelta(hours=1))
    insert_signal(symbol="R_75", strategy="MA_CROSS", status="hit_tp", created_at=now - timedelta(days=40))

    engine = se.SignalEngine(database.SessionLocal, ["R_75", "BOOM1000"], clock=lambda: now)
    stats = engine.stats(7)
    assert stats["window_days"] == 7
    assert stats["overall"] == {"total": 5, "hit_tp": 2, "hit_sl": 1, "expired": 1, "win_rate": 0.6667}
    by_strategy = {row["strategy"]: row for row in stats["by_strategy"]}
    assert by_strategy["MA_CROSS"] == {"strategy": "MA_CROSS", "total": 2, "hit_tp": 1, "hit_sl": 1,
                                       "expired": 0, "win_rate": 0.5}
    assert by_strategy["RSI"]["total"] == 2 and by_strategy["RSI"]["win_rate"] == 1.0
    assert by_strategy["SPIKE"] == {"strategy": "SPIKE", "total": 1, "hit_tp": 0, "hit_sl": 0,
                                    "expired": 1, "win_rate": None}
    by_symbol = {row["symbol"]: row for row in stats["by_symbol"]}
    assert by_symbol["R_75"]["total"] == 4 and by_symbol["R_75"]["win_rate"] == 0.6667
    assert by_symbol["BOOM1000"] == {"symbol": "BOOM1000", "total": 0, "hit_tp": 0, "hit_sl": 0,
                                     "expired": 0, "win_rate": None}
    assert by_symbol["CRASH500"]["expired"] == 1
    assert engine.stats(60)["overall"]["total"] == 6
    assert engine.stats(0)["window_days"] == 1

    recent = engine.recent(200)
    assert recent[0]["id"] == active_id                   # plus récent en premier
    assert all(signal["status"] != "active" for signal in engine.recent(200, closed_only=True))


# ---------------------------------------------------------------------------
# REST et WebSocket (routeur monté sur une FastAPI de test)
# ---------------------------------------------------------------------------

def _live_users(make_user):
    now = datetime.now(UTC)
    return {
        "essai": make_user(trial_started_at=now),
        "premium": make_user(subscription_tier="premium", subscription_expires_at=now + timedelta(days=30)),
        "premium_a_vie": make_user(subscription_tier="premium"),
        "admin": make_user(role="admin"),
    }


def _free_users(make_user):
    now = datetime.now(UTC)
    return {
        "gratuit": make_user(),
        "premium_expire": make_user(subscription_tier="premium", subscription_expires_at=now - timedelta(days=1)),
    }


def test_rest_signaux_filtre_live(make_user, auth_headers, make_app):
    active_id = insert_signal(status="active")
    closed_id = insert_signal(status="hit_tp")
    engine = se.SignalEngine(database.SessionLocal, ["R_75"])
    client = TestClient(make_app(signals_router.router, signal_engine=engine))

    for label, user in _live_users(make_user).items():
        body = client.get("/signals?limit=200", headers=auth_headers(user)).json()
        assert body["live_access"] is True, label
        assert {active_id, closed_id} <= {signal["id"] for signal in body["signals"]}, label

    for label, user in _free_users(make_user).items():
        response = client.get("/signals", params={"limit": 200}, headers=auth_headers(user))
        assert response.status_code == 200
        body = response.json()
        assert body["live_access"] is False, label
        ids = {signal["id"] for signal in body["signals"]}
        assert closed_id in ids and active_id not in ids, label
        assert all(signal["status"] != "active" for signal in body["signals"])

    closed = next(signal for signal in body["signals"] if signal["id"] == closed_id)
    assert set(closed) == SIGNAL_KEYS
    assert closed["status"] == "hit_tp" and closed["close_price"] == 104.0
    assert closed["closed_at"] is not None

    headers = auth_headers(make_user())
    assert client.get("/signals").status_code == 401
    assert client.get("/signals?limit=0", headers=headers).status_code == 422
    stats = client.get("/signals/stats?days=7", headers=headers).json()
    assert set(stats) == {"window_days", "overall", "by_strategy", "by_symbol"}
    assert stats["window_days"] == 7 and set(stats["overall"]) == STATS_KEYS
    assert {row["strategy"] for row in stats["by_strategy"]} == {"MA_CROSS", "RSI", "SPIKE"}
    assert all(set(row) == STATS_KEYS | {"symbol"} for row in stats["by_symbol"])
    assert client.get("/signals/stats").status_code == 401


def test_rest_sans_moteur(make_user, auth_headers, make_app):
    client = TestClient(make_app(signals_router.router, signal_engine=None))
    live = auth_headers(make_user(trial_started_at=datetime.now(UTC)))
    free = auth_headers(make_user())
    assert client.get("/signals", headers=live).json() == {"live_access": True, "signals": []}
    assert client.get("/signals", headers=free).json() == {"live_access": False, "signals": []}
    assert client.get("/signals/stats?days=3", headers=free).json() == {
        "window_days": 3,
        "overall": {"total": 0, "hit_tp": 0, "hit_sl": 0, "expired": 0, "win_rate": None},
        "by_strategy": [],
        "by_symbol": [],
    }


SAMPLE_ACTIVE = {
    "id": 1, "symbol": "R_75", "symbol_name": "Volatility 75 Index", "strategy": "RSI",
    "direction": "BUY", "entry": 100.0, "stop_loss": 97.0, "take_profit": 104.0, "timeframe": "1m",
    "created_at": "2030-01-01T12:00:00+00:00", "expires_at": "2030-01-01T12:15:00+00:00",
    "status": "active", "closed_at": None, "close_price": None, "note": "Signal de test.",
}
SAMPLE_CLOSED = {**SAMPLE_ACTIVE, "status": "hit_tp", "closed_at": "2030-01-01T12:05:00+00:00",
                 "close_price": 104.0}


def _authenticate(ws, user) -> dict:
    ws.send_json({"type": "auth", "token": auth.create_access_token(user)})
    assert ws.receive_json()["type"] == "auth_ok"
    return ws.receive_json()


def test_ws_live_recoit_signaux_et_mises_a_jour(make_user, make_app):
    engine = se.SignalEngine(database.SessionLocal, ["R_75"])
    app = make_app(signals_router.router, signal_engine=engine)
    user = make_user(trial_started_at=datetime.now(UTC))
    with TestClient(app) as client:
        with client.websocket_connect("/ws/signals") as ws:
            assert _authenticate(ws, user) == {"type": "hello", "live_access": True}
            assert engine.hub.subscriber_count == 1
            client.portal.call(engine.hub.publish, {"type": "signal", "signal": SAMPLE_ACTIVE})
            assert ws.receive_json() == {"type": "signal", "signal": SAMPLE_ACTIVE}
            client.portal.call(engine.hub.publish, {"type": "update", "signal": SAMPLE_CLOSED})
            assert ws.receive_json() == {"type": "update", "signal": SAMPLE_CLOSED}
        wait_until(lambda: engine.hub.subscriber_count == 0)   # désinscrit à la déconnexion


def test_ws_non_live_ne_recoit_que_les_signaux_clos(make_user, make_app):
    engine = se.SignalEngine(database.SessionLocal, ["R_75"])
    app = make_app(signals_router.router, signal_engine=engine)
    user = make_user()

    def burst() -> None:
        engine.hub.publish({"type": "signal", "signal": SAMPLE_ACTIVE})   # filtré
        engine.hub.publish({"type": "update", "signal": SAMPLE_ACTIVE})   # encore actif : filtré
        engine.hub.publish({"type": "update", "signal": SAMPLE_CLOSED})

    with TestClient(app) as client:
        with client.websocket_connect("/ws/signals") as ws:
            assert _authenticate(ws, user) == {"type": "hello", "live_access": False}
            client.portal.call(burst)
            assert ws.receive_json() == {"type": "update", "signal": SAMPLE_CLOSED}
        wait_until(lambda: engine.hub.subscriber_count == 0)


def test_ws_client_trop_lent_deconnecte(make_user, make_app):
    engine = se.SignalEngine(database.SessionLocal, ["R_75"], queue_maxsize=1)
    app = make_app(signals_router.router, signal_engine=engine)
    user = make_user(trial_started_at=datetime.now(UTC))

    def burst() -> None:
        for i in range(3):
            engine.hub.publish({"type": "update", "signal": {**SAMPLE_CLOSED, "id": i}})

    with TestClient(app) as client:
        with client.websocket_connect("/ws/signals") as ws:
            _authenticate(ws, user)
            client.portal.call(burst)
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == signals_router.WS_CLOSE_TOO_SLOW
    assert engine.hub.subscriber_count == 0


def test_ws_sans_moteur_et_token_invalide(make_user, make_app):
    app = make_app(signals_router.router, signal_engine=None)
    user = make_user(role="admin")
    with TestClient(app) as client:
        with client.websocket_connect("/ws/signals") as ws:
            assert _authenticate(ws, user) == {"type": "hello", "live_access": True}
        with client.websocket_connect("/ws/signals") as ws:
            ws.send_json({"type": "auth", "token": "jeton-invalide"})
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 4401
