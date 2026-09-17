"""Moteur de bot de trading asynchrone pour indices synthétiques Deriv.

Architecture:
- `BotState`     : machine à états explicite.
- `RiskManager`  : PnL cumulé + arrêt strict côté serveur (SL/TP journaliers).
- `BotEngine`    : boucle d'exécution non-bloquante (asyncio.Task) pilotant une
                   stratégie simple (Rise/Fall ou Over/Under) via `DerivClient`.

Un moteur porte UNE session d'UN utilisateur sur UN type de compte (démo ou
réel) : `bot_manager.BotManager` crée un moteur neuf par session et par
utilisateur.

Toute la logique de risque est calculée côté serveur : dès que le seuil de perte
ou de gain est franchi, la boucle est interrompue immédiatement.

Événements émis (voir `trade_events`) : SessionEvent "started" puis "stopped"
(une seule fois par session), TradeOpenedEvent après chaque achat réussi,
TradeSettledEvent à chaque règlement. Ils sont livrés dans l'ordre d'émission
par des tâches asyncio : un abonné lent ou en erreur ne bloque ni n'interrompt
jamais la boucle de trading.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from deriv_client import DerivClient, DerivError
from trade_events import (
    SessionEvent,
    SessionListener,
    TradeOpenedEvent,
    TradeOpenedListener,
    TradeSettledEvent,
    TradeSettledListener,
)

logger = logging.getLogger("bot_engine")

# Types de compte Deriv acceptés.
ACCOUNT_TYPES: frozenset[str] = frozenset({"demo", "real"})

# Tolérance flottante du contrôle « mise <= budget de perte restant ».
_RISK_EPSILON = 1e-9

# Livraisons d'événements en cours. asyncio ne garde qu'une référence faible sur
# les tâches : sans ce registre, une livraison pourrait être collectée avant sa
# fin, notamment après la libération du moteur qui l'a émise.
_PENDING_DELIVERIES: set[asyncio.Task[None]] = set()


def normalize_account_type(account_type: str) -> str:
    """Normalise un type de compte Deriv ("demo" | "real") ; ValueError sinon."""
    value = str(account_type or "").strip().lower()
    if value not in ACCOUNT_TYPES:
        raise ValueError(
            f"account_type invalide : {account_type!r} (attendu : demo | real)"
        )
    return value


class BotState(str, Enum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOP_LOSS_REACHED = "STOP_LOSS_REACHED"
    TAKE_PROFIT_REACHED = "TAKE_PROFIT_REACHED"
    ERROR = "ERROR"


class StrategyType(str, Enum):
    RISE_FALL = "RISE_FALL"
    OVER_UNDER = "OVER_UNDER"
    MARTINGALE = "MARTINGALE"  # Rise/Fall + doublement de mise après perte


# États terminaux : la boucle ne doit plus ouvrir de nouveau trade.
_TERMINAL_STATES: frozenset[BotState] = frozenset(
    {
        BotState.STOPPED,
        BotState.STOP_LOSS_REACHED,
        BotState.TAKE_PROFIT_REACHED,
        BotState.ERROR,
    }
)


@dataclass(slots=True)
class TradeRecord:
    """Trace immuable d'un trade exécuté et réglé."""

    contract_id: int
    contract_type: str
    symbol: str
    stake: float
    payout: float
    profit: float
    result: str  # "won" | "lost"
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "contract_type": self.contract_type,
            "symbol": self.symbol,
            "stake": round(self.stake, 2),
            "payout": round(self.payout, 2),
            "profit": round(self.profit, 2),
            "result": self.result,
            "timestamp": self.timestamp,
        }


