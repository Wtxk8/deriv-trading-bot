"""Tests du copy trading : chiffrement, calcul de mise, stop loss journalier,
isolation des suiveurs, règlement des contrats copiés et routes HTTP.

Aucun appel réseau : les clients Deriv et le BotManager sont simulés, aucun
ordre n'est jamais envoyé à Deriv.

    python -m pytest tests/test_copy_trading.py -q   (depuis backend/)
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

# Base SQLite temporaire et secret JWT AVANT tout import applicatif (le
# conftest partagé les fixe déjà s'il existe).
os.environ.setdefault(
    "TRADING_DATABASE_URL",
    "sqlite:///"
    + os.path.join(tempfile.mkdtemp(prefix="copy_trading_"), "test.db").replace("\\", "/"),
)
os.environ.setdefault("JWT_SECRET", "secret-de-test-copy-trading")

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

import auth  # noqa: E402
import copy_trading as ct  # noqa: E402
import models  # noqa: E402
import token_crypto  # noqa: E402
from database import Base, SessionLocal, engine  # noqa: E402
from deriv_client import DerivError  # noqa: E402
from models_copy import CopiedTrade, CopyFollow, CopyMasterTrade  # noqa: E402
from routers.copy_trading import router as copy_router  # noqa: E402
from trade_events import SessionEvent, TradeOpenedEvent, TradeSettledEvent  # noqa: E402

Base.metadata.create_all(bind=engine)

# Un seul hachage bcrypt pour tous les utilisateurs de test (lent).
_PASSWORD_HASH = auth.hash_password("mot-de-passe-de-test")

FOLLOW_OUT_KEYS = {
    "master_id", "master_name", "account_type", "account_currency", "multiplier",
    "max_stake", "daily_stop_loss", "active", "today_pnl", "paused_reason", "created_at",
}
TRADE_OUT_KEYS = {
    "id", "master_contract_id", "follower_contract_id", "symbol", "contract_type",
    "stake", "profit", "status", "reason", "created_at",
}


@pytest.fixture(autouse=True)
def copy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COPY_TOKEN_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("COPY_TRADING_ENABLED", "true")


# ---------------------------------------------------------------------------
# Doublures
# ---------------------------------------------------------------------------
class FakeDerivClient:
    """Imite DerivClient : comptes simulés, achats enregistrés, aucun réseau."""

    def __init__(self, deriv: "FakeDeriv", account_type: str) -> None:
        self.deriv = deriv
        self.account_type = account_type
        self.token: Optional[str] = None
        self.connected = False
        self.closed = False
        self.callbacks: dict[str, Any] = {}
        self.buys: list[dict[str, Any]] = []
        self.poc_calls: list[tuple[int, bool]] = []
        self.sent: list[dict[str, Any]] = []
        self._info: Optional[dict[str, Any]] = None

    @property
    def is_connected(self) -> bool:
        return self.connected and not self.closed

    @property
    def account_info(self) -> dict[str, Any]:
        if self._info is None:
            raise RuntimeError("Non connecté")
        return self._info

    async def connect(self, pat_token: str) -> None:
        self.token = pat_token
        accounts = self.deriv.accounts.get(pat_token)
        if accounts is None:
            raise DerivError("Unauthorized", "Token PAT invalide ou Deriv-App-ID erroné")
        account = accounts.get(self.account_type)
        if account is None:
            raise DerivError("NoAccount", f"Aucun compte {self.account_type} disponible sur ce token")
        self._info = {
            "loginid": account["account_id"],
            "account_id": account["account_id"],
            "balance": account["balance"],
            "currency": account["currency"],
            "account_type": self.account_type,
        }
        self.connected = True

    async def close(self) -> None:
        self.closed = True
        self.connected = False

    def on_subscription(self, msg_type: str, callback: Any) -> None:
        self.callbacks[msg_type] = callback

    async def buy_proposal(
        self,
        contract_type: str,
        symbol: str,
        amount: float,
        duration: int,
        duration_unit: str = "t",
        basis: str = "stake",
        currency: str = "USD",
        barrier: Optional[str] = None,
        max_price: Optional[float] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self.buys.append(
            {
                "contract_type": contract_type,
                "symbol": symbol,
                "amount": amount,
                "duration": duration,
                "duration_unit": duration_unit,
                "basis": basis,
                "currency": currency,
                "barrier": barrier,
                "max_price": max_price,
            }
        )
        error = self.deriv.buy_errors.get(self.token)
        if error is not None:
            raise error
        self.deriv.next_contract += 1
        return {"contract_id": self.deriv.next_contract, "buy_price": amount}

    async def proposal_open_contract(self, contract_id: int, subscribe: bool = True) -> dict[str, Any]:
        self.poc_calls.append((contract_id, subscribe))
        if not subscribe and contract_id in self.deriv.poc_results:
            return {"contract_id": contract_id, **self.deriv.poc_results[contract_id]}
        return {"contract_id": contract_id, "is_sold": 0}

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(payload)
        return {}

    async def push_settlement(self, contract_id: int, profit: float, sub_id: str = "sub-1") -> None:
        """Simule le message final du flux proposal_open_contract."""
        await self.callbacks["proposal_open_contract"](
            {
                "msg_type": "proposal_open_contract",
                "proposal_open_contract": {
                    "contract_id": contract_id,
                    "is_sold": 1,
                    "profit": profit,
                    "status": "won" if profit > 0 else "lost",
                },
                "subscription": {"id": sub_id},
            }
        )


class FakeDeriv:
    """Registre des comptes simulés + fabrique de clients (client_factory)."""

    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, dict[str, Any]]] = {}
        self.clients: list[FakeDerivClient] = []
        self.buy_errors: dict[str, Exception] = {}
        self.poc_results: dict[int, dict[str, Any]] = {}
        self.next_contract = 5_000_000

    def add_token(
        self, *, demo: Optional[float] = None, real: Optional[float] = None, currency: str = "USD"
    ) -> str:
        token = f"pat-{uuid.uuid4().hex}"
        accounts: dict[str, dict[str, Any]] = {}
        if demo is not None:
            accounts["demo"] = {"account_id": "DOT" + token[-6:], "balance": demo, "currency": currency}
        if real is not None:
            accounts["real"] = {"account_id": "ROT" + token[-6:], "balance": real, "currency": currency}
        self.accounts[token] = accounts
        return token

    def factory(self, account_type: str) -> FakeDerivClient:
        client = FakeDerivClient(self, account_type)
        self.clients.append(client)
        return client

    def live(self, token: str) -> list[FakeDerivClient]:
        return [client for client in self.clients if client.token == token and client.is_connected]


class FakeBotManager:
    """Expose les trois méthodes d'abonnement du BotManager et rejoue ses événements."""

    def __init__(self) -> None:
        self.opened_listeners: list[Any] = []
        self.settled_listeners: list[Any] = []
        self.session_listeners: list[Any] = []

    def add_trade_opened_listener(self, listener: Any) -> None:
        self.opened_listeners.append(listener)

    def add_trade_settled_listener(self, listener: Any) -> None:
        self.settled_listeners.append(listener)

    def add_session_listener(self, listener: Any) -> None:
        self.session_listeners.append(listener)

    async def session(self, user_id: int, kind: str, account_type: str = "demo") -> None:
        for listener in self.session_listeners:
            await listener(SessionEvent(user_id=user_id, kind=kind, account_type=account_type))

    async def opened(self, event: TradeOpenedEvent) -> None:
        for listener in self.opened_listeners:
            await listener(event)

    async def settled(self, event: TradeSettledEvent) -> None:
        for listener in self.settled_listeners:
            await listener(event)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------
