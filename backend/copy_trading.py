"""Copy trading : réplique les trades des comptes maîtres sur leurs suiveurs.

Principe (option B du cahier des charges) :
- un maître est un utilisateur désigné par un admin qui trade via NOTRE bot.
  L'API Deriv v1 n'offre ni flux « transaction » ni copy_start : on s'abonne
  donc aux événements du BotManager (session, trade ouvert, trade réglé) ;
- à chaque contrat acheté par le bot d'un maître, chaque suiveur actif achète
  le même contrat sur SON compte Deriv (un achat par suiveur, en parallèle),
  avec une mise proportionnelle à son solde, bornée par ses propres limites ;
- le règlement de chaque contrat copié est suivi via proposal_open_contract
  (flux, puis relevé ponctuel si le flux se tait) ; le PnL du jour (UTC) du
  suiveur alimente son stop loss journalier.

Sécurité et isolation :
- le token Deriv du suiveur est chiffré en base (token_crypto) ; il n'est
  déchiffré qu'en mémoire, à l'ouverture d'une connexion, et jamais journalisé ;
- l'échec d'un suiveur (token révoqué, solde insuffisant, réseau) n'affecte ni
  les autres suiveurs ni le bot du maître : chaque copie est isolée, et le
  BotManager livre ses événements sans jamais bloquer le moteur.

Les accès base sont synchrones et courts, exécutés sur la boucle asyncio : les
lectures-modifications-écritures (PnL du jour, pauses) ne sont donc jamais
entrelacées au sein du processus.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import os
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, Protocol

from sqlalchemy import case, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
import payments
import token_crypto
from deriv_client import DerivClient, DerivError
from models_copy import CopiedTrade, CopyFollow, CopyMaster, CopyMasterTrade
from trade_events import SessionEvent, TradeOpenedEvent, TradeSettledEvent

logger = logging.getLogger("copy_trading")

# ----------------------------------------------------------------------------
# Constantes métier
# ----------------------------------------------------------------------------
MIN_STAKE = 0.35  # mise minimale acceptée par Deriv
MIN_MULTIPLIER, MAX_MULTIPLIER = 0.1, 10.0
MIN_MAX_STAKE, MAX_MAX_STAKE = 0.35, 1000.0
MIN_DAILY_STOP_LOSS, MAX_DAILY_STOP_LOSS = 1.0, 100_000.0
STATS_WINDOW_DAYS = 30
ACCOUNT_TYPES = ("demo", "real")

STATUS_OPEN = "open"
STATUS_WON = "won"
STATUS_LOST = "lost"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# Motifs de pause (codes interprétés par l'application mobile).
PAUSE_DAILY_STOP_LOSS = "daily_stop_loss"  # levée au changement de jour UTC
PAUSE_TOKEN_INVALID = "token_invalid"  # levée en recréant l'abonnement
PAUSE_SUBSCRIPTION = "subscription_expired"  # levée dès que l'accès revient
PAUSE_MASTER_DISABLED = "master_disabled"  # affichage seulement, jamais stocké

REASON_MASTER_DEMO = "maitre en demo"
REASON_DAILY_STOP_LOSS = "stop loss journalier atteint"

# Tolérance flottante du contrôle « mise <= budget de perte restant du jour ».
_RISK_EPSILON = 1e-9

# Erreurs Deriv signifiant que le token (ou le compte demandé) est inutilisable.
_FATAL_AUTH_CODES = frozenset(
    {"Unauthorized", "NoAccount", "InvalidToken", "AuthorizationRequired", "HTTP401", "HTTP403"}
)

# Durée approximative d'une unité de contrat Deriv, en secondes.
_UNIT_SECONDS = {"t": 2.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class CopyTradingError(Exception):
    """Erreur métier du copy trading (message affichable, sans secret)."""


class CopyTradingUnavailable(CopyTradingError):
    """Fonction désactivée ou mal configurée côté serveur (-> 503)."""


class MasterNotFound(CopyTradingError, LookupError):
    """Maître inexistant ou désactivé (-> 404)."""


class AlreadyFollowing(CopyTradingError):
    """L'utilisateur suit déjà un maître (-> 409)."""


class NotFollowing(CopyTradingError, LookupError):
    """L'utilisateur ne suit aucun maître (-> 404)."""


class DerivClientLike(Protocol):
    """Sous-ensemble de DerivClient utilisé ici (remplaçable en test)."""

    @property
    def account_info(self) -> dict[str, Any]: ...

    async def connect(self, pat_token: str) -> None: ...

    async def close(self) -> None: ...

    def on_subscription(self, msg_type: str, callback: Any) -> None: ...

    async def buy_proposal(self, contract_type: str, symbol: str, amount: float, duration: int,
                           duration_unit: str = "t", basis: str = "stake", currency: str = "USD",
                           barrier: Optional[str] = None, max_price: Optional[float] = None,
                           ) -> dict[str, Any]: ...

    async def proposal_open_contract(self, contract_id: int, subscribe: bool = True) -> dict[str, Any]: ...


# Appelée avec le type de compte voulu ("demo" | "real").
ClientFactory = Callable[[str], DerivClientLike]
Clock = Callable[[], datetime]


def default_client_factory(account_type: str) -> DerivClient:
    """Client Deriv réel, avec le Deriv-App-ID de l'environnement."""
    app_id = os.environ.get("DERIV_APP_ID", "").strip()
    if not app_id:
        raise CopyTradingUnavailable("DERIV_APP_ID non configuré côté serveur")
    return DerivClient(app_id, preferred_account_type=account_type)


def is_feature_enabled() -> bool:
    """COPY_TRADING_ENABLED vaut true ET une clé COPY_TOKEN_KEY valide existe."""
    flag = os.environ.get("COPY_TRADING_ENABLED", "").strip().lower() in _TRUE_VALUES
    return flag and token_crypto.is_configured()


def has_copy_access(user: Optional[models.User]) -> bool:
    """Accès au copy trading : compte actif ET (admin, essai actif ou premium actif)."""
    if user is None or not user.active:
        return False
    if user.role == "admin":
        return True
    return payments.can_trade_real(
        user.subscription_tier, user.subscription_expires_at, user.trial_started_at
    )


def _positive(value: Optional[float]) -> bool:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def compute_copy_stake(
    master_stake: float,
    master_balance: Optional[float],
    follower_balance: Optional[float],
    multiplier: float,
    max_stake: float,
) -> float:
    """Mise du suiveur : proportionnelle aux soldes, multipliée, bornée.

    mise = stake_maître x (solde_suiveur / solde_maître) x multiplicateur,
    ou stake_maître x multiplicateur si l'un des soldes est inconnu ou nul ;
    arrondie à 2 décimales puis bornée à [0.35, max_stake].
    """
    raw = float(master_stake)
    if _positive(master_balance) and _positive(follower_balance):
        raw *= float(follower_balance) / float(master_balance)  # type: ignore[arg-type]
    raw *= float(multiplier)
    stake = round(raw, 2) if math.isfinite(raw) else MIN_STAKE
    upper = max(MIN_STAKE, float(max_stake))
    return round(min(max(stake, MIN_STAKE), upper), 2)


