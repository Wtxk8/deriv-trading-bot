"""Gestionnaire multi-utilisateurs des sessions de bot.

Chaque utilisateur a sa propre session : un `BotEngine` neuf est créé à chaque
démarrage. Plusieurs utilisateurs tradent donc en parallèle, et l'arrêt ou le
statut de l'un n'affecte jamais les autres.

Les opérations d'un même utilisateur (start/stop) sont sérialisées par un
verrou qui lui est propre — jamais de verrou global : une connexion lente chez
Deriv ne bloque que l'utilisateur concerné.

Les événements des moteurs (trade ouvert/réglé, début/fin de session) sont
relayés aux abonnés enregistrés (ex. copy trading) ; l'exception d'un abonné
est journalisée et n'affecte ni les autres abonnés ni le moteur.

Un moteur dont la session s'est terminée d'elle-même (SL/TP atteint, erreur)
est libéré dès réception de son SessionEvent "stopped" ; son dernier statut
reste consultable jusqu'au prochain démarrage.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import logging
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from bot_engine import (
    BotEngine,
    BotState,
    StrategyType,
    idle_status,
    normalize_account_type,
)
from trade_events import (
    SessionEvent,
    SessionListener,
    TradeOpenedEvent,
    TradeOpenedListener,
    TradeSettledEvent,
    TradeSettledListener,
)

logger = logging.getLogger("bot_manager")

# États pour lesquels une session est considérée comme active.
ACTIVE_STATES: frozenset[str] = frozenset(
    {BotState.RUNNING.value, BotState.PAUSED.value}
)

# Garde-fou mémoire : nombre max de derniers statuts de sessions terminées.
_MAX_FINISHED_SNAPSHOTS = 10_000


class BotEngineLike(Protocol):
    """Interface attendue d'un moteur (BotEngine, ou faux moteur de test)."""

    async def start(
        self,
        api_token: str,
        symbol: str,
        stake: float,
        stop_loss: float,
        take_profit: float,
        strategy_type: StrategyType | str = ...,
    ) -> None: ...

    async def stop(self) -> None: ...

    def get_status(self) -> dict[str, Any]: ...


# Appelée par mots-clés : user_id, account_type, on_trade_opened,
# on_trade_settled, on_session. `BotEngine` convient tel quel.
EngineFactory = Callable[..., BotEngineLike]


@dataclass(eq=False, slots=True)
class _Session:
    """Session d'un utilisateur (l'identité de l'objet distingue les sessions)."""

    user_id: int
    account_type: str
    engine: Any = None


