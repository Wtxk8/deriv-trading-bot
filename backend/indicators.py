"""Indicateurs techniques purs (sans I/O) utilisés par le moteur de signaux.

Conventions :
- les séries renvoyées ont la même longueur que l'entrée ; les positions où
  l'indicateur n'est pas encore défini valent None ;
- RSI et ATR suivent le lissage de Wilder, avec la convention TA-Lib : la
  première valeur est disponible à l'index `period` (il faut une valeur
  précédente pour calculer une variation ou un True Range).

Rappel produit : les indices synthétiques Deriv sont générés aléatoirement.
Ces indicateurs décrivent le passé ; ils ne prédisent rien de fiable.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Optional

CANDLE_GRANULARITY_SECONDS = 60


@dataclass(slots=True)
class Candle:
    """Bougie OHLC ; `epoch` = début de la bougie (secondes UTC), comme chez Deriv."""

    epoch: int
    open: float
    high: float
    low: float
    close: float


# ---------------------------------------------------------------------------
# Moyennes mobiles
# ---------------------------------------------------------------------------

def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError("period doit être >= 1")


def sma(values: Sequence[float], period: int) -> list[Optional[float]]:
    """Moyenne mobile simple."""
    _check_period(period)
    out: list[Optional[float]] = [None] * len(values)
    window_sum = 0.0
    for i, value in enumerate(values):
        window_sum += value
        if i >= period:
            window_sum -= values[i - period]
        if i >= period - 1:
            out[i] = window_sum / period
    return out


def ema(values: Sequence[float], period: int) -> list[Optional[float]]:
    """Moyenne mobile exponentielle (alpha = 2 / (period + 1)).

    Amorcée par la SMA des `period` premières valeurs (première valeur à
    l'index period - 1).
    """
    _check_period(period)
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    current = sum(values[:period]) / period
    out[period - 1] = current
    for i in range(period, len(values)):
        current = current + alpha * (values[i] - current)
        out[i] = current
    return out


# ---------------------------------------------------------------------------
# RSI et ATR (Wilder)
# ---------------------------------------------------------------------------

def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 50.0 if avg_gain == 0.0 else 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi(values: Sequence[float], period: int = 14) -> list[Optional[float]]:
    """RSI de Wilder. Première valeur à l'index `period` (il faut period + 1 cours).

    Marché parfaitement plat (ni hausse ni baisse) : 50 par convention.
    """
    _check_period(period)
    out: list[Optional[float]] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change > 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from_averages(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from_averages(avg_gain, avg_loss)
    return out


def true_range(candle: Candle, previous_close: Optional[float]) -> float:
    """True Range : max(H - L, |H - clôture préc.|, |L - clôture préc.|)."""
    high_low = candle.high - candle.low
    if previous_close is None:
        return high_low
    return max(high_low, abs(candle.high - previous_close), abs(candle.low - previous_close))


def atr(candles: Sequence[Candle], period: int = 14) -> list[Optional[float]]:
    """Average True Range de Wilder.

    Le True Range n'est défini qu'à partir de la 2e bougie ; première ATR à
    l'index `period` = moyenne des TR 1..period, puis lissage de Wilder.
    """
    _check_period(period)
    out: list[Optional[float]] = [None] * len(candles)
    if len(candles) <= period:
        return out
    ranges = [true_range(candles[i], candles[i - 1].close) for i in range(1, len(candles))]
    current = sum(ranges[:period]) / period
    out[period] = current
    for i in range(period + 1, len(candles)):
        current = (current * (period - 1) + ranges[i - 1]) / period
        out[i] = current
    return out


# ---------------------------------------------------------------------------
# Agrégation ticks -> bougies 1 minute
# ---------------------------------------------------------------------------

def candle_start(epoch: int, granularity: int = CANDLE_GRANULARITY_SECONDS) -> int:
    """Début (epoch) de la bougie contenant `epoch`."""
    return int(epoch) - int(epoch) % granularity


class CandleAggregator:
    """Construit des bougies à partir des ticks.

    La bougie en cours est close dès qu'un tick tombe dans une minute
    ultérieure ; ce tick ouvre alors la bougie suivante.
    """

    def __init__(self, granularity: int = CANDLE_GRANULARITY_SECONDS) -> None:
        self.granularity = granularity
        self.current: Optional[Candle] = None

    def seed(self, candle: Optional[Candle]) -> None:
        """Reprend une bougie (éventuellement incomplète) comme bougie en cours."""
        self.current = None if candle is None else Candle(
            candle.epoch, candle.open, candle.high, candle.low, candle.close
        )

    def add_tick(self, epoch: int, quote: float) -> Optional[Candle]:
        """Intègre un tick ; renvoie la bougie qui vient de se clore, sinon None."""
        start = candle_start(epoch, self.granularity)
        current = self.current
        if current is None:
            self.current = Candle(start, quote, quote, quote, quote)
            return None
        if start <= current.epoch:
            # Même minute (ou tick en retard) : mise à jour de la bougie en cours.
            current.high = max(current.high, quote)
            current.low = min(current.low, quote)
            current.close = quote
            return None
        self.current = Candle(start, quote, quote, quote, quote)
        return current


def aggregate_ticks(
    ticks: Iterable[tuple[int, float]], granularity: int = CANDLE_GRANULARITY_SECONDS
) -> tuple[list[Candle], Optional[Candle]]:
    """Version pure : (epoch, cours)* -> (bougies closes, bougie en cours)."""
    aggregator = CandleAggregator(granularity)
    closed: list[Candle] = []
    for epoch, quote in ticks:
        candle = aggregator.add_tick(epoch, quote)
        if candle is not None:
            closed.append(candle)
    return closed, aggregator.current


# ---------------------------------------------------------------------------
# Détection de spike (Boom / Crash)
# ---------------------------------------------------------------------------

SPIKE_K = 6.0
SPIKE_WINDOW = 100


def detect_spike(
    recent_abs_changes: Sequence[float],
    change: float,
    k: float = SPIKE_K,
    window: int = SPIKE_WINDOW,
) -> Optional[str]:
    """Renvoie "up" / "down" si |change| > k x écart-type des `window` dernières
    variations absolues (hors variation courante), sinon None.

    Il faut une fenêtre complète ; une fenêtre d'écart-type nul (série
    dégénérée) ne déclenche jamais.
    """
    if len(recent_abs_changes) < window or change == 0:
        return None
    sample = list(recent_abs_changes)[-window:]
    mean = sum(sample) / window
    std = math.sqrt(sum((x - mean) ** 2 for x in sample) / window)
    if std <= 0.0 or abs(change) <= k * std:
        return None
    return "up" if change > 0 else "down"


class SpikeDetector:
    """Détecteur incrémental : alimenté tick par tick."""

    def __init__(self, k: float = SPIKE_K, window: int = SPIKE_WINDOW) -> None:
        self.k = k
        self.window = window
        self._abs_changes: deque[float] = deque(maxlen=window)
        self._last_quote: Optional[float] = None

    def reset(self) -> None:
        self._abs_changes.clear()
        self._last_quote = None

    def update(self, quote: float) -> tuple[Optional[str], float]:
        """Renvoie (direction du spike ou None, variation tick-à-tick)."""
        if self._last_quote is None:
            self._last_quote = quote
            return None, 0.0
        change = quote - self._last_quote
        self._last_quote = quote
        direction = detect_spike(self._abs_changes, change, self.k, self.window)
        self._abs_changes.append(abs(change))
        return direction, change