def make_user(
    *,
    role: str = "user",
    trial: bool = True,
    tier: str = "free",
    expires: Optional[datetime] = None,
) -> int:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        user = models.User(
            name="Test copy",
            email=f"copy-{uuid.uuid4().hex[:12]}@test.local",
            hashed_password=_PASSWORD_HASH,
            role=role,
            active=True,
            subscription_tier=tier,
            subscription_expires_at=expires,
            trial_started_at=now if trial else now - timedelta(days=30),
        )
        db.add(user)
        db.commit()
        return user.id


def bearer(user_id: int) -> dict[str, str]:
    with SessionLocal() as db:
        token = auth.create_access_token(db.get(models.User, user_id))
    return {"Authorization": f"Bearer {token}"}


def new_service(deriv: FakeDeriv, clock: Optional[FakeClock] = None) -> tuple[ct.CopyTradingService, FakeBotManager]:
    manager = FakeBotManager()
    service = ct.CopyTradingService(
        SessionLocal,
        manager,
        client_factory=deriv.factory,
        clock=clock or FakeClock(datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)),
    )
    service.register()
    return service, manager


def make_master(service: ct.CopyTradingService, name: str = "Maître Test") -> int:
    master_id = make_user()
    service.admin_set_master(master_id, name, "Stratégie de test")
    return master_id