def _expected_duration_s(duration: Any, unit: Any) -> float:
    try:
        amount = max(0.0, float(duration))
    except (TypeError, ValueError):
        amount = 0.0
    return amount * _UNIT_SECONDS.get(str(unit or "").strip().lower(), 60.0)


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite rend des datetimes naïfs : on les considère en UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    moment = _utc(value)
    return moment.isoformat() if moment is not None else None


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clip(message: str, limit: int = 255) -> str:
    message = " ".join(str(message).split())
    return message if len(message) <= limit else message[: limit - 1] + "…"


def _normalize_account_type(account_type: str) -> str:
    value = str(account_type or "").strip().lower()
    if value not in ACCOUNT_TYPES:
        raise ValueError("account_type doit valoir 'demo' ou 'real'")
    return value


def _check_range(name: str, value: Optional[float], low: float, high: float) -> None:
    if value is None:
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} invalide") from None
    if not math.isfinite(number) or number < low or number > high:
        raise ValueError(f"{name} doit être compris entre {low:g} et {high:g}")


def _validate_limits(
    multiplier: Optional[float], max_stake: Optional[float], daily_stop_loss: Optional[float]
) -> None:
    _check_range("multiplier", multiplier, MIN_MULTIPLIER, MAX_MULTIPLIER)
    _check_range("max_stake", max_stake, MIN_MAX_STAKE, MAX_MAX_STAKE)
    _check_range("daily_stop_loss", daily_stop_loss, MIN_DAILY_STOP_LOSS, MAX_DAILY_STOP_LOSS)


def _connect_failure(exc: BaseException) -> tuple[str, bool]:
    """(raison affichable, token définitivement inutilisable ?)."""
    if isinstance(exc, token_crypto.TokenDecryptError):
        return "Token Deriv illisible côté serveur : reconfigurez la copie", True
    if isinstance(exc, token_crypto.TokenCryptoError):
        return "Chiffrement des tokens indisponible côté serveur", False
    if isinstance(exc, DerivError):
        if exc.code in _FATAL_AUTH_CODES:
            return "Token Deriv refusé ou compte introuvable : reconfigurez la copie", True
        return _clip(f"Connexion Deriv impossible : {exc.message}"), False
    if isinstance(exc, asyncio.TimeoutError):
        return "Connexion Deriv : délai dépassé", False
    if isinstance(exc, CopyTradingError):
        return _clip(str(exc)), False
    return f"Connexion Deriv impossible ({type(exc).__name__})", False


def _buy_failure(exc: BaseException) -> str:
    if isinstance(exc, DerivError):
        return _clip(f"Achat refusé par Deriv : {exc.message}")
    if isinstance(exc, asyncio.TimeoutError):
        return "Pas de réponse de Deriv à l'achat : vérifiez votre relevé Deriv"
    return f"Achat impossible ({type(exc).__name__})"


# ----------------------------------------------------------------------------
# Structures internes
# ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _FollowSnapshot:
    """Copie immuable d'un abonnement (jamais d'objet ORM entre deux await)."""

    id: int
    follower_user_id: int
    master_user_id: int
    account_type: str
    account_currency: str
    multiplier: float
    max_stake: float
    encrypted_token: str = field(repr=False)

    @classmethod
    def of(cls, follow: CopyFollow) -> "_FollowSnapshot":
        return cls(
            id=follow.id,
            follower_user_id=follow.follower_user_id,
            master_user_id=follow.master_user_id,
            account_type=follow.account_type,
            account_currency=follow.account_currency or "USD",
            multiplier=float(follow.multiplier),
            max_stake=float(follow.max_stake),
            encrypted_token=follow.encrypted_token,
        )


@dataclass(eq=False)
class _FollowerConn:
    """Connexion Deriv ouverte d'un suiveur."""

    follow_id: int
    follower_user_id: int
    client: Any
    account_type: str
    currency: str
    balance: Optional[float]
    contracts: set[int] = field(default_factory=set)  # contrats achetés via cette connexion
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    closing: bool = False

    def __post_init__(self) -> None:
        self.idle.set()


@dataclass(eq=False)
class _Pending:
    """Contrat copié en attente de règlement."""

    trade_id: int
    conn: _FollowerConn
    day: date  # jour UTC d'ouverture : le résultat compte pour ce jour-là
    sub_id: Optional[str] = None
    watchdog: Optional[asyncio.Task[Any]] = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------------