class BotManager:
    """Registre des sessions de bot, une par utilisateur."""

    def __init__(self, engine_factory: Optional[EngineFactory] = None) -> None:
        self._engine_factory: EngineFactory = engine_factory or BotEngine
        self._sessions: dict[int, _Session] = {}
        self._finished: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self._locks: dict[int, asyncio.Lock] = {}
        self._trade_opened_listeners: list[TradeOpenedListener] = []
        self._trade_settled_listeners: list[TradeSettledListener] = []
        self._session_listeners: list[SessionListener] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closing: bool = False

    # ------------------------------------------------------------------
    # Abonnements aux événements des moteurs
    # ------------------------------------------------------------------
    def add_trade_opened_listener(self, listener: TradeOpenedListener) -> None:
        self._trade_opened_listeners.append(listener)

    def add_trade_settled_listener(self, listener: TradeSettledListener) -> None:
        self._trade_settled_listeners.append(listener)

    def add_session_listener(self, listener: SessionListener) -> None:
        self._session_listeners.append(listener)

    # ------------------------------------------------------------------
    # Lecture
    # ------------------------------------------------------------------
    def get(self, user_id: int) -> Optional[BotEngineLike]:
        """Moteur de la session en cours de `user_id`, ou None."""
        session = self._sessions.get(user_id)
        return session.engine if session is not None else None

    def is_active(self, user_id: int) -> bool:
        """True si `user_id` a une session RUNNING ou PAUSED."""
        session = self._sessions.get(user_id)
        if session is None:
            return False
        return session.engine.get_status().get("state") in ACTIVE_STATES

    def status(self, user_id: int) -> dict[str, Any]:
        """Snapshot de la session de `user_id` (STOPPED s'il n'en a jamais eu)."""
        session = self._sessions.get(user_id)
        if session is not None:
            return self._snapshot(session)
        finished = self._finished.get(user_id)
        if finished is not None:
            return copy.deepcopy(finished)
        return idle_status()

    # ------------------------------------------------------------------
    # Contrôle
    # ------------------------------------------------------------------
    async def start(
        self,
        user_id: int,
        *,
        api_token: str,
        symbol: str,
        stake: float,
        stop_loss: float,
        take_profit: float,
        strategy_type: StrategyType | str,
        account_type: str,
    ) -> dict[str, Any]:
        """Démarre une session neuve pour `user_id` et retourne son statut.

        Lève RuntimeError si une session de CET utilisateur est déjà active
        (RUNNING/PAUSED) ou si le serveur s'arrête. Les erreurs du moteur
        (DerivError, ValueError...) sont propagées telles quelles.
        """
        account = normalize_account_type(account_type)
        async with self._lock_for(user_id):
            if self._closing:
                raise RuntimeError("Serveur en cours d'arrêt : démarrage impossible")
            current = self._sessions.get(user_id)
            if current is not None:
                if current.engine.get_status().get("state") in ACTIVE_STATES:
                    raise RuntimeError(
                        "Une session de bot est déjà active pour cet utilisateur"
                    )
                # Session terminée d'elle-même (SL/TP/erreur), pas encore libérée.
                await self._release(current)

            session = _Session(user_id=user_id, account_type=account)
            session.engine = self._engine_factory(
                user_id=user_id,
                account_type=account,
                on_trade_opened=self._relay_trade_opened,
                on_trade_settled=self._relay_trade_settled,
                on_session=functools.partial(self._relay_session, session),
            )
            try:
                await session.engine.start(
                    api_token=api_token,
                    symbol=symbol,
                    stake=stake,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    strategy_type=strategy_type,
                )
            except BaseException:
                # Moteur jamais (ou à moitié) démarré : on le ferme et on l'oublie.
                await self._stop_engine_quietly(session.engine)
                raise
            if self._closing:
                # Arrêt serveur survenu pendant la connexion à Deriv.
                await self._stop_engine_quietly(session.engine)
                raise RuntimeError("Serveur en cours d'arrêt : démarrage impossible")

            self._sessions[user_id] = session
            self._finished.pop(user_id, None)
            logger.info(
                "Session bot démarrée : user=%s compte=%s symbole=%s",
                user_id,
                account,
                symbol,
            )
            return self._snapshot(session)

    async def stop(self, user_id: int) -> dict[str, Any]:
        """Arrête la session de `user_id` (sans effet s'il n'en a pas)."""
        async with self._lock_for(user_id):
            session = self._sessions.get(user_id)
            if session is not None:
                await self._release(session)
                logger.info("Session bot arrêtée : user=%s", user_id)
        return self.status(user_id)

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Arrêt serveur : stoppe toutes les sessions puis refuse tout démarrage."""
        self._closing = True
        sessions = list(self._sessions.values())
        if sessions:
            logger.info("Arrêt de %d session(s) de bot", len(sessions))
        results = await asyncio.gather(
            *(self.stop(session.user_id) for session in sessions),
            return_exceptions=True,
        )
        for session, result in zip(sessions, results):
            if isinstance(result, BaseException):
                logger.error(
                    "Échec d'arrêt de la session user=%s",
                    session.user_id,
                    exc_info=result,
                )
        # Laisse partir les derniers événements ("stopped") vers les abonnés.
        flushes = [
            session.engine.flush_events(timeout)
            for session in sessions
            if hasattr(session.engine, "flush_events")
        ]
        if flushes:
            await asyncio.gather(*flushes, return_exceptions=True)
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    # ------------------------------------------------------------------
    # Interne : cycle de vie des sessions
    # ------------------------------------------------------------------
    def _lock_for(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    @staticmethod
    def _snapshot(session: _Session) -> dict[str, Any]:
        status = dict(session.engine.get_status())
        status.setdefault("account_type", session.account_type)
        return status

    async def _release(self, session: _Session) -> None:
        """Arrête le moteur, mémorise son dernier statut et l'oublie.

        À appeler sous le verrou de l'utilisateur.
        """
        await self._stop_engine_quietly(session.engine)
        try:
            snapshot = self._snapshot(session)
        except Exception:  # noqa: BLE001
            logger.exception("Statut illisible pour user=%s", session.user_id)
            snapshot = idle_status(session.account_type)
        self._finished[session.user_id] = snapshot
        self._finished.move_to_end(session.user_id)
        while len(self._finished) > _MAX_FINISHED_SNAPSHOTS:
            self._finished.popitem(last=False)
        if self._sessions.get(session.user_id) is session:
            del self._sessions[session.user_id]

    @staticmethod
    async def _stop_engine_quietly(engine: BotEngineLike) -> None:
        # BotEngine.stop() annule la boucle avant toute opération faillible :
        # même en cas d'erreur ici, le moteur ne peut plus ouvrir de trade.
        try:
            await engine.stop()
        except Exception:  # noqa: BLE001
            logger.exception("Erreur à l'arrêt d'un moteur de bot")

    async def _release_if_current(self, session: _Session) -> None:
        async with self._lock_for(session.user_id):
            if self._sessions.get(session.user_id) is session:
                await self._release(session)
                logger.info(
                    "Session bot terminée et libérée : user=%s", session.user_id
                )

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "Tâche de fond du BotManager en erreur", exc_info=task.exception()
            )

    # ------------------------------------------------------------------
    # Interne : relais des événements
    # ------------------------------------------------------------------
    async def _relay_trade_opened(self, event: TradeOpenedEvent) -> None:
        await self._broadcast(self._trade_opened_listeners, event)

    async def _relay_trade_settled(self, event: TradeSettledEvent) -> None:
        await self._broadcast(self._trade_settled_listeners, event)

    async def _relay_session(self, session: _Session, event: SessionEvent) -> None:
        if event.kind == "stopped":
            # Libère le moteur sans attendre les abonnés : tâche séparée, qui
            # prend le verrou de l'utilisateur (jamais tenu ici).
            self._spawn(self._release_if_current(session))
        await self._broadcast(self._session_listeners, event)

    @staticmethod
    async def _broadcast(listeners: list[Any], event: Any) -> None:
        if not listeners:
            return
        await asyncio.gather(
            *(BotManager._notify(listener, event) for listener in list(listeners))
        )

    @staticmethod
    async def _notify(listener: Any, event: Any) -> None:
        try:
            await listener(event)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Abonné %s en erreur sur %s (user=%s)",
                getattr(listener, "__qualname__", repr(listener)),
                type(event).__name__,
                getattr(event, "user_id", None),
            )