@dataclass(slots=True)
class RiskManager:
    """Suivi du PnL de session et évaluation stricte des seuils SL/TP."""

    stop_loss: float
    take_profit: float
    pnl: float = 0.0
    trades_won: int = 0
    trades_lost: int = 0

    @property
    def trades_total(self) -> int:
        return self.trades_won + self.trades_lost

    def register(self, profit: float) -> None:
        self.pnl += profit
        if profit > 0:
            self.trades_won += 1
        else:
            self.trades_lost += 1

    def remaining_loss_budget(self) -> float:
        """Perte encore permise avant le stop loss : stop_loss + PnL de session."""
        return abs(self.stop_loss) + self.pnl

    def stake_exceeds_budget(self, stake: float) -> bool:
        """Vrai si perdre `stake` ferait passer le PnL sous -stop_loss.

        Une mise égale au budget restant reste autorisée (tolérance flottante).
        """
        return stake > self.remaining_loss_budget() + _RISK_EPSILON

    def breached_state(self) -> Optional[BotState]:
        """Retourne l'état terminal si un seuil est franchi, sinon None."""
        if self.pnl <= -abs(self.stop_loss):
            return BotState.STOP_LOSS_REACHED
        if self.pnl >= abs(self.take_profit):
            return BotState.TAKE_PROFIT_REACHED
        return None


async def _deliver(
    listener: Callable[[Any], Awaitable[None]],
    event: Any,
    previous: Optional[asyncio.Task[None]],
) -> None:
    """Livre `event` une fois la livraison précédente terminée ; n'échoue jamais."""
    if previous is not None and not previous.done():
        await asyncio.wait({previous})
    try:
        await listener(event)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Abonné en erreur sur %s (user=%s) : événement ignoré",
            type(event).__name__,
            getattr(event, "user_id", None),
        )