# Service
# ----------------------------------------------------------------------------
class CopyTradingService:
    """Réplication des trades des maîtres + opérations des routes /copy."""

    # Réglages (surchargés en test).
    connect_timeout_s: float = 25.0
    settle_grace_s: float = 30.0  # marge après la durée théorique du contrat
    settle_poll_attempts: int = 3
    settle_poll_interval_s: float = 10.0
    max_drain_s: float = 900.0  # attente max des règlements avant fermeture

    def __init__(
        self,
        session_factory: Callable[[], Session],
        bot_manager: Any,
        client_factory: Optional[ClientFactory] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        self._session_factory = session_factory
        self._bot_manager = bot_manager
        self._client_factory: ClientFactory = client_factory or default_client_factory
        self._clock: Clock = clock or _utcnow
        self.enabled: bool = is_feature_enabled()
        self._started_at: datetime = self._now()
        self._registered = False
        self._closing = False
        self._conns: dict[int, _FollowerConn] = {}  # follow_id -> connexion courante
        self._pools: dict[int, set[int]] = {}  # master_id -> follow_ids connectés
        self._live: set[_FollowerConn] = set()  # toutes les connexions ouvertes
        self._conn_locks: dict[int, asyncio.Lock] = {}
        self._pending: dict[int, dict[int, _Pending]] = {}  # follow_id -> contrat -> suivi
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------
    def register(self) -> None:
        """S'abonne aux événements du BotManager (sans effet si désactivé)."""
        if not self.enabled:
            logger.info(
                "Copy trading désactivé (COPY_TRADING_ENABLED != true ou COPY_TOKEN_KEY absente)"
            )
            return
        if self._registered:
            return
        self._bot_manager.add_trade_opened_listener(self.on_trade_opened)
        self._bot_manager.add_trade_settled_listener(self.on_trade_settled)
        self._bot_manager.add_session_listener(self.on_session)
        self._registered = True
        logger.info("Copy trading activé")

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Arrêt serveur : annule les suivis et ferme toutes les connexions.

        Les contrats encore ouverts restent « open » en base ; ils sont relevés
        à la prochaine connexion du suiveur.
        """
        self._closing = True
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        conns = list(self._live)
        self._conns.clear()
        self._pools.clear()
        self._pending.clear()
        if conns:
            closers = [asyncio.create_task(self._close_conn(conn)) for conn in conns]
            _, not_done = await asyncio.wait(closers, timeout=timeout)
            for task in not_done:
                task.cancel()
            logger.info("Copy trading : %d connexion(s) suiveur fermée(s)", len(conns))

    # ------------------------------------------------------------------
    # Écouteurs du BotManager
    # ------------------------------------------------------------------
    async def on_session(self, event: SessionEvent) -> None:
        if self._closing:
            return
        if event.kind == "stopped":
            self._close_pool(event.user_id)
            return
        if event.kind != "started":
            return
        snaps = [
            snap
            for snap in (
                self._prepare(follow_id, event.account_type, None)
                for follow_id in self._follow_ids_of_master(event.user_id)
            )
            if snap is not None
        ]
        if not snaps:
            return
        results = await asyncio.gather(
            *(self._open_for_session(event.user_id, snap) for snap in snaps)
        )
        logger.info(
            "Session maître user=%s : %d/%d connexion(s) suiveur ouverte(s)",
            event.user_id,
            sum(1 for ok in results if ok),
            len(snaps),
        )

    async def on_trade_opened(self, event: TradeOpenedEvent) -> None:
        if self._closing:
            return
        now = self._now()
        with self._session_factory() as db:
            master = db.get(CopyMaster, event.user_id)
            if master is None or not master.enabled:
                return
            db.add(
                CopyMasterTrade(
                    master_user_id=event.user_id,
                    contract_id=int(event.contract_id),
                    symbol=event.symbol,
                    contract_type=event.contract_type,
                    stake=float(event.stake),
                    account_type=event.account_type,
                    created_at=now,
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                logger.warning(
                    "Contrat maître %s (user=%s) déjà traité : copie ignorée",
                    event.contract_id,
                    event.user_id,
                )
                return
            follow_ids = self._active_follow_ids(db, event.user_id)
        if not follow_ids:
            return
        results = await asyncio.gather(
            *(self._copy_to_follower(event, follow_id) for follow_id in follow_ids),
            return_exceptions=True,
        )
        for follow_id, result in zip(follow_ids, results):
            if isinstance(result, BaseException):
                logger.error(
                    "Copie du contrat %s en erreur pour follow=%s",
                    event.contract_id,
                    follow_id,
                    exc_info=result,
                )

    async def on_trade_settled(self, event: TradeSettledEvent) -> None:
        with self._session_factory() as db:
            row = db.execute(
                select(CopyMasterTrade).where(
                    CopyMasterTrade.master_user_id == event.user_id,
                    CopyMasterTrade.contract_id == int(event.contract_id),
                )
            ).scalar_one_or_none()
            if row is None:
                return
            row.profit = round(float(event.profit), 2)
            row.settled_at = self._now()
            db.commit()

    # ------------------------------------------------------------------
    # Copie d'un contrat sur un suiveur
    # ------------------------------------------------------------------
    async def _copy_to_follower(self, event: TradeOpenedEvent, follow_id: int) -> None:
        snap = self._prepare(follow_id, event.account_type, event)
        if snap is None:
            return
        try:
            conn = await self._get_conn(event.user_id, snap)
        except Exception as exc:  # noqa: BLE001 — isolé par suiveur
            reason, fatal = _connect_failure(exc)
            self._add_trade(snap, event, stake=0.0, status=STATUS_FAILED, reason=reason)
            if fatal:
                self._pause(snap.id, PAUSE_TOKEN_INVALID)
            logger.warning(
                "Copie impossible (connexion) : follow=%s contrat maître=%s (%s)",
                snap.id,
                event.contract_id,
                type(exc).__name__,
            )
            return

        stake = compute_copy_stake(
            event.stake, event.account_balance, conn.balance, snap.multiplier, snap.max_stake
        )
        trade_id = self._add_trade_within_budget(snap, event, stake)
        if trade_id is None:
            return
        try:
            result = await conn.client.buy_proposal(
                contract_type=event.contract_type,
                symbol=event.symbol,
                amount=stake,
                duration=event.duration,
                duration_unit=event.duration_unit,
                basis="stake",
                currency=conn.currency,
                barrier=event.barrier,
                max_price=stake,
            )
        except Exception as exc:  # noqa: BLE001 — isolé par suiveur
            self._close_trade(trade_id, STATUS_FAILED, reason=_buy_failure(exc))
            logger.warning(
                "Copie refusée : follow=%s contrat maître=%s (%s)",
                snap.id,
                event.contract_id,
                type(exc).__name__,
            )
            return

        contract_id = _to_int(result.get("contract_id")) if isinstance(result, dict) else None
        if contract_id is None:
            self._close_trade(
                trade_id,
                STATUS_FAILED,
                reason="Réponse d'achat Deriv inattendue : vérifiez votre relevé Deriv",
            )
            return
        buy_price = round(_to_float(result.get("buy_price"), stake), 2)
        delay = _expected_duration_s(event.duration, event.duration_unit) + self.settle_grace_s
        self._track(conn, trade_id, contract_id, buy_price, delay=delay, day=self._now().date())
        logger.info(
            "Contrat copié : follow=%s maître=%s contrat=%s mise=%.2f %s",
            snap.id,
            event.contract_id,
            contract_id,
            buy_price,
            conn.currency,
        )
        try:
            poc = await conn.client.proposal_open_contract(contract_id, subscribe=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Abonnement au contrat copié %s impossible (%s) : relevé différé",
                contract_id,
                type(exc).__name__,
            )
            return
        if isinstance(poc, dict) and poc.get("is_sold"):
            self._settle(snap.id, contract_id, poc)

    def _prepare(
        self, follow_id: int, master_account_type: str, event: Optional[TradeOpenedEvent]
    ) -> Optional[_FollowSnapshot]:
        """Applique jour UTC, droits et pauses ; renvoie l'abonnement à copier.

        Avec `event`, une copie ignorée (stop loss journalier, maître en démo
        vers suiveur réel) est enregistrée en « skipped ».
        """
        now = self._now()
        with self._session_factory() as db:
            follow = db.get(CopyFollow, follow_id)
            if follow is None or not follow.active:
                return None
            self._rollover(follow, now)
            if not has_copy_access(db.get(models.User, follow.follower_user_id)):
                if follow.paused_reason is None:
                    follow.paused_reason = PAUSE_SUBSCRIPTION
                    logger.info("Copie en pause (accès expiré) : follow=%s", follow.id)
                db.commit()
                return None
            if follow.paused_reason == PAUSE_SUBSCRIPTION:
                follow.paused_reason = None
            if follow.paused_reason is None and follow.today_pnl <= -follow.daily_stop_loss:
                follow.paused_reason = PAUSE_DAILY_STOP_LOSS
            skip_reason: Optional[str] = None
            if follow.paused_reason == PAUSE_DAILY_STOP_LOSS:
                skip_reason = REASON_DAILY_STOP_LOSS
            elif follow.paused_reason is not None:
                db.commit()
                return None
            elif master_account_type == "demo" and follow.account_type == "real":
                skip_reason = REASON_MASTER_DEMO
            if skip_reason is not None:
                if event is not None:
                    db.add(self._new_trade(follow, event, 0.0, STATUS_SKIPPED, skip_reason, now))
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                return None
            snap = _FollowSnapshot.of(follow)
            db.commit()
            return snap

    @staticmethod
    def _rollover(follow: CopyFollow, now: datetime) -> None:
        """Nouveau jour UTC : compteur remis à zéro, pause stop loss levée."""
        today = now.date()
        if follow.pnl_day != today:
            follow.pnl_day = today
            follow.today_pnl = 0.0
            if follow.paused_reason == PAUSE_DAILY_STOP_LOSS:
                follow.paused_reason = None

    # ------------------------------------------------------------------
    # Connexions des suiveurs
    # ------------------------------------------------------------------
    async def _open_for_session(self, master_id: int, snap: _FollowSnapshot) -> bool:
        try:
            await self._get_conn(master_id, snap)
        except Exception as exc:  # noqa: BLE001 — isolé par suiveur
            _, fatal = _connect_failure(exc)
            if fatal:
                self._pause(snap.id, PAUSE_TOKEN_INVALID)
            logger.warning(
                "Connexion suiveur impossible : follow=%s (%s)", snap.id, type(exc).__name__
            )
            return False
        return True

    async def _get_conn(self, master_id: int, snap: _FollowSnapshot) -> _FollowerConn:
        """Connexion ouverte du suiveur, ouverte à la volée si absente."""
        conn = self._conns.get(snap.id)
        if conn is None or not self._is_alive(conn):
            lock = self._conn_locks.setdefault(snap.id, asyncio.Lock())
            async with lock:
                conn = self._conns.get(snap.id)
                if conn is None or not self._is_alive(conn):
                    if conn is not None:
                        # Connexion morte (reconnexion Deriv en échec) : remplacée.
                        self._conns.pop(snap.id, None)
                        self._spawn(self._drain_and_close(conn))
                    conn = await self._open_conn(snap)
                    if self._closing:
                        await self._close_conn(conn)
                        raise CopyTradingUnavailable("Service en cours d'arrêt")
                    self._conns[snap.id] = conn
                    self._spawn(self._reconcile_open_trades(conn))
        self._pools.setdefault(master_id, set()).add(snap.id)
        return conn

    async def _open_conn(self, snap: _FollowSnapshot) -> _FollowerConn:
        token = token_crypto.decrypt(snap.encrypted_token)
        client = self._client_factory(snap.account_type)
        try:
            await asyncio.wait_for(client.connect(token), timeout=self.connect_timeout_s)
            info = dict(client.account_info)
        except BaseException:
            await self._close_client_quietly(client)
            raise
        finally:
            del token
        balance = info.get("balance")
        conn = _FollowerConn(
            follow_id=snap.id,
            follower_user_id=snap.follower_user_id,
            client=client,
            account_type=snap.account_type,
            currency=str(info.get("currency") or snap.account_currency or "USD"),
            balance=_to_float(balance, 0.0) if balance is not None else None,
        )
        # Un seul callback par msg_type et par client : réparti par contract_id.
        client.on_subscription(
            "proposal_open_contract", functools.partial(self._on_poc_message, snap.id)
        )
        self._live.add(conn)
        return conn

    @staticmethod
    def _is_alive(conn: _FollowerConn) -> bool:
        return not conn.closing and bool(getattr(conn.client, "is_connected", True))

    def _close_pool(self, master_id: int) -> None:
        """Fin de session du maître : ferme les connexions de ses suiveurs."""
        for follow_id in self._pools.pop(master_id, set()):
            conn = self._conns.pop(follow_id, None)
            if conn is not None:
                self._spawn(self._drain_and_close(conn))

    def _close_follow(self, follow_id: int) -> None:
        for follow_ids in self._pools.values():
            follow_ids.discard(follow_id)
        conn = self._conns.pop(follow_id, None)
        if conn is not None:
            self._spawn(self._drain_and_close(conn))

    async def _drain_and_close(self, conn: _FollowerConn) -> None:
        """Attend le règlement des contrats achetés via `conn`, puis la ferme."""
        conn.closing = True
        if conn.contracts:
            try:
                await asyncio.wait_for(conn.idle.wait(), timeout=self.max_drain_s)
            except asyncio.TimeoutError:
                logger.warning(
                    "follow=%s : %d contrat(s) non réglé(s) à la fermeture",
                    conn.follow_id,
                    len(conn.contracts),
                )
        await self._close_conn(conn)

    async def _close_conn(self, conn: _FollowerConn) -> None:
        conn.closing = True
        try:
            await self._close_client_quietly(conn.client)
        finally:
            self._live.discard(conn)

    @staticmethod
    async def _close_client_quietly(client: Any) -> None:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            logger.warning("Fermeture d'une connexion Deriv suiveur en erreur", exc_info=True)

    # ------------------------------------------------------------------
    # Suivi des règlements
    # ------------------------------------------------------------------
    def _track(
        self,
        conn: _FollowerConn,
        trade_id: int,
        contract_id: int,
        buy_price: float,
        *,
        delay: float,
        day: date,
    ) -> None:
        try:
            with self._session_factory() as db:
                trade = db.get(CopiedTrade, trade_id)
                if trade is not None:
                    trade.follower_contract_id = contract_id
                    trade.stake = buy_price
                    db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Enregistrement du contrat copié %s en erreur", contract_id)
        self._watch(conn, trade_id, contract_id, day=day, delay=delay)

    def _watch(
        self, conn: _FollowerConn, trade_id: int, contract_id: int, *, day: date, delay: float
    ) -> None:
        pending = _Pending(trade_id=trade_id, conn=conn, day=day)
        self._pending.setdefault(conn.follow_id, {})[contract_id] = pending
        conn.contracts.add(contract_id)
        conn.idle.clear()
        pending.watchdog = self._spawn(
            self._watch_settlement(conn.follow_id, contract_id, delay)
        )

    async def _on_poc_message(self, follow_id: int, message: dict[str, Any]) -> None:
        poc = message.get("proposal_open_contract")
        if not isinstance(poc, dict):
            return
        contract_id = _to_int(poc.get("contract_id"))
        pending = self._pending.get(follow_id, {}).get(contract_id) if contract_id else None
        if pending is None:
            return
        subscription = message.get("subscription")
        if isinstance(subscription, dict) and subscription.get("id"):
            pending.sub_id = str(subscription["id"])
        if poc.get("is_sold"):
            self._settle(follow_id, contract_id, poc)

    async def _watch_settlement(self, follow_id: int, contract_id: int, delay: float) -> None:
        """Filet de sécurité : relève le contrat si le flux ne l'a pas réglé."""
        await asyncio.sleep(max(0.0, delay))
        for attempt in range(max(1, self.settle_poll_attempts)):
            pending = self._pending.get(follow_id, {}).get(contract_id)
            if pending is None:
                return
            client = self._polling_client(follow_id, pending.conn)
            if client is not None:
                try:
                    poc = await client.proposal_open_contract(contract_id, subscribe=False)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Relevé du contrat copié %s en erreur (%s)", contract_id, type(exc).__name__
                    )
                else:
                    if isinstance(poc, dict) and poc.get("is_sold"):
                        self._settle(follow_id, contract_id, poc)
                        return
            if attempt + 1 < self.settle_poll_attempts:
                await asyncio.sleep(self.settle_poll_interval_s)
        pending = self._pending.get(follow_id, {}).pop(contract_id, None)
        if pending is not None:
            # Reste « open » en base : relevé à la prochaine connexion du suiveur.
            self._forget_pending(pending, contract_id)
            logger.warning(
                "Contrat copié %s (follow=%s) non réglé : relevé à la prochaine connexion",
                contract_id,
                follow_id,
            )

    def _polling_client(self, follow_id: int, conn: _FollowerConn) -> Any:
        """Client utilisable pour relever un contrat : celui de l'achat (même en
        cours de fermeture), sinon la connexion courante du suiveur."""
        for candidate in (conn, self._conns.get(follow_id)):
            if (
                candidate is not None
                and candidate in self._live
                and getattr(candidate.client, "is_connected", True)
            ):
                return candidate.client
        return None

    def _settle(self, follow_id: int, contract_id: int, poc: dict[str, Any]) -> None:
        bucket = self._pending.get(follow_id)
        pending = bucket.pop(contract_id, None) if bucket else None
        if pending is None:
            return  # déjà réglé
        if not bucket:
            self._pending.pop(follow_id, None)
        profit = round(_to_float(poc.get("profit"), 0.0), 2)
        status = STATUS_WON if profit > 0 else STATUS_LOST
        now = self._now()
        try:
            with self._session_factory() as db:
                trade = db.get(CopiedTrade, pending.trade_id)
                if trade is not None and trade.status == STATUS_OPEN:
                    trade.status = status
                    trade.profit = profit
                    trade.settled_at = now
                follow = db.get(CopyFollow, follow_id)
                if follow is not None:
                    self._rollover(follow, now)
                    if pending.day == follow.pnl_day:
                        follow.today_pnl = round(follow.today_pnl + profit, 2)
                        if (
                            follow.paused_reason is None
                            and follow.today_pnl <= -follow.daily_stop_loss
                        ):
                            follow.paused_reason = PAUSE_DAILY_STOP_LOSS
                            logger.info(
                                "Stop loss journalier atteint : follow=%s (PnL %.2f)",
                                follow_id,
                                follow.today_pnl,
                            )
                db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Enregistrement du règlement du contrat copié %s en erreur", contract_id)
        conn = pending.conn
        if conn.balance is not None:
            conn.balance = round(conn.balance + profit, 2)
        self._forget_pending(pending, contract_id)
        if pending.sub_id and self._is_alive(conn):
            self._spawn(self._forget_subscription(conn, pending.sub_id))

    def _forget_pending(self, pending: _Pending, contract_id: int) -> None:
        conn = pending.conn
        conn.contracts.discard(contract_id)
        if not conn.contracts:
            conn.idle.set()
        watchdog = pending.watchdog
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()

    @staticmethod
    async def _forget_subscription(conn: _FollowerConn, sub_id: str) -> None:
        send = getattr(conn.client, "send", None)
        if send is None:
            return
        try:
            await send({"forget": sub_id})
        except Exception:  # noqa: BLE001 — abonnement déjà clos côté Deriv
            logger.debug("forget %s ignoré", sub_id)

    async def _reconcile_open_trades(self, conn: _FollowerConn) -> None:
        """Reprend les contrats restés « open » (redémarrage, flux perdu)."""
        now = self._now()
        known = self._pending.get(conn.follow_id, {})
        with self._session_factory() as db:
            rows = db.execute(
                select(CopiedTrade).where(
                    CopiedTrade.follow_id == conn.follow_id,
                    CopiedTrade.status == STATUS_OPEN,
                )
            ).scalars().all()
            to_poll: list[tuple[int, int, date]] = []
            for trade in rows:
                created = _utc(trade.created_at) or now
                if trade.follower_contract_id is None:
                    if created < self._started_at:
                        # Achat interrompu par un redémarrage : issue inconnue.
                        trade.status = STATUS_FAILED
                        trade.reason = "Achat interrompu (redémarrage) : vérifiez votre relevé Deriv"
                        trade.settled_at = now
                elif trade.follower_contract_id not in known:
                    to_poll.append((trade.id, int(trade.follower_contract_id), created.date()))
            db.commit()
        for trade_id, contract_id, day in to_poll:
            if contract_id not in self._pending.get(conn.follow_id, {}):
                self._watch(conn, trade_id, contract_id, day=day, delay=0.0)

    # ------------------------------------------------------------------
    # Écritures élémentaires
    # ------------------------------------------------------------------
    @staticmethod
    def _new_trade(
        follow: CopyFollow | _FollowSnapshot,
        event: TradeOpenedEvent,
        stake: float,
        status: str,
        reason: Optional[str],
        now: datetime,
    ) -> CopiedTrade:
        return CopiedTrade(
            follow_id=follow.id,
            follower_user_id=follow.follower_user_id,
            master_contract_id=int(event.contract_id),
            symbol=event.symbol,
            contract_type=event.contract_type,
            stake=stake,
            status=status,
            reason=_clip(reason) if reason else None,
            created_at=now,
            settled_at=None if status == STATUS_OPEN else now,
        )

    def _add_trade(
        self,
        snap: _FollowSnapshot,
        event: TradeOpenedEvent,
        *,
        stake: float,
        status: str,
        reason: Optional[str] = None,
    ) -> Optional[int]:
        try:
            with self._session_factory() as db:
                trade = self._new_trade(snap, event, stake, status, reason, self._now())
                db.add(trade)
                db.commit()
                return trade.id
        except IntegrityError:
            logger.warning(
                "Contrat maître %s déjà copié pour follow=%s", event.contract_id, snap.id
            )
        except Exception:  # noqa: BLE001
            logger.exception("Enregistrement de la copie follow=%s en erreur", snap.id)
        return None

    def _add_trade_within_budget(
        self, snap: _FollowSnapshot, event: TradeOpenedEvent, stake: float
    ) -> Optional[int]:
        """Enregistre la copie « open » si la mise tient dans le budget du jour.

        Budget de perte restant = daily_stop_loss + today_pnl (après remise à
        zéro du jour UTC), relu ici car un règlement a pu le modifier pendant la
        connexion. Une mise qui le dépasse ferait franchir le stop loss
        journalier : la copie est enregistrée en « skipped » et l'abonnement
        passe en pause stop loss journalier (levée au changement de jour UTC).
        Renvoie l'id de la copie à acheter, ou None s'il ne faut rien acheter.
        """
        now = self._now()
        try:
            with self._session_factory() as db:
                follow = db.get(CopyFollow, snap.id)
                if follow is None:
                    return None
                self._rollover(follow, now)
                realized_budget = float(follow.daily_stop_loss) + float(follow.today_pnl)
                # Les copies achetées mais pas encore réglées peuvent toutes perdre :
                # leur mise est une perte potentielle à retrancher du budget.
                open_exposure = float(
                    db.scalar(
                        select(func.coalesce(func.sum(CopiedTrade.stake), 0.0)).where(
                            CopiedTrade.follow_id == follow.id,
                            CopiedTrade.status == STATUS_OPEN,
                        )
                    )
                    or 0.0
                )
                budget = realized_budget - open_exposure
                if stake > budget + _RISK_EPSILON:
                    # Pause pour la journée seulement si le budget RÉALISÉ est dépassé.
                    # Si ce sont les copies en cours qui l'occupent, elles peuvent
                    # encore gagner : on saute uniquement cette copie.
                    exhausted = stake > realized_budget + _RISK_EPSILON
                    if exhausted:
                        reason = (
                            f"stop loss journalier : mise {stake:.2f} supérieure au "
                            f"budget de perte restant {max(realized_budget, 0.0):.2f}"
                        )
                    else:
                        reason = (
                            f"stop loss journalier : mise {stake:.2f} supérieure au budget "
                            f"restant {max(budget, 0.0):.2f} (copies en cours non réglées : "
                            f"{open_exposure:.2f})"
                        )
                    db.add(self._new_trade(follow, event, 0.0, STATUS_SKIPPED, reason, now))
                    if exhausted and follow.paused_reason is None:
                        follow.paused_reason = PAUSE_DAILY_STOP_LOSS
                    db.commit()
                    logger.info(
                        "Copie ignorée, mise %.2f > budget restant %.2f (dont copies en "
                        "cours %.2f) : follow=%s contrat maître=%s%s",
                        stake,
                        budget,
                        open_exposure,
                        snap.id,
                        event.contract_id,
                        " (pause stop loss journalier)" if exhausted else "",
                    )
                    return None
                trade = self._new_trade(follow, event, stake, STATUS_OPEN, None, now)
                db.add(trade)
                db.commit()
                return trade.id
        except IntegrityError:
            logger.warning(
                "Contrat maître %s déjà copié pour follow=%s", event.contract_id, snap.id
            )
        except Exception:  # noqa: BLE001
            logger.exception("Enregistrement de la copie follow=%s en erreur", snap.id)
        return None

    def _close_trade(self, trade_id: int, status: str, *, reason: Optional[str]) -> None:
        try:
            with self._session_factory() as db:
                trade = db.get(CopiedTrade, trade_id)
                if trade is not None:
                    trade.status = status
                    trade.reason = _clip(reason) if reason else None
                    trade.settled_at = self._now()
                    db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Mise à jour de la copie %s en erreur", trade_id)

    def _pause(self, follow_id: int, reason: str) -> None:
        try:
            with self._session_factory() as db:
                follow = db.get(CopyFollow, follow_id)
                if follow is not None and follow.paused_reason != reason:
                    follow.paused_reason = reason
                    db.commit()
                    logger.info("Copie en pause (%s) : follow=%s", reason, follow_id)
        except Exception:  # noqa: BLE001
            logger.exception("Mise en pause de follow=%s en erreur", follow_id)
        if reason == PAUSE_TOKEN_INVALID:
            self._close_follow(follow_id)

    # ------------------------------------------------------------------
    # Lectures utilitaires
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        return _utc(self._clock()) or _utcnow()

    @staticmethod
    def _active_follow_ids(db: Session, master_id: int) -> list[int]:
        return list(
            db.execute(
                select(CopyFollow.id)
                .where(CopyFollow.master_user_id == master_id, CopyFollow.active.is_(True))
                .order_by(CopyFollow.id)
            ).scalars()
        )

    def _follow_ids_of_master(self, master_id: int) -> list[int]:
        with self._session_factory() as db:
            master = db.get(CopyMaster, master_id)
            if master is None or not master.enabled:
                return []
            return self._active_follow_ids(db, master_id)

    @staticmethod
    def _followers_counts(db: Session, master_ids: list[int]) -> dict[int, int]:
        if not master_ids:
            return {}
        rows = db.execute(
            select(CopyFollow.master_user_id, func.count(CopyFollow.id))
            .where(CopyFollow.master_user_id.in_(master_ids), CopyFollow.active.is_(True))
            .group_by(CopyFollow.master_user_id)
        ).all()
        return {int(master_id): int(count) for master_id, count in rows}

    @staticmethod
    def _empty_stats(window_days: int) -> dict[str, Any]:
        return {"trades": 0, "win_rate": None, "pnl": 0.0, "window_days": window_days}

    def _stats_by_master(
        self, db: Session, master_ids: list[int], window_days: int
    ) -> dict[int, dict[str, Any]]:
        if not master_ids:
            return {}
        since = self._now() - timedelta(days=window_days)
        wins = func.sum(case((CopyMasterTrade.profit > 0, 1), else_=0))
        rows = db.execute(
            select(
                CopyMasterTrade.master_user_id,
                func.count(CopyMasterTrade.id),
                func.count(CopyMasterTrade.profit),
                wins,
                func.sum(CopyMasterTrade.profit),
            )
            .where(
                CopyMasterTrade.master_user_id.in_(master_ids),
                CopyMasterTrade.created_at >= since,
            )
            .group_by(CopyMasterTrade.master_user_id)
        ).all()
        stats: dict[int, dict[str, Any]] = {}
        for master_id, total, settled, won, pnl in rows:
            settled = int(settled or 0)
            stats[int(master_id)] = {
                "trades": int(total or 0),
                "win_rate": round(int(won or 0) / settled, 4) if settled else None,
                "pnl": round(float(pnl or 0.0), 2),
                "window_days": window_days,
            }
        return stats

    def _follow_out(
        self,
        follow: CopyFollow,
        master: Optional[CopyMaster],
        now: datetime,
        *,
        entitled: bool = True,
    ) -> dict[str, Any]:
        same_day = follow.pnl_day == now.date()
        paused = follow.paused_reason
        if paused == PAUSE_DAILY_STOP_LOSS and not same_day:
            paused = None  # levée au prochain trade (nouveau jour UTC)
        if paused == PAUSE_SUBSCRIPTION and entitled:
            paused = None  # levée au prochain trade (accès retrouvé)
        if paused is None and not entitled:
            paused = PAUSE_SUBSCRIPTION
        if paused is None and (master is None or not master.enabled):
            paused = PAUSE_MASTER_DISABLED
        return {
            "master_id": follow.master_user_id,
            "master_name": master.display_name if master is not None
            else f"Maître #{follow.master_user_id}",
            "account_type": follow.account_type,
            "account_currency": follow.account_currency,
            "multiplier": float(follow.multiplier),
            "max_stake": float(follow.max_stake),
            "daily_stop_loss": float(follow.daily_stop_loss),
            "active": bool(follow.active),
            "today_pnl": round(float(follow.today_pnl), 2) if same_day else 0.0,
            "paused_reason": paused,
            "created_at": _iso(follow.created_at),
        }

    @staticmethod
    def _trade_out(trade: CopiedTrade) -> dict[str, Any]:
        return {
            "id": trade.id,
            "master_contract_id": trade.master_contract_id,
            "follower_contract_id": trade.follower_contract_id,
            "symbol": trade.symbol,
            "contract_type": trade.contract_type,
            "stake": round(float(trade.stake or 0.0), 2),
            "profit": round(float(trade.profit), 2) if trade.profit is not None else None,
            "status": trade.status,
            "reason": trade.reason,
            "created_at": _iso(trade.created_at),
        }

    def _ensure_enabled(self) -> None:
        if not self.enabled:
            raise CopyTradingUnavailable("Copy trading désactivé sur ce serveur")

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Tâche de fond du copy trading en erreur", exc_info=task.exception())

    # ------------------------------------------------------------------
    # Opérations des routes (utilisateur)
    # ------------------------------------------------------------------
    def me(self, user_id: int) -> dict[str, Any]:
        now = self._now()
        with self._session_factory() as db:
            own_master = db.get(CopyMaster, user_id)
            is_master = own_master is not None and own_master.enabled
            followers = (
                self._followers_counts(db, [user_id]).get(user_id, 0)
                if own_master is not None
                else 0
            )
            follow = db.execute(
                select(CopyFollow).where(CopyFollow.follower_user_id == user_id)
            ).scalar_one_or_none()
            following = None
            if follow is not None:
                following = self._follow_out(
                    follow,
                    db.get(CopyMaster, follow.master_user_id),
                    now,
                    entitled=has_copy_access(db.get(models.User, user_id)),
                )
        return {
            "enabled": self.enabled,
            "is_master": is_master,
            "following": following,
            "followers_count": followers,
        }

    def masters_with_stats(self, window_days: int = STATS_WINDOW_DAYS) -> list[dict[str, Any]]:
        with self._session_factory() as db:
            masters = db.execute(
                select(CopyMaster)
                .join(models.User, models.User.id == CopyMaster.user_id)
                .where(CopyMaster.enabled.is_(True), models.User.active.is_(True))
            ).scalars().all()
            ids = [master.user_id for master in masters]
            stats = self._stats_by_master(db, ids, window_days)
            followers = self._followers_counts(db, ids)
            out = [
                {
                    "master_id": master.user_id,
                    "display_name": master.display_name,
                    "bio": master.bio or "",
                    "followers": followers.get(master.user_id, 0),
                    "stats": stats.get(master.user_id) or self._empty_stats(window_days),
                }
                for master in masters
            ]
        out.sort(key=lambda item: (-item["followers"], item["display_name"].lower()))
        return out

    async def validate_and_store_follow(
        self,
        follower_user_id: int,
        *,
        master_id: int,
        api_token: str,
        account_type: str,
        multiplier: float = 1.0,
        max_stake: float = 10.0,
        daily_stop_loss: float = 20.0,
    ) -> dict[str, Any]:
        """Valide le token (compte du type demandé), le chiffre, crée l'abonnement.

        Lève ValueError (paramètres, token refusé, compte introuvable),
        MasterNotFound, AlreadyFollowing ou CopyTradingUnavailable.
        """
        self._ensure_enabled()
        account = _normalize_account_type(account_type)
        _validate_limits(multiplier, max_stake, daily_stop_loss)
        token = str(api_token or "").strip()
        if not token:
            raise ValueError("Token API Deriv requis")
        if int(follower_user_id) == int(master_id):
            raise ValueError("Impossible de se suivre soi-même")
        with self._session_factory() as db:
            master = db.get(CopyMaster, master_id)
            if master is None or not master.enabled:
                raise MasterNotFound("Maître introuvable ou désactivé")
            if db.execute(
                select(CopyFollow.id).where(CopyFollow.follower_user_id == follower_user_id)
            ).first() is not None:
                raise AlreadyFollowing("Vous suivez déjà un maître")

        info = await self._probe_account(token, account)
        try:
            encrypted = token_crypto.encrypt(token)
        except token_crypto.TokenCryptoError:
            raise CopyTradingUnavailable("Chiffrement des tokens indisponible côté serveur") from None
        del token

        now = self._now()
        with self._session_factory() as db:
            follow = CopyFollow(
                follower_user_id=follower_user_id,
                master_user_id=master_id,
                encrypted_token=encrypted,
                account_type=account,
                account_id=_clip(str(info.get("account_id") or info.get("loginid") or ""), 64),
                account_currency=_clip(str(info.get("currency") or "USD"), 10),
                multiplier=float(multiplier),
                max_stake=float(max_stake),
                daily_stop_loss=float(daily_stop_loss),
                active=True,
                today_pnl=0.0,
                pnl_day=now.date(),
                paused_reason=None,
                consent_at=now,
                created_at=now,
            )
            db.add(follow)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise AlreadyFollowing("Vous suivez déjà un maître") from None
            logger.info(
                "Copie créée : suiveur=%s maître=%s compte=%s", follower_user_id, master_id, account
            )
            return self._follow_out(follow, db.get(CopyMaster, master_id), now)

    async def _probe_account(self, token: str, account_type: str) -> dict[str, Any]:
        """Connecte le token une fois pour découvrir le compte demandé, puis ferme."""
        client = self._client_factory(account_type)
        try:
            await asyncio.wait_for(client.connect(token), timeout=self.connect_timeout_s)
            return dict(client.account_info)
        except DerivError as exc:
            if exc.code == "NoAccount":
                raise ValueError(f"Aucun compte {account_type} sur ce token Deriv") from None
            if exc.code in _FATAL_AUTH_CODES:
                raise ValueError("Token Deriv invalide ou révoqué") from None
            raise ValueError(_clip(f"Deriv a refusé le token : {exc.message}", 200)) from None
        except asyncio.TimeoutError:
            raise ValueError("Deriv ne répond pas : réessayez dans un instant") from None
        except CopyTradingError:
            raise
        except Exception as exc:  # noqa: BLE001 — jamais de 5xx vers le client
            raise ValueError(f"Connexion à Deriv impossible ({type(exc).__name__})") from None
        finally:
            await self._close_client_quietly(client)

    async def update_follow(
        self,
        user_id: int,
        *,
        multiplier: Optional[float] = None,
        max_stake: Optional[float] = None,
        daily_stop_loss: Optional[float] = None,
        active: Optional[bool] = None,
    ) -> dict[str, Any]:
        self._ensure_enabled()
        _validate_limits(multiplier, max_stake, daily_stop_loss)
        now = self._now()
        with self._session_factory() as db:
            follow = db.execute(
                select(CopyFollow).where(CopyFollow.follower_user_id == user_id)
            ).scalar_one_or_none()
            if follow is None:
                raise NotFollowing("Aucun maître suivi")
            self._rollover(follow, now)
            if multiplier is not None:
                follow.multiplier = float(multiplier)
            if max_stake is not None:
                follow.max_stake = float(max_stake)
            if daily_stop_loss is not None:
                follow.daily_stop_loss = float(daily_stop_loss)
                if (
                    follow.paused_reason == PAUSE_DAILY_STOP_LOSS
                    and follow.today_pnl > -follow.daily_stop_loss
                ):
                    follow.paused_reason = None  # limite relevée : la copie reprend
            if active is not None:
                follow.active = bool(active)
            db.commit()
            follow_id = follow.id
            out = self._follow_out(
                follow,
                db.get(CopyMaster, follow.master_user_id),
                now,
                entitled=has_copy_access(db.get(models.User, user_id)),
            )
        if active is False:
            self._close_follow(follow_id)
        return out

    async def delete_follow(self, user_id: int) -> bool:
        """Supprime l'abonnement ET le token chiffré. False s'il n'existait pas."""
        with self._session_factory() as db:
            follow = db.execute(
                select(CopyFollow).where(CopyFollow.follower_user_id == user_id)
            ).scalar_one_or_none()
            if follow is None:
                return False
            follow_id = follow.id
            if db.get_bind().dialect.name == "sqlite":
                # Écrase les pages libérées : le chiffré ne traîne pas dans le fichier.
                db.execute(text("PRAGMA secure_delete = ON"))
            db.execute(
                update(CopiedTrade).where(CopiedTrade.follow_id == follow_id).values(follow_id=None)
            )
            db.delete(follow)
            db.commit()
        self._close_follow(follow_id)
        logger.info("Copie supprimée (token effacé) : suiveur=%s", user_id)
        return True

    def trades(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._session_factory() as db:
            rows = db.execute(
                select(CopiedTrade)
                .where(CopiedTrade.follower_user_id == user_id)
                .order_by(CopiedTrade.id.desc())
                .limit(limit)
            ).scalars().all()
            return [self._trade_out(trade) for trade in rows]

    async def purge_user(self, user_id: int) -> None:
        """Suppression d'un utilisateur : efface sa copie (token) et son statut maître."""
        await self.delete_follow(user_id)
        with self._session_factory() as db:
            master = db.get(CopyMaster, user_id)
            if master is not None and master.enabled:
                master.enabled = False
                db.commit()
        self._close_pool(user_id)

    # ------------------------------------------------------------------
    # Opérations des routes (admin)
    # ------------------------------------------------------------------
    def _admin_master_out(
        self, db: Session, master: CopyMaster, window_days: int = STATS_WINDOW_DAYS
    ) -> dict[str, Any]:
        user = db.get(models.User, master.user_id)
        return {
            "user_id": master.user_id,
            "email": user.email if user is not None else None,
            "name": user.name if user is not None else None,
            "display_name": master.display_name,
            "bio": master.bio or "",
            "enabled": bool(master.enabled),
            "followers": self._followers_counts(db, [master.user_id]).get(master.user_id, 0),
            "stats": self._stats_by_master(db, [master.user_id], window_days).get(master.user_id)
            or self._empty_stats(window_days),
            "created_at": _iso(master.created_at),
        }

    def admin_list_masters(self) -> list[dict[str, Any]]:
        with self._session_factory() as db:
            masters = db.execute(
                select(CopyMaster).order_by(CopyMaster.enabled.desc(), CopyMaster.display_name)
            ).scalars().all()
            return [self._admin_master_out(db, master) for master in masters]

    def admin_set_master(self, user_id: int, display_name: str, bio: str = "") -> dict[str, Any]:
        """Désigne (ou met à jour et réactive) un maître. LookupError si user absent."""
        name = _clip(str(display_name or "").strip(), 80)
        if not name:
            raise ValueError("display_name requis")
        about = _clip(str(bio or "").strip(), 500) if bio else ""
        with self._session_factory() as db:
            if db.get(models.User, user_id) is None:
                raise LookupError("Utilisateur introuvable")
            master = db.get(CopyMaster, user_id)
            if master is None:
                master = CopyMaster(
                    user_id=user_id,
                    display_name=name,
                    bio=about,
                    enabled=True,
                    created_at=self._now(),
                )
                db.add(master)
            else:
                master.display_name = name
                master.bio = about
                master.enabled = True
            db.commit()
            logger.info("Maître copy trading défini : user=%s", user_id)
            return self._admin_master_out(db, master)

    async def admin_remove_master(self, user_id: int) -> None:
        """Retire le statut de maître (désactivation) et ferme son pool."""
        with self._session_factory() as db:
            master = db.get(CopyMaster, user_id)
            if master is None:
                raise MasterNotFound("Maître introuvable")
            master.enabled = False
            db.commit()
        self._close_pool(user_id)
        logger.info("Maître copy trading retiré : user=%s", user_id)