async def add_follower(
    service: ct.CopyTradingService,
    deriv: FakeDeriv,
    master_id: int,
    *,
    account_type: str = "demo",
    balance: float = 500.0,
    **limits: float,
) -> tuple[int, str]:
    user_id = make_user()
    token = deriv.add_token(**{account_type: balance})
    await service.validate_and_store_follow(
        user_id, master_id=master_id, api_token=token, account_type=account_type, **limits
    )
    return user_id, token


def trade_event(
    master_id: int,
    contract_id: int,
    *,
    stake: float = 1.0,
    balance: float = 1000.0,
    account_type: str = "demo",
    duration: int = 5,
) -> TradeOpenedEvent:
    return TradeOpenedEvent(
        user_id=master_id,
        contract_id=contract_id,
        contract_type="CALLE",
        symbol="R_75",
        stake=stake,
        duration=duration,
        duration_unit="t",
        barrier=None,
        currency="USD",
        account_type=account_type,
        account_balance=balance,
    )


def copied(user_id: int) -> list[CopiedTrade]:
    with SessionLocal() as db:
        return list(
            db.execute(
                select(CopiedTrade)
                .where(CopiedTrade.follower_user_id == user_id)
                .order_by(CopiedTrade.id)
            ).scalars()
        )


def follow_of(user_id: int) -> Optional[CopyFollow]:
    with SessionLocal() as db:
        return db.execute(
            select(CopyFollow).where(CopyFollow.follower_user_id == user_id)
        ).scalar_one_or_none()


async def let_tasks_run() -> None:
    await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# Chiffrement
# ---------------------------------------------------------------------------
def test_token_crypto_round_trip_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "pat-secret-0123456789"
    encrypted = token_crypto.encrypt(secret)
    assert encrypted != secret and secret not in encrypted
    assert token_crypto.decrypt(encrypted) == secret

    # Rotation : nouvelle clé en tête, l'ancienne déchiffre encore.
    old_key = os.environ["COPY_TOKEN_KEY"]
    monkeypatch.setenv("COPY_TOKEN_KEY", f"{Fernet.generate_key().decode()},{old_key}")
    assert token_crypto.decrypt(encrypted) == secret

    monkeypatch.setenv("COPY_TOKEN_KEY", Fernet.generate_key().decode())
    with pytest.raises(token_crypto.TokenDecryptError):
        token_crypto.decrypt(encrypted)

    monkeypatch.setenv("COPY_TOKEN_KEY", "cle-invalide-tres-secrete")
    assert not token_crypto.is_configured()
    with pytest.raises(token_crypto.TokenCryptoNotConfigured) as excinfo:
        token_crypto.encrypt(secret)
    assert "cle-invalide-tres-secrete" not in str(excinfo.value)

    monkeypatch.delenv("COPY_TOKEN_KEY")
    assert not token_crypto.is_configured()
    assert not ct.is_feature_enabled()


def test_follow_stores_only_encrypted_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario() -> None:
        deriv = FakeDeriv()
        service, _ = new_service(deriv)
        master_id = make_master(service)
        user_id, token = await add_follower(service, deriv, master_id, balance=321.0)

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM copy_follows WHERE follower_user_id = :u"), {"u": user_id}
            ).mappings().one()
        assert all(token not in str(value) for value in row.values())
        assert token_crypto.decrypt(row["encrypted_token"]) == token
        assert row["account_currency"] == "USD" and row["account_type"] == "demo"
        # La connexion de validation est refermée aussitôt.
        assert deriv.clients and all(client.closed for client in deriv.clients)
        await service.shutdown()
        return token

    token = asyncio.run(scenario())
    assert token not in caplog.text


# ---------------------------------------------------------------------------
# Calcul de mise
# ---------------------------------------------------------------------------
def test_compute_copy_stake() -> None:
    stake = ct.compute_copy_stake
    assert stake(1.0, 1000.0, 500.0, 1.0, 10.0) == 0.5  # proportionnel au solde
    assert stake(1.0, 1000.0, 500.0, 2.0, 10.0) == 1.0  # multiplicateur
    assert stake(2.0, 1000.0, 3000.0, 1.0, 10.0) == 6.0
    assert stake(1.0, 3000.0, 2000.0, 1.0, 10.0) == 0.67  # arrondi à 2 décimales
    assert stake(1.0, 1000.0, 100.0, 1.0, 10.0) == 0.35  # plancher Deriv
    assert stake(5.0, 100.0, 10_000.0, 1.0, 10.0) == 10.0  # plafond max_stake
    assert stake(1.5, 1000.0, None, 2.0, 10.0) == 3.0  # solde suiveur inconnu
    assert stake(1.5, 1000.0, 0.0, 1.0, 10.0) == 1.5  # solde suiveur nul
    assert stake(1.5, 0.0, 800.0, 1.0, 10.0) == 1.5  # solde maître nul