class BotEngine:
    """Moteur de trading : une session active à la fois par instance."""

    def __init__(
        self,
        app_id: Optional[str] = None,
        rest_base_url: str = "https://api.derivws.com",
        min_ticks: int = 10,
        trade_duration: int = 5,
        trade_cooldown: float = 1.0,
        trade_timeout: float = 120.0,
        max_history: int = 5,
        *,
        user_id: Optional[int] = None,
        account_type: str = "demo",
        on_trade_opened: Optional[TradeOpenedListener] = None,
        on_trade_settled: Optional[TradeSettledListener] = None,
        on_session: Optional[SessionListener] = None,
    ) -> None:
        # app_id = Deriv-App-ID enregistré via api.deriv.com dashboard (PAT app).
        self._app_id: str = app_id or os.environ.get("DERIV_APP_ID", "")
        self._rest_base_url: str = rest_base_url
        self._min_ticks: int = min_ticks
        self._trade_duration: int = trade_duration
        self._trade_cooldown: float = trade_cooldown
        self._trade_timeout: float = trade_timeout

        # Propriétaire de la session et type de compte Deriv à utiliser.
        self._user_id: Optional[int] = user_id
        self._account_type: str = normalize_account_type(account_type)

        # Abonnés aux événements (voir trade_events), appelés sans bloquer.
        self._on_trade_opened: Optional[TradeOpenedListener] = on_trade_opened
        self._on_trade_settled: Optional[TradeSettledListener] = on_trade_settled
        self._on_session: Optional[SessionListener] = on_session
        # Dernière livraison planifiée : la suivante l'attend (ordre garanti).
        self._last_delivery: Optional[asyncio.Task[None]] = None
        # Vrai entre l'émission de "started" et celle de "stopped".
        self._session_open: bool = False

        self._client: Optional[DerivClient] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._lock: asyncio.Lock = asyncio.Lock()

        # État configurable de session.
        self._state: BotState = BotState.STOPPED
        self._symbol: str = ""
        self._stake: float = 0.0
        self._currency: str = "USD"
        self._strategy: StrategyType = StrategyType.RISE_FALL
        self._risk: RiskManager = RiskManager(stop_loss=0.0, take_profit=0.0)
        self._error_message: Optional[str] = None

        # Solde compte (au démarrage) pour affichage mobile.
        self._start_balance: float = 0.0

        # Buffers de marché et de trades.
        self._ticks: deque[float] = deque(maxlen=200)
        self._pip_size: int = 2
        self._trades: deque[TradeRecord] = deque(maxlen=max_history)

        # État Martingale : mise en cours (doublée après chaque perte, reset après gain).
        self._base_stake: float = 0.0
        self._current_stake: float = 0.0
        self._martingale_factor: float = 2.0
        self._martingale_max_stake: float = 128.0

        # Synchronisation du règlement de contrat.
        self._current_contract_id: int = 0
        self._settlement: Optional[asyncio.Future[dict[str, Any]]] = None

    # ------------------------------------------------------------------
    # Propriétés
    # ------------------------------------------------------------------
    @property
    def user_id(self) -> Optional[int]:
        return self._user_id

    @property
    def account_type(self) -> str:
        return self._account_type

    # ------------------------------------------------------------------
    # API publique de contrôle
    # ------------------------------------------------------------------
    async def start(
        self,
        api_token: str,
        symbol: str,
        stake: float,
        stop_loss: float,
        take_profit: float,
        strategy_type: StrategyType | str = StrategyType.RISE_FALL,
    ) -> None:
        """Démarre une session de trading. Refuse si une session est active."""
        async with self._lock:
            if self._state in (BotState.RUNNING, BotState.PAUSED):
                raise RuntimeError("Bot déjà en cours d'exécution")
            # Réutilisation après une session terminée : libère l'ancienne connexion.
            await self._teardown()

            self._symbol = symbol
            self._stake = float(stake)
            self._base_stake = float(stake)
            self._current_stake = float(stake)
            self._strategy = StrategyType(strategy_type)
            self._risk = RiskManager(
                stop_loss=abs(float(stop_loss)),
                take_profit=abs(float(take_profit)),
            )
            self._error_message = None
            self._ticks.clear()
            self._trades.clear()
            self._current_contract_id = 0

            if not self._app_id:
                raise RuntimeError(
                    "DERIV_APP_ID non configuré côté serveur — enregistrer une "
                    "app PAT sur api.deriv.com et fournir l'App ID via l'env."
                )
            client = DerivClient(
                app_id=self._app_id,
                rest_base_url=self._rest_base_url,
                preferred_account_type=self._account_type,
            )
            self._client = client
            try:
                await client.connect(pat_token=api_token)
                info = client.account_info
                connected_type = str(info.get("account_type", "")).strip().lower()
                if connected_type != self._account_type:
                    # Garde-fou : ne jamais trader sur un autre type de compte
                    # que celui demandé (démo demandée => jamais de réel).
                    raise DerivError(
                        "AccountTypeMismatch",
                        f"Compte {connected_type or 'inconnu'} obtenu au lieu "
                        f"d'un compte {self._account_type}",
                    )
                self._currency = str(info.get("currency", "USD"))
                self._start_balance = float(info.get("balance", 0.0))
            except BaseException:
                # Aucune session ouverte : on libère la connexion éventuelle.
                self._client = None
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    logger.exception("Erreur à la fermeture du client Deriv")
                raise

            self._state = BotState.RUNNING
            self._session_open = True
            # Émis avant le lancement de la boucle : "started" précède donc
            # tout TradeOpenedEvent de la session.
            self._emit(
                self._on_session,
                SessionEvent(
                    user_id=self._user_id,
                    kind="started",
                    account_type=self._account_type,
                ),
            )
            self._task = asyncio.create_task(self._run_loop())
            logger.info(
                "Bot démarré: user=%s compte=%s symbol=%s stake=%.2f SL=%.2f TP=%.2f strat=%s",
                self._user_id,
                self._account_type,
                symbol,
                stake,
                stop_loss,
                take_profit,
                self._strategy.value,
            )

    async def stop(self) -> None:
        """Arrête la session et libère les ressources (idempotent)."""
        async with self._lock:
            if self._state not in _TERMINAL_STATES:
                self._state = BotState.STOPPED
            await self._teardown()
            # La boucle émet "stopped" en sortant ; filet de sécurité si elle
            # n'a jamais tourné (tâche annulée avant son premier pas).
            self._close_session()
            logger.info("Bot arrêté (user=%s, PnL=%.2f)", self._user_id, self._risk.pnl)

    async def pause(self) -> None:
        """Met la boucle en pause sans fermer la connexion."""
        async with self._lock:
            if self._state == BotState.RUNNING:
                self._state = BotState.PAUSED
                logger.info("Bot en pause")

    async def resume(self) -> None:
        async with self._lock:
            if self._state == BotState.PAUSED:
                self._state = BotState.RUNNING
                logger.info("Bot repris")

    async def flush_events(self, timeout: Optional[float] = 5.0) -> None:
        """Attend la livraison des événements déjà émis (arrêt serveur, tests)."""
        last = self._last_delivery
        if last is not None and not last.done():
            await asyncio.wait({last}, timeout=timeout)

    def get_status(self) -> dict[str, Any]:
        """Snapshot léger de l'état (lecture synchrone, sûre en event-loop)."""
        return {
            "state": self._state.value,
            "account_type": self._account_type,
            "symbol": self._symbol,
            "strategy": self._strategy.value,
            "stake": round(self._stake, 2),
            "currency": self._currency,
            "stop_loss": round(self._risk.stop_loss, 2),
            "take_profit": round(self._risk.take_profit, 2),
            "pnl": round(self._risk.pnl, 2),
            "start_balance": round(self._start_balance, 2),
            "current_balance": round(self._start_balance + self._risk.pnl, 2),
            "trades_total": self._risk.trades_total,
            "trades_won": self._risk.trades_won,
            "trades_lost": self._risk.trades_lost,
            "error": self._error_message,
            "last_trades": [t.to_dict() for t in reversed(self._trades)],
        }

    # ------------------------------------------------------------------
    # Boucle d'exécution
    # ------------------------------------------------------------------
    async def _run_loop(self) -> None:
        assert self._client is not None
        client = self._client
        try:
            client.on_subscription("tick", self._on_tick)
            client.on_subscription("proposal_open_contract", self._on_poc)
            await client.send({"ticks": self._symbol, "subscribe": 1})

            while self._state not in _TERMINAL_STATES:
                if self._state == BotState.PAUSED:
                    await asyncio.sleep(0.5)
                    continue
                # Contrôle AVANT achat : le seuil n'est évalué qu'après règlement,
                # une mise supérieure au budget restant ferait donc dépasser le
                # stop loss (jusqu'au plafond Martingale). On s'arrête avant.
                if self._risk.stake_exceeds_budget(self._current_stake):
                    self._state = BotState.STOP_LOSS_REACHED
                    logger.warning(
                        "Stop loss : prochaine mise %.2f > budget restant %.2f, "
                        "arrêt avant dépassement (PnL=%.2f)",
                        self._current_stake,
                        self._risk.remaining_loss_budget(),
                        self._risk.pnl,
                    )
                    break

                if len(self._ticks) < self._min_ticks:
                    await asyncio.sleep(0.3)
                    continue

                decision = self._decide()
                if decision is None:
                    await asyncio.sleep(0.3)
                    continue

                await self._execute_trade(*decision)

                breached = self._risk.breached_state()
                if breached is not None:
                    self._state = breached
                    logger.warning(
                        "Seuil atteint (%s), PnL=%.2f", breached.value, self._risk.pnl
                    )
                    break

                await asyncio.sleep(self._trade_cooldown)

        except asyncio.CancelledError:
            raise
        except DerivError as exc:
            self._state = BotState.ERROR
            self._error_message = str(exc)
            logger.error("Erreur Deriv: %s", exc)
        except Exception as exc:  # noqa: BLE001
            self._state = BotState.ERROR
            self._error_message = str(exc)
            logger.exception("Erreur moteur")
        finally:
            try:
                await self._release_client()
            finally:
                # Sortie de boucle = fin de session, quelle qu'en soit la cause.
                if self._state not in _TERMINAL_STATES:
                    self._state = BotState.STOPPED
                self._close_session()

    # ------------------------------------------------------------------
    # Stratégie
    # ------------------------------------------------------------------
    def _decide(self) -> Optional[tuple[str, Optional[str]]]:
        """Retourne (contract_type, barrier) ou None si pas de signal."""
        if self._strategy == StrategyType.OVER_UNDER:
            return self._decide_over_under()
        # RISE_FALL et MARTINGALE partagent la même décision directionnelle ;
        # seule la mise diffère (voir _update_stake_after_trade).
        return self._decide_rise_fall()

    def _decide_rise_fall(self) -> Optional[tuple[str, Optional[str]]]:
        quotes = list(self._ticks)
        window = quotes[-self._min_ticks :]
        sma = sum(window) / len(window)
        last = quotes[-1]
        # Bande morte de 0.5 pip pour filtrer le bruit.
        deadband = 0.5 * (10 ** (-self._pip_size))
        # Nouvelle API Deriv : CALLE/PUTE (European Call/Put) remplacent CALL/PUT.
        if last > sma + deadband:
            return ("CALLE", None)
        if last < sma - deadband:
            return ("PUTE", None)
        return None

    def _decide_over_under(self) -> Optional[tuple[str, Optional[str]]]:
        digits = [self._last_digit(q) for q in list(self._ticks)[-self._min_ticks :]]
        avg_digit = sum(digits) / len(digits)
        # Retour à la moyenne autour de la barrière 5.
        if avg_digit < 4.0:
            return ("DIGITOVER", "5")
        if avg_digit > 5.0:
            return ("DIGITUNDER", "4")
        return None

    def _last_digit(self, quote: float) -> int:
        scaled = round(quote * (10**self._pip_size))
        return int(scaled) % 10

    # ------------------------------------------------------------------
    # Exécution d'un trade
    # ------------------------------------------------------------------
    async def _execute_trade(
        self, contract_type: str, barrier: Optional[str]
    ) -> None:
        assert self._client is not None
        loop = asyncio.get_running_loop()
        self._settlement = loop.create_future()
        duration_unit = "t"

        try:
            buy = await self._client.buy_proposal(
                contract_type=contract_type,
                symbol=self._symbol,
                amount=self._current_stake,
                duration=self._trade_duration,
                duration_unit=duration_unit,
                basis="stake",
                currency=self._currency,
                barrier=barrier,
                max_price=self._current_stake,  # basis=stake => ask_price == stake
            )
        except DerivError as exc:
            logger.warning("Achat refusé (%s), trade ignoré", exc)
            self._settlement = None
            return

        contract_id = int(buy["contract_id"])
        self._current_contract_id = contract_id
        buy_price = float(buy.get("buy_price", self._current_stake))
        self._emit(
            self._on_trade_opened,
            TradeOpenedEvent(
                user_id=self._user_id,
                contract_id=contract_id,
                contract_type=contract_type,
                symbol=self._symbol,
                stake=buy_price,
                duration=self._trade_duration,
                duration_unit=duration_unit,
                barrier=barrier,
                currency=self._currency,
                account_type=self._account_type,
                account_balance=self._start_balance + self._risk.pnl,
            ),
        )

        await self._client.proposal_open_contract(contract_id, subscribe=True)

        try:
            poc = await asyncio.wait_for(
                self._settlement, timeout=self._trade_timeout
            )
        except asyncio.TimeoutError:
            logger.error("Timeout règlement contrat %s", contract_id)
            self._settlement = None
            with_client = self._client
            if with_client is not None:
                await with_client.forget_all("proposal_open_contract")
            return

        self._record_settlement(poc, buy_price)
        self._settlement = None
        with_client = self._client
        if with_client is not None:
            await with_client.forget_all("proposal_open_contract")

    def _record_settlement(self, poc: dict[str, Any], buy_price: float) -> None:
        profit = float(poc.get("profit", 0.0))
        payout = float(poc.get("payout", 0.0))
        self._risk.register(profit)
        record = TradeRecord(
            contract_id=int(poc.get("contract_id", self._current_contract_id)),
            contract_type=str(poc.get("contract_type", "")),
            symbol=self._symbol,
            stake=buy_price,
            payout=payout,
            profit=profit,
            result="won" if profit > 0 else "lost",
            timestamp=time.time(),
        )
        self._trades.append(record)
        self._update_stake_after_trade(profit >= 0)
        logger.info(
            "Trade réglé: %s profit=%.2f PnL=%.2f next_stake=%.2f",
            record.contract_type,
            profit,
            self._risk.pnl,
            self._current_stake,
        )
        self._emit(
            self._on_trade_settled,
            TradeSettledEvent(
                user_id=self._user_id,
                contract_id=record.contract_id,
                profit=profit,
                payout=payout,
            ),
        )

    def _update_stake_after_trade(self, won: bool) -> None:
        """Ajuste la mise du prochain trade selon la stratégie."""
        if self._strategy != StrategyType.MARTINGALE:
            self._current_stake = self._base_stake
            return
        if won:
            # Reset : gain → on repart de la mise de base.
            self._current_stake = self._base_stake
        else:
            # Doublement, avec plafond dur pour éviter la ruine sur série longue.
            doubled = self._current_stake * self._martingale_factor
            self._current_stake = min(doubled, self._martingale_max_stake)

    # ------------------------------------------------------------------
    # Callbacks de flux
    # ------------------------------------------------------------------
    async def _on_tick(self, message: dict[str, Any]) -> None:
        tick = message.get("tick")
        if not isinstance(tick, dict):
            return
        quote = tick.get("quote")
        if quote is None:
            return
        pip = tick.get("pip_size")
        if isinstance(pip, int):
            self._pip_size = pip
        self._ticks.append(float(quote))

    async def _on_poc(self, message: dict[str, Any]) -> None:
        poc = message.get("proposal_open_contract")
        if not isinstance(poc, dict):
            return
        if int(poc.get("contract_id", -1)) != self._current_contract_id:
            return
        if poc.get("is_sold") and self._settlement is not None:
            if not self._settlement.done():
                self._settlement.set_result(poc)

    # ------------------------------------------------------------------
    # Événements
    # ------------------------------------------------------------------
    def _emit(
        self, listener: Optional[Callable[[Any], Awaitable[None]]], event: Any
    ) -> None:
        """Planifie la livraison de `event` sans bloquer le moteur.

        Chaque livraison attend la précédente : les abonnés reçoivent les
        événements d'une session dans l'ordre (started < opened < settled <
        stopped). Les exceptions des abonnés sont journalisées, jamais propagées.
        """
        if listener is None:
            return
        task = asyncio.create_task(_deliver(listener, event, self._last_delivery))
        self._last_delivery = task
        _PENDING_DELIVERIES.add(task)
        task.add_done_callback(_PENDING_DELIVERIES.discard)

    def _close_session(self) -> None:
        """Émet SessionEvent "stopped", une seule fois par session démarrée."""
        if not self._session_open:
            return
        self._session_open = False
        self._emit(
            self._on_session,
            SessionEvent(
                user_id=self._user_id,
                kind="stopped",
                account_type=self._account_type,
            ),
        )

    # ------------------------------------------------------------------
    # Nettoyage
    # ------------------------------------------------------------------
    async def _release_client(self) -> None:
        """Ferme la connexion Deriv de la session terminée.

        La fermeture du WebSocket annule côté Deriv tous les abonnements de la
        connexion (ticks, contrats) : pas besoin de forget_all au préalable.
        """
        client = self._client
        if client is None:
            return
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            logger.exception("Erreur à la fermeture du client Deriv")
        if self._client is client:
            self._client = None

    async def _teardown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("Erreur lors de l'annulation de la tâche")
            self._task = None
        if self._client is not None:
            await self._client.close()
            self._client = None


def idle_status(account_type: str = "demo") -> dict[str, Any]:
    """Snapshot d'un utilisateur sans session, au format exact de get_status()."""
    return BotEngine(account_type=account_type).get_status()