# ---------------------------------------------------------------------------
# Réplication
# ---------------------------------------------------------------------------
def test_copy_flow_session_buy_and_settlement(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario() -> str:
        deriv = FakeDeriv()
        service, manager = new_service(deriv)
        master_id = make_master(service, "Alpha")
        follower_id, token = await add_follower(
            service, deriv, master_id, balance=500.0, multiplier=2.0, max_stake=10.0
        )
        follow_id = follow_of(follower_id).id

        # Début de session du maître : connexion du suiveur ouverte d'avance.
        await manager.session(master_id, "started", "demo")
        live = deriv.live(token)
        assert len(live) == 1
        client = live[0]

        await manager.opened(trade_event(master_id, 111, stake=1.0, balance=1000.0))
        # 1.0 x (500 / 1000) x 2 = 1.0
        assert client.buys == [
            {
                "contract_type": "CALLE",
                "symbol": "R_75",
                "amount": 1.0,
                "duration": 5,
                "duration_unit": "t",
                "basis": "stake",
                "currency": "USD",
                "barrier": None,
                "max_price": 1.0,
            }
        ]
        [trade] = copied(follower_id)
        assert trade.status == "open" and trade.stake == 1.0
        assert trade.master_contract_id == 111
        assert trade.follower_contract_id == deriv.next_contract
        assert (trade.follower_contract_id, True) in client.poc_calls

        # Règlement via le flux proposal_open_contract.
        await client.push_settlement(trade.follower_contract_id, 0.95)
        [trade] = copied(follower_id)
        assert trade.status == "won" and trade.profit == 0.95 and trade.settled_at is not None
        assert follow_of(follower_id).today_pnl == 0.95
        assert service._conns[follow_id].balance == 500.95
        await let_tasks_run()
        assert {"forget": "sub-1"} in client.sent
        # Un second message pour le même contrat est ignoré.
        await client.push_settlement(trade.follower_contract_id, 0.95)
        assert follow_of(follower_id).today_pnl == 0.95

        # Règlement côté maître -> stats publiques.
        await manager.settled(TradeSettledEvent(user_id=master_id, contract_id=111, profit=0.95, payout=1.95))
        with SessionLocal() as db:
            master_trade = db.execute(
                select(CopyMasterTrade).where(
                    CopyMasterTrade.master_user_id == master_id, CopyMasterTrade.contract_id == 111
                )
            ).scalar_one()
            assert master_trade.profit == 0.95 and master_trade.settled_at is not None
        [entry] = [m for m in service.masters_with_stats() if m["master_id"] == master_id]
        assert entry["followers"] == 1
        assert entry["stats"] == {"trades": 1, "win_rate": 1.0, "pnl": 0.95, "window_days": 30}

        me = service.me(follower_id)
        assert me["enabled"] is True and set(me["following"]) == FOLLOW_OUT_KEYS
        assert me["following"]["today_pnl"] == 0.95
        assert me["following"]["master_name"] == "Alpha"
        assert service.me(master_id)["is_master"] is True
        assert service.me(master_id)["followers_count"] == 1
        [out] = service.trades(follower_id)
        assert set(out) == TRADE_OUT_KEYS and out["status"] == "won" and out["profit"] == 0.95

        # Événement rejoué : pas de second achat (idempotence).
        await manager.opened(trade_event(master_id, 111))
        assert len(client.buys) == 1

        # Fin de session : pool fermé.
        await manager.session(master_id, "stopped", "demo")
        await let_tasks_run()
        assert client.closed
        await service.shutdown()
        return token

    token = asyncio.run(scenario())
    assert token not in caplog.text


def test_settlement_watchdog_polls_when_stream_is_silent() -> None:
    async def scenario() -> None:
        deriv = FakeDeriv()
        service, manager = new_service(deriv)
        service.settle_grace_s = 0.0
        service.settle_poll_interval_s = 0.0
        master_id = make_master(service)
        follower_id, token = await add_follower(service, deriv, master_id, balance=1000.0)

        # Connexion ouverte à la volée (pas de SessionEvent "started").
        deriv.poc_results[deriv.next_contract + 1] = {"is_sold": 1, "profit": -1.0}
        await manager.opened(trade_event(master_id, 301, duration=0))
        await let_tasks_run()
        [trade] = copied(follower_id)
        assert trade.status == "lost" and trade.profit == -1.0
        assert follow_of(follower_id).today_pnl == -1.0
        [client] = deriv.live(token)
        assert (trade.follower_contract_id, False) in client.poc_calls
        await service.shutdown()
        assert client.closed

    asyncio.run(scenario())


def test_daily_stop_loss_then_resume_next_day() -> None:
    async def scenario() -> None:
        deriv = FakeDeriv()
        clock = FakeClock(datetime(2026, 9, 14, 22, 0, tzinfo=timezone.utc))
        service, manager = new_service(deriv, clock)
        master_id = make_master(service)
        follower_id, token = await add_follower(
            service, deriv, master_id, balance=1000.0, daily_stop_loss=1.0
        )

        await manager.opened(trade_event(master_id, 201))
        [client] = deriv.live(token)
        [trade] = copied(follower_id)
        await client.push_settlement(trade.follower_contract_id, -1.0)
        follow = follow_of(follower_id)
        assert follow.today_pnl == -1.0 and follow.paused_reason == "daily_stop_loss"
        assert service.me(follower_id)["following"]["paused_reason"] == "daily_stop_loss"

        # Même jour : trade ignoré, aucun achat.
        await manager.opened(trade_event(master_id, 202))
        assert len(client.buys) == 1
        skipped = copied(follower_id)[-1]
        assert skipped.status == "skipped" and skipped.master_contract_id == 202
        assert skipped.reason == "stop loss journalier atteint"

        # Lendemain (UTC) : compteur remis à zéro, la copie reprend.
        clock.advance(hours=3)
        following = service.me(follower_id)["following"]
        assert following["paused_reason"] is None and following["today_pnl"] == 0.0
        await manager.opened(trade_event(master_id, 203))
        assert len(client.buys) == 2
        assert [t.status for t in copied(follower_id)] == ["lost", "skipped", "open"]
        follow = follow_of(follower_id)
        assert follow.paused_reason is None and follow.today_pnl == 0.0
        assert follow.pnl_day == date(2026, 9, 15)
        await service.shutdown()

    asyncio.run(scenario())


def test_stake_above_remaining_daily_budget_is_skipped_then_resumes_next_day(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        caplog.set_level("INFO", logger="copy_trading")
        deriv = FakeDeriv()
        clock = FakeClock(datetime(2026, 9, 14, 22, 0, tzinfo=timezone.utc))
        service, manager = new_service(deriv, clock)
        master_id = make_master(service)
        # Soldes égaux, multiplicateur 1 : mise suiveur = mise maître.
        follower_id, token = await add_follower(
            service, deriv, master_id, balance=1000.0, daily_stop_loss=3.0, max_stake=50.0
        )

        await manager.opened(trade_event(master_id, 401, stake=1.0))
        [client] = deriv.live(token)
        [trade] = copied(follower_id)
        await client.push_settlement(trade.follower_contract_id, -1.0)
        follow = follow_of(follower_id)
        assert follow.today_pnl == -1.0 and follow.paused_reason is None

        # Budget restant 3 - 1 = 2.00 < mise 2.50 : aucun achat, copie ignorée.
        await manager.opened(trade_event(master_id, 402, stake=2.5))
        assert len(client.buys) == 1
        skipped = copied(follower_id)[-1]
        assert skipped.status == "skipped" and skipped.master_contract_id == 402
        assert skipped.stake == 0.0
        assert skipped.reason == (
            "stop loss journalier : mise 2.50 supérieure au budget de perte restant 2.00"
        )
        follow = follow_of(follower_id)
        assert follow.paused_reason == "daily_stop_loss" and follow.today_pnl == -1.0
        assert service.me(follower_id)["following"]["paused_reason"] == "daily_stop_loss"
        assert token not in caplog.text

        # Même jour : la pause tient, même pour une petite mise.
        await manager.opened(trade_event(master_id, 403, stake=1.0))
        assert len(client.buys) == 1
        assert copied(follower_id)[-1].reason == "stop loss journalier atteint"

        # Lendemain (UTC) : reprise ; mise égale au budget (3.00) autorisée.
        clock.advance(hours=3)
        assert service.me(follower_id)["following"]["paused_reason"] is None
        await manager.opened(trade_event(master_id, 404, stake=3.0))
        assert len(client.buys) == 2
        assert [t.status for t in copied(follower_id)] == ["lost", "skipped", "skipped", "open"]
        assert copied(follower_id)[-1].stake == 3.0
        follow = follow_of(follower_id)
        assert follow.paused_reason is None and follow.today_pnl == 0.0
        await service.shutdown()

    asyncio.run(scenario())


def test_open_copies_count_against_daily_budget_without_pausing() -> None:
    async def scenario() -> None:
        deriv = FakeDeriv()
        clock = FakeClock(datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))
        service, manager = new_service(deriv, clock)
        master_id = make_master(service)
        follower_id, token = await add_follower(
            service, deriv, master_id, balance=1000.0, daily_stop_loss=3.0, max_stake=50.0
        )

        # Copie de 2.50 achetée, pas encore réglée : perte potentielle de 2.50.
        await manager.opened(trade_event(master_id, 501, stake=2.5))
        [client] = deriv.live(token)
        [first] = copied(follower_id)
        assert first.status == "open" and len(client.buys) == 1

        # Budget réalisé 3.00, mais 2.50 déjà engagés : reste 0.50 < mise 1.00.
        await manager.opened(trade_event(master_id, 502, stake=1.0))
        assert len(client.buys) == 1
        skipped = copied(follower_id)[-1]
        assert skipped.status == "skipped" and skipped.master_contract_id == 502
        assert "copies en cours non réglées : 2.50" in skipped.reason
        # Les copies en cours peuvent encore gagner : pas de pause pour la journée.
        assert follow_of(follower_id).paused_reason is None

        # La copie en cours se règle gagnante : le budget est libéré.
        await client.push_settlement(first.follower_contract_id, 2.2)
        await manager.opened(trade_event(master_id, 503, stake=1.0))
        assert len(client.buys) == 2
        assert copied(follower_id)[-1].status == "open"
        await service.shutdown()

    asyncio.run(scenario())


def test_demo_master_is_not_copied_to_real_follower() -> None:
    async def scenario() -> None:
        deriv = FakeDeriv()
        service, manager = new_service(deriv)
        master_id = make_master(service)
        real_id, real_token = await add_follower(service, deriv, master_id, account_type="real", balance=200.0)
        demo_id, demo_token = await add_follower(service, deriv, master_id, account_type="demo", balance=200.0)

        await manager.session(master_id, "started", "demo")
        assert deriv.live(real_token) == []  # inutile d'ouvrir le compte réel
        assert len(deriv.live(demo_token)) == 1

        await manager.opened(trade_event(master_id, 401, account_type="demo"))
        [skipped] = copied(real_id)
        assert skipped.status == "skipped" and skipped.reason == "maitre en demo"
        assert all(not client.buys for client in deriv.clients if client.token == real_token)
        [bought] = copied(demo_id)
        assert bought.status == "open"
        await service.shutdown()

    asyncio.run(scenario())


def test_failing_follower_is_isolated() -> None:
    async def scenario() -> None:
        deriv = FakeDeriv()
        service, manager = new_service(deriv)
        master_id = make_master(service)
        ok_id, ok_token = await add_follower(service, deriv, master_id, balance=1000.0)
        poor_id, poor_token = await add_follower(service, deriv, master_id, balance=1000.0)
        revoked_id, revoked_token = await add_follower(service, deriv, master_id, balance=1000.0)
        deriv.buy_errors[poor_token] = DerivError("InsufficientBalance", "Solde insuffisant")
        deriv.accounts.pop(revoked_token)  # token révoqué côté Deriv

        # Aucun "started" : connexions ouvertes à la volée ; le listener ne lève pas.
        await manager.opened(trade_event(master_id, 501))
        assert copied(ok_id)[0].status == "open"
        [poor] = copied(poor_id)
        assert poor.status == "failed" and "Solde insuffisant" in poor.reason
        [revoked] = copied(revoked_id)
        assert revoked.status == "failed" and revoked.follower_contract_id is None
        assert follow_of(revoked_id).paused_reason == "token_invalid"

        # Trade suivant : le token révoqué n'est plus tenté, les autres si.
        await manager.opened(trade_event(master_id, 502))
        assert [t.status for t in copied(ok_id)] == ["open", "open"]
        assert [t.status for t in copied(poor_id)] == ["failed", "failed"]
        assert len(copied(revoked_id)) == 1
        assert service.me(revoked_id)["following"]["paused_reason"] == "token_invalid"
        await service.shutdown()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Routes HTTP
# ---------------------------------------------------------------------------
def make_client(service: Optional[ct.CopyTradingService]) -> TestClient:
    app = FastAPI()
    app.include_router(copy_router)
    if service is not None:
        app.state.copy_service = service
    return TestClient(app)


def follow_body(master_id: int, token: str, **overrides: Any) -> dict[str, Any]:
    body = {"master_id": master_id, "api_token": token, "account_type": "demo", "consent": True}
    body.update(overrides)
    return body


def test_routes_require_auth_and_enabled_service(monkeypatch: pytest.MonkeyPatch) -> None:
    deriv = FakeDeriv()
    service = ct.CopyTradingService(SessionLocal, FakeBotManager(), client_factory=deriv.factory)
    with make_client(service) as client:
        assert client.get("/copy/me").status_code == 401
        assert client.get("/copy/masters").status_code == 401
        assert client.post("/copy/follow", json=follow_body(1, "x")).status_code == 401
        assert client.delete("/copy/follow").status_code == 401
        assert client.get("/admin/copy/masters").status_code == 401

    user_headers = bearer(make_user())
    admin_headers = bearer(make_user(role="admin"))
    monkeypatch.setenv("COPY_TRADING_ENABLED", "false")
    disabled = ct.CopyTradingService(SessionLocal, FakeBotManager(), client_factory=deriv.factory)
    assert disabled.enabled is False
    for candidate in (disabled, None):
        with make_client(candidate) as client:
            response = client.get("/copy/me", headers=user_headers)
            assert response.status_code == 200
            assert response.json() == {
                "enabled": False, "is_master": False, "following": None, "followers_count": 0,
            }
            assert client.get("/copy/masters", headers=user_headers).status_code == 503
            assert client.post(
                "/copy/follow", json=follow_body(1, "x"), headers=user_headers
            ).status_code == 503
            assert client.patch("/copy/follow", json={"active": False}, headers=user_headers).status_code == 503
            assert client.delete("/copy/follow", headers=user_headers).status_code == 503
            assert client.get("/copy/trades", headers=user_headers).status_code == 503
            assert client.get("/admin/copy/masters", headers=admin_headers).status_code == 503

    # Drapeau actif mais clé absente : toujours désactivé.
    monkeypatch.setenv("COPY_TRADING_ENABLED", "true")
    monkeypatch.delenv("COPY_TOKEN_KEY")
    assert ct.CopyTradingService(SessionLocal, FakeBotManager()).enabled is False


def test_follow_route_errors_and_lifecycle() -> None:
    deriv = FakeDeriv()
    service = ct.CopyTradingService(SessionLocal, FakeBotManager(), client_factory=deriv.factory)
    admin_headers = bearer(make_user(role="admin"))
    master_id = make_user()
    retired_id = make_user()
    follower_id = make_user()
    headers = bearer(follower_id)
    token = deriv.add_token(demo=250.0)

    with make_client(service) as client:
        for uid, name in ((master_id, "Alpha"), (retired_id, "Retraité")):
            response = client.post(
                "/admin/copy/masters",
                json={"user_id": uid, "display_name": name, "bio": "Scalping R_75"},
                headers=admin_headers,
            )
            assert response.status_code == 200, response.text
        assert client.delete(f"/admin/copy/masters/{retired_id}", headers=admin_headers).status_code == 204

        # 402 : ni essai ni premium.
        expired_headers = bearer(make_user(trial=False))
        assert client.post("/copy/follow", json=follow_body(master_id, token), headers=expired_headers).status_code == 402
        # 422 : consentement absent ou faux ; paramètres hors bornes.
        assert client.post("/copy/follow", json=follow_body(master_id, token, consent=False), headers=headers).status_code == 422
        assert client.post("/copy/follow", json=follow_body(master_id, token, multiplier=50), headers=headers).status_code == 422
        # 400 : se suivre soi-même.
        assert client.post("/copy/follow", json=follow_body(master_id, token), headers=bearer(master_id)).status_code == 400
        # 404 : maître inexistant ou désactivé.
        assert client.post("/copy/follow", json=follow_body(987_654_321, token), headers=headers).status_code == 404
        assert client.post("/copy/follow", json=follow_body(retired_id, token), headers=headers).status_code == 404
        # 400 : token refusé, ou pas de compte du type demandé.
        response = client.post("/copy/follow", json=follow_body(master_id, "pat-inconnu"), headers=headers)
        assert response.status_code == 400 and "invalide" in response.json()["detail"]
        response = client.post("/copy/follow", json=follow_body(master_id, token, account_type="real"), headers=headers)
        assert response.status_code == 400 and "real" in response.json()["detail"]
        assert follow_of(follower_id) is None

        # Succès.
        response = client.post("/copy/follow", json=follow_body(master_id, token), headers=headers)
        assert response.status_code == 200, response.text
        data = response.json()
        assert set(data) == FOLLOW_OUT_KEYS
        assert data["master_id"] == master_id and data["master_name"] == "Alpha"
        assert data["account_type"] == "demo" and data["account_currency"] == "USD"
        assert (data["multiplier"], data["max_stake"], data["daily_stop_loss"]) == (1.0, 10.0, 20.0)
        assert data["active"] is True and data["today_pnl"] == 0.0 and data["paused_reason"] is None
        assert token not in response.text

        # 409 : déjà suiveur.
        assert client.post("/copy/follow", json=follow_body(master_id, token), headers=headers).status_code == 409

        # Premium actif sans essai : autorisé.
        premium_id = make_user(trial=False, tier="premium", expires=datetime.now(timezone.utc) + timedelta(days=30))
        response = client.post(
            "/copy/follow", json=follow_body(master_id, deriv.add_token(demo=100.0)), headers=bearer(premium_id)
        )
        assert response.status_code == 200, response.text

        me = client.get("/copy/me", headers=headers).json()
        assert me["enabled"] is True and me["is_master"] is False
        assert me["following"]["master_id"] == master_id
        master_me = client.get("/copy/me", headers=bearer(master_id)).json()
        assert master_me["is_master"] is True and master_me["followers_count"] == 2

        masters = client.get("/copy/masters", headers=headers).json()
        [alpha] = [m for m in masters if m["master_id"] == master_id]
        assert alpha["display_name"] == "Alpha" and alpha["followers"] == 2
        assert alpha["stats"] == {"trades": 0, "win_rate": None, "pnl": 0.0, "window_days": 30}
        assert all(m["master_id"] != retired_id for m in masters)

        response = client.patch("/copy/follow", json={"multiplier": 2.5, "active": False}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["multiplier"] == 2.5 and response.json()["active"] is False
        assert client.patch("/copy/follow", json={"max_stake": 5000}, headers=headers).status_code == 422
        assert client.get("/copy/trades?limit=10", headers=headers).json() == []

        # DELETE : l'abonnement ET le token chiffré disparaissent.
        encrypted = follow_of(follower_id).encrypted_token
        assert client.delete("/copy/follow", headers=headers).status_code == 204
        assert follow_of(follower_id) is None
        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM copy_follows WHERE encrypted_token = :e"), {"e": encrypted}
            ).scalar_one()
        assert count == 0
        assert client.get("/copy/me", headers=headers).json()["following"] is None
        assert client.patch("/copy/follow", json={"active": True}, headers=headers).status_code == 404
        assert client.delete("/copy/follow", headers=headers).status_code == 204


def test_admin_master_routes_are_admin_only() -> None:
    service = ct.CopyTradingService(SessionLocal, FakeBotManager(), client_factory=FakeDeriv().factory)
    user_id = make_user()
    user_headers = bearer(user_id)
    admin_headers = bearer(make_user(role="admin"))
    body = {"user_id": user_id, "display_name": "Bêta", "bio": ""}

    with make_client(service) as client:
        assert client.get("/admin/copy/masters", headers=user_headers).status_code == 403
        assert client.post("/admin/copy/masters", json=body, headers=user_headers).status_code == 403
        assert client.delete(f"/admin/copy/masters/{user_id}", headers=user_headers).status_code == 403
        assert service.me(user_id)["is_master"] is False

        assert client.post(
            "/admin/copy/masters", json={**body, "user_id": 987_654_321}, headers=admin_headers
        ).status_code == 404
        assert client.delete("/admin/copy/masters/987654321", headers=admin_headers).status_code == 404

        response = client.post("/admin/copy/masters", json=body, headers=admin_headers)
        assert response.status_code == 200, response.text
        assert response.json()["enabled"] is True and response.json()["display_name"] == "Bêta"
        listed = client.get("/admin/copy/masters", headers=admin_headers).json()
        assert any(m["user_id"] == user_id and m["enabled"] for m in listed)
        assert any(m["master_id"] == user_id for m in client.get("/copy/masters", headers=user_headers).json())

        assert client.delete(f"/admin/copy/masters/{user_id}", headers=admin_headers).status_code == 204
        listed = client.get("/admin/copy/masters", headers=admin_headers).json()
        assert any(m["user_id"] == user_id and not m["enabled"] for m in listed)
        assert all(m["master_id"] != user_id for m in client.get("/copy/masters", headers=user_headers).json())
