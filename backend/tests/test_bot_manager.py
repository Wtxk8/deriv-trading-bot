"""Tests du bot multi-utilisateurs.

Couvre : BotManager (isolation, verrous, relais d'événements, libération),
routes /api/bot/* et /ws/bot/status (JWT, 402, 409, 400), événements émis par
BotEngine et découverte STRICTE du compte Deriv (_discover_account).

Aucun appel réseau et aucun ordre : moteurs, client Deriv et transport HTTP
sont remplacés par des faux injectés.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import auth
import bot_engine
import deriv_client
from bot_engine import BotEngine, BotState, StrategyType
from bot_manager import BotManager
from deriv_client import DerivClient, DerivError
from routers import bot as bot_router
from trade_events import SessionEvent, TradeOpenedEvent, TradeSettledEvent

START_PARAMS: dict[str, Any] = {
    "api_token": "faux-token",
    "symbol": "R_75",
    "stake": 1.0,
    "stop_loss": 10.0,
    "take_profit": 10.0,
    "strategy_type": "RISE_FALL",
    "account_type": "demo",
}

START_BODY: dict[str, Any] = dict(START_PARAMS)


def run(coro: Coroutine[Any, Any, Any], timeout: float = 10.0) -> Any:
    """Exécute un scénario asynchrone avec un garde-fou de durée."""
    return asyncio.run(asyncio.wait_for(coro, timeout))


async def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("Condition non atteinte dans le délai imparti")
        await asyncio.sleep(0.01)


# ======================================================================
# Faux moteur injecté dans BotManager
# ======================================================================
class FakeEngine:
    """Même interface que BotEngine, sans aucune connexion à Deriv."""

    def __init__(
        self,
        *,
        user_id: int,
        account_type: str,
        on_trade_opened: Any,
        on_trade_settled: Any,
        on_session: Any,
        start_error: BaseException | None = None,
        start_gate: asyncio.Event | None = None,
    ) -> None:
        self.user_id = user_id
        self.account_type = account_type
        self.on_trade_opened = on_trade_opened
        self.on_trade_settled = on_trade_settled
        self.on_session = on_session
        self.start_error = start_error
        self.start_gate = start_gate
        self.state = BotState.STOPPED.value
        self.start_kwargs: dict[str, Any] = {}
        self.stop_calls = 0
        self._open = False

    async def start(
        self,
        api_token: str,
        symbol: str,
        stake: float,
        stop_loss: float,
        take_profit: float,
        strategy_type: Any = StrategyType.RISE_FALL,
    ) -> None:
        self.start_kwargs = {
            "api_token": api_token,
            "symbol": symbol,
            "stake": stake,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "strategy_type": strategy_type,
        }
        if self.start_gate is not None:
            await self.start_gate.wait()
        if self.start_error is not None:
            raise self.start_error
        self.state = BotState.RUNNING.value
        self._open = True
        await self.on_session(
            SessionEvent(user_id=self.user_id, kind="started", account_type=self.account_type)
        )

    async def stop(self) -> None:
        self.stop_calls += 1
        if self.state in (BotState.RUNNING.value, BotState.PAUSED.value):
            self.state = BotState.STOPPED.value
        await self._close()

    async def finish(self, state: str) -> None:
        """Simule une fin de session autonome (SL/TP atteint, erreur)."""
        self.state = state
        await self._close()

    async def _close(self) -> None:
        if self._open:
            self._open = False
            await self.on_session(
                SessionEvent(user_id=self.user_id, kind="stopped", account_type=self.account_type)
            )

    def get_status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "account_type": self.account_type,
            "symbol": self.start_kwargs.get("symbol", ""),
            "stake": self.start_kwargs.get("stake", 0.0),
            "pnl": 0.0,
            "trades_total": 0,
            "last_trades": [],
        }


class FakeEngineFactory:
    """Fabrique injectée dans BotManager ; garde chaque moteur créé."""

    def __init__(self) -> None:
        self.engines: list[FakeEngine] = []
        self.start_errors: dict[int, BaseException] = {}
        self.start_gates: dict[int, asyncio.Event] = {}

    def __call__(self, **kwargs: Any) -> FakeEngine:
        user_id = kwargs["user_id"]
        engine = FakeEngine(
            **kwargs,
            start_error=self.start_errors.pop(user_id, None),
            start_gate=self.start_gates.get(user_id),
        )
        self.engines.append(engine)
        return engine

    def for_user(self, user_id: int) -> list[FakeEngine]:
        return [engine for engine in self.engines if engine.user_id == user_id]


# ======================================================================
# BotManager
# ======================================================================
def test_manager_isole_les_sessions_par_utilisateur() -> None:
    async def scenario() -> None:
        factory = FakeEngineFactory()
        manager = BotManager(engine_factory=factory)

        await manager.start(1, **{**START_PARAMS, "symbol": "R_75"})
        await manager.start(2, **{**START_PARAMS, "symbol": "BOOM1000", "account_type": "real"})
        engine_a, engine_b = manager.get(1), manager.get(2)

        assert engine_a is not engine_b
        assert manager.is_active(1) and manager.is_active(2)
        assert (engine_a.user_id, engine_a.account_type) == (1, "demo")
        assert (engine_b.user_id, engine_b.account_type) == (2, "real")
        assert manager.status(1)["symbol"] == "R_75"
        assert manager.status(2)["symbol"] == "BOOM1000"
        assert manager.status(2)["account_type"] == "real"

        # L'arrêt de A n'affecte pas B.
        stopped = await manager.stop(1)
        assert stopped["state"] == "STOPPED"
        assert manager.get(1) is None and not manager.is_active(1)
        assert manager.status(1)["state"] == "STOPPED"
        assert manager.is_active(2)
        assert engine_b.stop_calls == 0
        assert manager.status(2)["state"] == "RUNNING"

        # Arrêt serveur : toutes les sessions restantes sont coupées.
        await manager.shutdown()
        assert engine_b.stop_calls == 1
        assert not manager.is_active(2)
        with pytest.raises(RuntimeError):
            await manager.start(3, **START_PARAMS)

    run(scenario())


def test_manager_refuse_un_double_start_du_meme_utilisateur() -> None:
    async def scenario() -> None:
        factory = FakeEngineFactory()
        manager = BotManager(engine_factory=factory)

        await manager.start(1, **START_PARAMS)
        with pytest.raises(RuntimeError):
            await manager.start(1, **START_PARAMS)
        assert len(factory.for_user(1)) == 1

        # Un autre utilisateur démarre sans problème.
        await manager.start(2, **START_PARAMS)
        assert manager.is_active(2)

        # Après arrêt : nouvelle session sur un moteur neuf.
        first = manager.get(1)
        await manager.stop(1)
        await manager.start(1, **START_PARAMS)
        assert manager.get(1) is not first
        assert len(factory.for_user(1)) == 2
        await manager.shutdown()

    run(scenario())


def test_manager_verrou_par_utilisateur_et_jamais_global() -> None:
    async def scenario() -> None:
        factory = FakeEngineFactory()
        manager = BotManager(engine_factory=factory)
        gate = asyncio.Event()
        factory.start_gates[1] = gate

        # A reste bloqué pendant sa « connexion à Deriv »...
        start_a1 = asyncio.create_task(manager.start(1, **START_PARAMS))
        start_a2 = asyncio.create_task(manager.start(1, **START_PARAMS))
        await asyncio.sleep(0.05)
        assert not start_a1.done() and not start_a2.done()
        assert manager.status(1)["state"] == "STOPPED"

        # ... sans bloquer B.
        await asyncio.wait_for(manager.start(2, **START_PARAMS), timeout=1.0)
        assert manager.is_active(2)

        # Les deux démarrages de A sont sérialisés : un seul réussit.
        gate.set()
        results = await asyncio.gather(start_a1, start_a2, return_exceptions=True)
        assert isinstance(results[0], dict) and results[0]["state"] == "RUNNING"
        assert isinstance(results[1], RuntimeError)
        assert len(factory.for_user(1)) == 1
        await manager.shutdown()

    run(scenario())


def test_manager_relaie_les_evenements_et_isole_les_abonnes() -> None:
    async def scenario() -> dict[str, list[Any]]:
        manager = BotManager(engine_factory=FakeEngineFactory())
        received: dict[str, list[Any]] = {"opened": [], "settled": [], "session": []}

        async def cassé(event: Any) -> None:
            raise RuntimeError("abonné en panne")

        async def on_opened(event: TradeOpenedEvent) -> None:
            received["opened"].append(event)

        async def on_settled(event: TradeSettledEvent) -> None:
            received["settled"].append(event)

        async def on_session(event: SessionEvent) -> None:
            received["session"].append(event)

        # L'abonné en panne est placé avant ET après les abonnés sains.
        manager.add_trade_opened_listener(cassé)
        manager.add_trade_opened_listener(on_opened)
        manager.add_trade_settled_listener(on_settled)
        manager.add_trade_settled_listener(cassé)
        manager.add_session_listener(cassé)
        manager.add_session_listener(on_session)

        await manager.start(5, **START_PARAMS)
        engine = manager.get(5)
        opened = TradeOpenedEvent(
            user_id=5,
            contract_id=42,
            contract_type="CALLE",
            symbol="R_75",
            stake=1.0,
            duration=5,
            duration_unit="t",
            barrier=None,
            currency="USD",
            account_type="demo",
            account_balance=1000.0,
        )
        settled = TradeSettledEvent(user_id=5, contract_id=42, profit=0.95, payout=1.95)
        # Le moteur n'est jamais atteint par l'exception de l'abonné en panne.
        await engine.on_trade_opened(opened)
        await engine.on_trade_settled(settled)
        await manager.stop(5)
        await manager.shutdown()
        received["opened_expected"] = [opened]
        received["settled_expected"] = [settled]
        return received

    received = run(scenario())
    assert received["opened"] == received["opened_expected"]
    assert received["settled"] == received["settled_expected"]
    assert [event.kind for event in received["session"]] == ["started", "stopped"]
    assert all(event.user_id == 5 for event in received["session"])


def test_manager_libere_un_moteur_termine_de_lui_meme() -> None:
    async def scenario() -> None:
        manager = BotManager(engine_factory=FakeEngineFactory())
        await manager.start(3, **START_PARAMS)
        engine = manager.get(3)

        await engine.finish(BotState.TAKE_PROFIT_REACHED.value)
        await wait_until(lambda: manager.get(3) is None)
        assert engine.stop_calls == 1
        assert not manager.is_active(3)
        # Le dernier statut reste consultable jusqu'au prochain démarrage.
        assert manager.status(3)["state"] == "TAKE_PROFIT_REACHED"

        await manager.start(3, **START_PARAMS)
        assert manager.get(3) is not engine
        assert manager.status(3)["state"] == "RUNNING"
        await manager.shutdown()

    run(scenario())


def test_manager_echec_de_demarrage_ne_laisse_aucune_session() -> None:
    async def scenario() -> None:
        factory = FakeEngineFactory()
        manager = BotManager(engine_factory=factory)
        factory.start_errors[4] = DerivError("InvalidToken", "Token refusé")

        with pytest.raises(DerivError):
            await manager.start(4, **START_PARAMS)
        assert manager.get(4) is None and not manager.is_active(4)
        assert factory.for_user(4)[0].stop_calls == 1

        with pytest.raises(ValueError):
            await manager.start(4, **{**START_PARAMS, "account_type": "reel"})

        await manager.start(4, **START_PARAMS)
        assert manager.is_active(4)
        await manager.shutdown()

    run(scenario())


def test_manager_statut_stopped_par_defaut() -> None:
    manager = BotManager(engine_factory=FakeEngineFactory())
    status = manager.status(424242)
    assert status["state"] == "STOPPED"
    assert status["account_type"] == "demo"
    assert set(BotEngine(app_id="test-app-id").get_status()) == set(status)
    assert manager.get(424242) is None
    assert not manager.is_active(424242)


# ======================================================================
# Routes /api/bot/* et /ws/bot/status
# ======================================================================
@pytest.fixture
def bot_api(make_app: Callable[..., Any]) -> Any:
    factory = FakeEngineFactory()
    manager = BotManager(engine_factory=factory)
    app = make_app(bot_router.router, bot_manager=manager)
    with TestClient(app) as client:
        yield client, manager, factory
        client.portal.call(manager.shutdown)


def test_routes_bot_exigent_un_jwt(bot_api: Any) -> None:
    client, _, factory = bot_api
    assert client.post("/api/bot/start", json=START_BODY).status_code == 401
    assert client.post("/api/bot/stop").status_code == 401
    assert client.get("/api/bot/status").status_code == 401

    bad = {"Authorization": "Bearer pas-un-jwt"}
    assert client.post("/api/bot/start", json=START_BODY, headers=bad).status_code == 401
    assert client.post("/api/bot/stop", headers=bad).status_code == 401
    assert client.get("/api/bot/status", headers=bad).status_code == 401
    assert factory.engines == []


def test_routes_bot_isolent_deux_utilisateurs(bot_api: Any, make_user: Any, auth_headers: Any) -> None:
    client, manager, factory = bot_api
    user_a, user_b = make_user(), make_user()
    headers_a, headers_b = auth_headers(user_a), auth_headers(user_b)

    r = client.post("/api/bot/start", json={**START_BODY, "api_token": "token-a"}, headers=headers_a)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "state": "RUNNING", "detail": "Bot démarré"}
    r = client.post(
        "/api/bot/start",
        json={**START_BODY, "api_token": "token-b", "symbol": "BOOM1000"},
        headers=headers_b,
    )
    assert r.status_code == 200, r.text

    engine_a = factory.for_user(user_a.id)[0]
    engine_b = factory.for_user(user_b.id)[0]
    assert engine_a.account_type == "demo"
    assert engine_a.start_kwargs["api_token"] == "token-a"
    assert engine_a.start_kwargs["strategy_type"] == StrategyType.RISE_FALL
    assert engine_b.start_kwargs["api_token"] == "token-b"

    r = client.post("/api/bot/stop", headers=headers_a)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "state": "STOPPED", "detail": "Bot arrêté"}

    status_a = client.get("/api/bot/status", headers=headers_a).json()
    status_b = client.get("/api/bot/status", headers=headers_b).json()
    assert status_a["state"] == "STOPPED"
    assert status_b["state"] == "RUNNING"
    assert status_b["symbol"] == "BOOM1000"
    assert status_b["account_type"] == "demo"
    assert engine_b.stop_calls == 0
    assert manager.is_active(user_b.id)


def test_status_sans_session_renvoie_stopped(bot_api: Any, make_user: Any, auth_headers: Any) -> None:
    client, _, _ = bot_api
    r = client.get("/api/bot/status", headers=auth_headers(make_user()))
    assert r.status_code == 200
    assert r.json()["state"] == "STOPPED"
    assert r.json()["account_type"] == "demo"


def test_double_start_renvoie_409(bot_api: Any, make_user: Any, auth_headers: Any) -> None:
    client, _, factory = bot_api
    user = make_user()
    headers = auth_headers(user)
    assert client.post("/api/bot/start", json=START_BODY, headers=headers).status_code == 200
    r = client.post("/api/bot/start", json=START_BODY, headers=headers)
    assert r.status_code == 409
    assert len(factory.for_user(user.id)) == 1


def test_compte_reel_exige_essai_ou_premium(bot_api: Any, make_user: Any, auth_headers: Any) -> None:
    client, _, factory = bot_api
    now = datetime.now(timezone.utc)
    real_body = {**START_BODY, "account_type": "real"}

    # Ni essai ni premium : 402 sur réel, sans créer de moteur ; démo libre.
    free = make_user()
    r = client.post("/api/bot/start", json=real_body, headers=auth_headers(free))
    assert r.status_code == 402
    assert factory.for_user(free.id) == []
    r = client.post("/api/bot/start", json=START_BODY, headers=auth_headers(free))
    assert r.status_code == 200

    # Premium expiré et essai expiré : 402.
    expired = make_user(subscription_tier="premium", subscription_expires_at=now - timedelta(days=1))
    assert client.post("/api/bot/start", json=real_body, headers=auth_headers(expired)).status_code == 402

    # Essai actif, premium actif, admin : autorisés sur réel.
    in_trial = make_user(trial_started_at=now)
    premium = make_user(subscription_tier="premium", subscription_expires_at=now + timedelta(days=30))
    admin = make_user(role="admin")
    for user in (in_trial, premium, admin):
        r = client.post("/api/bot/start", json=real_body, headers=auth_headers(user))
        assert r.status_code == 200, r.text
        assert factory.for_user(user.id)[0].account_type == "real"


def test_erreurs_de_demarrage_jamais_en_5xx(bot_api: Any, make_user: Any, auth_headers: Any) -> None:
    client, _, factory = bot_api
    user = make_user()
    headers = auth_headers(user)

    factory.start_errors[user.id] = DerivError("NoAccount", "Aucun compte demo disponible")
    r = client.post("/api/bot/start", json=START_BODY, headers=headers)
    assert r.status_code == 400
    assert "Deriv" in r.json()["detail"]

    factory.start_errors[user.id] = ValueError("valeur inattendue")
    r = client.post("/api/bot/start", json=START_BODY, headers=headers)
    assert r.status_code == 400

    r = client.post("/api/bot/start", json={**START_BODY, "strategy_type": "YOLO"}, headers=headers)
    assert r.status_code == 422
    r = client.post("/api/bot/start", json={**START_BODY, "account_type": "reel"}, headers=headers)
    assert r.status_code == 422
    assert client.get("/api/bot/status", headers=headers).json()["state"] == "STOPPED"


def test_ws_status_ferme_en_4401_sans_auth(bot_api: Any) -> None:
    client, _, _ = bot_api
    for first_message in ({"type": "hello"}, {"type": "auth", "token": "invalide"}):
        with client.websocket_connect("/ws/bot/status") as ws:
            ws.send_json(first_message)
            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws.receive_json()
        assert excinfo.value.code == 4401


def test_ws_status_envoie_le_snapshot_de_l_utilisateur(
    bot_api: Any, make_user: Any, auth_headers: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = bot_api
    monkeypatch.setattr(bot_router, "WS_PUSH_INTERVAL", 0.05)
    user_a, user_b = make_user(), make_user()
    r = client.post("/api/bot/start", json=START_BODY, headers=auth_headers(user_a))
    assert r.status_code == 200

    with client.websocket_connect("/ws/bot/status") as ws:
        ws.send_json({"type": "auth", "token": auth.create_access_token(user_a)})
        assert ws.receive_json() == {"type": "auth_ok", "user_id": user_a.id}
        first, second = ws.receive_json(), ws.receive_json()
    assert first["state"] == "RUNNING"
    assert first["symbol"] == "R_75"
    assert first["account_type"] == "demo"
    assert second["state"] == "RUNNING"

    # B ne voit que SA session (aucune).
    with client.websocket_connect("/ws/bot/status") as ws:
        ws.send_json({"type": "auth", "token": auth.create_access_token(user_b)})
        assert ws.receive_json() == {"type": "auth_ok", "user_id": user_b.id}
        assert ws.receive_json()["state"] == "STOPPED"


# ======================================================================
# BotEngine avec un faux DerivClient
# ======================================================================
class FakeDerivBackend:
    """Configuration et traces partagées par les faux clients d'un test."""

    def __init__(self) -> None:
        # Cotations croissantes : la stratégie Rise/Fall décide CALLE.
        self.quotes: list[float] = [round(100.0 + 0.1 * i, 2) for i in range(12)]
        self.profit: float = 0.95
        # Profits successifs des trades (consommés dans l'ordre), puis `profit`.
        self.profits: list[float] = []
        self.account_type: str | None = None  # None : le type demandé
        self.subscribe_error: BaseException | None = None
        self.clients: list[FakeDerivClient] = []


class FakeDerivClient:
    """Faux DerivClient : aucun réseau, achats et règlements simulés localement."""

    def __init__(
        self,
        backend: FakeDerivBackend,
        *,
        app_id: str,
        rest_base_url: str = "",
        preferred_account_type: str = "demo",
    ) -> None:
        self.backend = backend
        self.init_kwargs = {
            "app_id": app_id,
            "rest_base_url": rest_base_url,
            "preferred_account_type": preferred_account_type,
        }
        self.callbacks: dict[str, Any] = {}
        self.orders: list[dict[str, Any]] = []
        self.closed = False
        self._account_info: dict[str, Any] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        backend.clients.append(self)

    async def connect(self, pat_token: str) -> None:
        account_type = self.backend.account_type or self.init_kwargs["preferred_account_type"]
        self._account_info = {
            "loginid": "VRTC000001",
            "account_id": "VRTC000001",
            "balance": 1000.0,
            "currency": "USD",
            "account_type": account_type,
            "status": "active",
        }

    @property
    def account_info(self) -> dict[str, Any]:
        if self._account_info is None:
            raise RuntimeError("Non connecté")
        return self._account_info

    def on_subscription(self, msg_type: str, callback: Any) -> None:
        self.callbacks[msg_type] = callback

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "ticks" in payload:
            if self.backend.subscribe_error is not None:
                raise self.backend.subscribe_error
            for quote in self.backend.quotes:
                await self.callbacks["tick"](
                    {"msg_type": "tick", "tick": {"symbol": payload["ticks"], "quote": quote, "pip_size": 2}}
                )
        return {}

    async def buy_proposal(self, **kwargs: Any) -> dict[str, Any]:
        self.orders.append(kwargs)
        return {"contract_id": 1000 + len(self.orders) - 1, "buy_price": kwargs["amount"]}

    async def proposal_open_contract(self, contract_id: int, subscribe: bool = True) -> dict[str, Any]:
        order = self.orders[-1]
        profit = self.backend.profits.pop(0) if self.backend.profits else self.backend.profit
        poc = {
            "contract_id": contract_id,
            "contract_type": order["contract_type"],
            "is_sold": 1,
            "profit": profit,
            "payout": round(order["amount"] + profit, 2),
        }
        task = asyncio.create_task(self.callbacks["proposal_open_contract"]({"proposal_open_contract": poc}))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return poc

    async def forget_all(self, *types: str) -> dict[str, Any]:
        return {}

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_deriv(monkeypatch: pytest.MonkeyPatch) -> FakeDerivBackend:
    backend = FakeDerivBackend()
    monkeypatch.setattr(
        bot_engine, "DerivClient", lambda **kwargs: FakeDerivClient(backend, **kwargs)
    )
    return backend


def _make_engine(events: list[Any], *, stopped: asyncio.Event | None = None, **kwargs: Any) -> BotEngine:
    async def on_session(event: SessionEvent) -> None:
        events.append(event)
        if stopped is not None and event.kind == "stopped":
            stopped.set()

    async def on_event(event: Any) -> None:
        events.append(event)

    params: dict[str, Any] = {
        "app_id": "test-app-id",
        "min_ticks": 3,
        "trade_cooldown": 0.0,
        "trade_timeout": 2.0,
        "on_trade_opened": on_event,
        "on_trade_settled": on_event,
        "on_session": on_session,
    }
    params.update(kwargs)
    return BotEngine(**params)


def test_engine_emet_les_evenements_dans_l_ordre(fake_deriv: FakeDerivBackend) -> None:
    async def scenario() -> tuple[BotEngine, list[Any]]:
        events: list[Any] = []
        stopped = asyncio.Event()
        engine = _make_engine(events, stopped=stopped, user_id=7, account_type="real")
        # 2 trades gagnants de 0.95 atteignent le TP de 1.5.
        await engine.start(
            api_token="faux-token", symbol="R_75", stake=1.0,
            stop_loss=5.0, take_profit=1.5, strategy_type="RISE_FALL",
        )
        await asyncio.wait_for(stopped.wait(), timeout=5.0)
        # Arrêts manuels après le TP : aucun second "stopped".
        await engine.stop()
        await engine.stop()
        await engine.flush_events()
        return engine, events

    engine, events = run(scenario())

    client = fake_deriv.clients[0]
    assert client.init_kwargs["preferred_account_type"] == "real"
    assert client.closed
    assert [type(event).__name__ for event in events] == [
        "SessionEvent", "TradeOpenedEvent", "TradeSettledEvent",
        "TradeOpenedEvent", "TradeSettledEvent", "SessionEvent",
    ]
    assert events[0] == SessionEvent(user_id=7, kind="started", account_type="real")
    assert events[-1] == SessionEvent(user_id=7, kind="stopped", account_type="real")
    assert events[1] == TradeOpenedEvent(
        user_id=7, contract_id=1000, contract_type="CALLE", symbol="R_75",
        stake=1.0, duration=5, duration_unit="t", barrier=None, currency="USD",
        account_type="real", account_balance=1000.0,
    )
    assert events[2] == TradeSettledEvent(user_id=7, contract_id=1000, profit=0.95, payout=1.95)
    # Solde estimé = solde de départ + PnL de la session au moment de l'achat.
    assert events[3].contract_id == 1001
    assert events[3].account_balance == pytest.approx(1000.95)

    status = engine.get_status()
    assert status["state"] == "TAKE_PROFIT_REACHED"
    assert status["account_type"] == "real"
    assert status["trades_total"] == 2


@pytest.mark.parametrize("pause_before_stop", [0.0, 0.1])
def test_engine_arret_manuel_emet_stopped_une_seule_fois(
    fake_deriv: FakeDerivBackend, pause_before_stop: float
) -> None:
    fake_deriv.quotes = [100.0]  # moins que min_ticks : aucun trade

    async def scenario() -> tuple[BotEngine, list[Any]]:
        events: list[Any] = []
        engine = _make_engine(events, user_id=8, account_type="demo")
        await engine.start(
            api_token="faux-token", symbol="R_75", stake=1.0,
            stop_loss=5.0, take_profit=5.0,
        )
        await asyncio.sleep(pause_before_stop)
        await engine.stop()
        await engine.stop()
        await engine.flush_events()
        return engine, events

    engine, events = run(scenario())
    assert [event.kind for event in events] == ["started", "stopped"]
    assert engine.get_status()["state"] == "STOPPED"
    client = fake_deriv.clients[0]
    assert client.init_kwargs["preferred_account_type"] == "demo"
    assert client.orders == []
    assert client.closed


def _run_until_stop_loss(
    fake_deriv: FakeDerivBackend, *, stake: float, stop_loss: float, strategy_type: str
) -> tuple[BotEngine, list[Any]]:
    """Session jusqu'à l'arrêt automatique, puis deux arrêts manuels sans effet."""

    async def scenario() -> tuple[BotEngine, list[Any]]:
        events: list[Any] = []
        stopped = asyncio.Event()
        engine = _make_engine(events, stopped=stopped, user_id=9, account_type="demo")
        await engine.start(
            api_token="faux-token", symbol="R_75", stake=stake,
            stop_loss=stop_loss, take_profit=1000.0, strategy_type=strategy_type,
        )
        await asyncio.wait_for(stopped.wait(), timeout=5.0)
        await engine.stop()
        await engine.stop()
        await engine.flush_events()
        return engine, events

    return run(scenario())


def _assert_stop_loss_jamais_depasse(events: list[Any], stop_loss: float) -> None:
    """Chaque achat tenait dans le budget restant ; "stopped" émis une seule fois."""
    pnl = 0.0
    for event in events:
        if isinstance(event, TradeOpenedEvent):
            assert event.stake <= stop_loss + pnl + 1e-9, (event.stake, pnl)
        elif isinstance(event, TradeSettledEvent):
            pnl += event.profit
            assert pnl >= -stop_loss - 1e-9
    kinds = [event.kind for event in events if isinstance(event, SessionEvent)]
    assert kinds == ["started", "stopped"]
    assert isinstance(events[-1], SessionEvent)


def test_engine_stop_loss_mise_fixe_arret_avant_depassement(
    fake_deriv: FakeDerivBackend, caplog: pytest.LogCaptureFixture
) -> None:
    # Cas de production : PnL -9.72 puis une mise de 1.00 aurait mené à -10.72.
    fake_deriv.profits = [0.28] + [-1.0] * 15
    caplog.set_level("WARNING", logger="bot_engine")

    engine, events = _run_until_stop_loss(
        fake_deriv, stake=1.0, stop_loss=10.0, strategy_type="RISE_FALL"
    )

    [client] = fake_deriv.clients
    assert len(client.orders) == 11  # 1 gain + 10 pertes, aucun 12e achat
    assert all(order["amount"] == 1.0 for order in client.orders)
    status = engine.get_status()
    assert status["state"] == "STOP_LOSS_REACHED"
    assert status["pnl"] == pytest.approx(-9.72)
    assert status["trades_total"] == 11
    assert client.closed
    _assert_stop_loss_jamais_depasse(events, 10.0)
    assert sum(isinstance(e, TradeOpenedEvent) for e in events) == 11
    assert "prochaine mise 1.00 > budget restant 0.28" in caplog.text
    assert "faux-token" not in caplog.text


@pytest.mark.parametrize(
    ("stop_loss", "expected_orders", "expected_pnl"),
    [
        (10.0, 10, -10.0),  # 10e mise = budget restant (1.00) : autorisée
        (9.5, 9, -9.0),  # budget restant 0.50 < mise 1.00 : arrêt avant achat
    ],
)
def test_engine_stop_loss_mise_egale_au_budget_autorisee(
    fake_deriv: FakeDerivBackend, stop_loss: float, expected_orders: int, expected_pnl: float
) -> None:
    fake_deriv.profits = [-1.0] * 15

    engine, events = _run_until_stop_loss(
        fake_deriv, stake=1.0, stop_loss=stop_loss, strategy_type="RISE_FALL"
    )

    [client] = fake_deriv.clients
    assert len(client.orders) == expected_orders
    status = engine.get_status()
    assert status["state"] == "STOP_LOSS_REACHED"
    assert status["pnl"] == pytest.approx(expected_pnl)
    assert status["pnl"] >= -stop_loss
    _assert_stop_loss_jamais_depasse(events, stop_loss)


@pytest.mark.parametrize(
    ("stop_loss", "expected_stakes", "expected_pnl"),
    [
        (10.0, [1.0, 2.0, 4.0], -7.0),  # mise 8 > budget 3 : arrêt avant achat
        (15.0, [1.0, 2.0, 4.0, 8.0], -15.0),  # mise 8 = budget 8 : autorisée
    ],
)
def test_engine_stop_loss_martingale_arret_avant_depassement(
    fake_deriv: FakeDerivBackend,
    stop_loss: float,
    expected_stakes: list[float],
    expected_pnl: float,
) -> None:
    # Pertes égales à la mise : 1, 2, 4, 8, 16, 32...
    fake_deriv.profits = [-float(2**i) for i in range(8)]

    engine, events = _run_until_stop_loss(
        fake_deriv, stake=1.0, stop_loss=stop_loss, strategy_type="MARTINGALE"
    )

    [client] = fake_deriv.clients
    assert [order["amount"] for order in client.orders] == expected_stakes
    status = engine.get_status()
    assert status["state"] == "STOP_LOSS_REACHED"
    assert status["pnl"] == pytest.approx(expected_pnl)
    assert status["pnl"] >= -stop_loss
    _assert_stop_loss_jamais_depasse(events, stop_loss)


def test_engine_mise_initiale_superieure_au_stop_loss_aucun_achat(
    fake_deriv: FakeDerivBackend,
) -> None:
    engine, events = _run_until_stop_loss(
        fake_deriv, stake=5.0, stop_loss=2.0, strategy_type="RISE_FALL"
    )

    [client] = fake_deriv.clients
    assert client.orders == []
    assert client.closed
    assert engine.get_status()["state"] == "STOP_LOSS_REACHED"
    assert [event.kind for event in events] == ["started", "stopped"]


def test_engine_erreur_deriv_termine_la_session(fake_deriv: FakeDerivBackend) -> None:
    fake_deriv.subscribe_error = DerivError("MarketIsClosed", "Marché fermé")

    async def scenario() -> tuple[BotEngine, list[Any]]:
        events: list[Any] = []
        stopped = asyncio.Event()
        engine = _make_engine(events, stopped=stopped, user_id=9)
        await engine.start(
            api_token="faux-token", symbol="R_75", stake=1.0,
            stop_loss=5.0, take_profit=5.0,
        )
        await asyncio.wait_for(stopped.wait(), timeout=5.0)
        await engine.stop()
        await engine.flush_events()
        return engine, events

    engine, events = run(scenario())
    assert [event.kind for event in events] == ["started", "stopped"]
    status = engine.get_status()
    assert status["state"] == "ERROR"
    assert "MarketIsClosed" in status["error"]
    assert fake_deriv.clients[0].closed


def test_engine_refuse_un_compte_d_un_autre_type(fake_deriv: FakeDerivBackend) -> None:
    fake_deriv.account_type = "real"  # le token ne rend qu'un compte réel

    async def scenario() -> tuple[BotEngine, list[Any], DerivError]:
        events: list[Any] = []
        engine = _make_engine(events, account_type="demo")
        with pytest.raises(DerivError) as excinfo:
            await engine.start(
                api_token="faux-token", symbol="R_75", stake=1.0,
                stop_loss=5.0, take_profit=5.0,
            )
        await engine.flush_events()
        return engine, events, excinfo.value

    engine, events, error = run(scenario())
    assert error.code == "AccountTypeMismatch"
    assert events == []
    assert engine.get_status()["state"] == "STOPPED"
    client = fake_deriv.clients[0]
    assert client.closed
    assert client.orders == []


def test_engine_abonnes_en_erreur_sans_effet(fake_deriv: FakeDerivBackend) -> None:
    calls: list[str] = []

    async def cassé(event: Any) -> None:
        calls.append(type(event).__name__)
        raise RuntimeError("abonné en panne")

    async def scenario() -> BotEngine:
        engine = BotEngine(
            app_id="test-app-id", min_ticks=3, trade_cooldown=0.0, trade_timeout=2.0,
            user_id=10, on_trade_opened=cassé, on_trade_settled=cassé, on_session=cassé,
        )
        await engine.start(
            api_token="faux-token", symbol="R_75", stake=1.0,
            stop_loss=5.0, take_profit=0.5,
        )
        await wait_until(lambda: engine.get_status()["state"] != "RUNNING")
        await engine.flush_events()
        await engine.stop()
        return engine

    engine = run(scenario())
    assert engine.get_status()["state"] == "TAKE_PROFIT_REACHED"
    assert engine.get_status()["trades_total"] == 1
    assert calls == ["SessionEvent", "TradeOpenedEvent", "TradeSettledEvent", "SessionEvent"]


def test_engine_type_de_compte_invalide() -> None:
    with pytest.raises(ValueError):
        BotEngine(app_id="test-app-id", account_type="reel")
    assert BotEngine(app_id="test-app-id", account_type=" REAL ").account_type == "real"


def test_manager_avec_vrai_moteur_libere_la_session_apres_tp(
    fake_deriv: FakeDerivBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DERIV_APP_ID", "test-app-id")

    async def scenario() -> tuple[list[str], list[TradeOpenedEvent], dict[str, Any]]:
        manager = BotManager()  # vrais BotEngine, faux DerivClient
        kinds: list[str] = []
        opened: list[TradeOpenedEvent] = []

        async def on_session(event: SessionEvent) -> None:
            kinds.append(event.kind)

        async def on_opened(event: TradeOpenedEvent) -> None:
            opened.append(event)

        manager.add_session_listener(on_session)
        manager.add_trade_opened_listener(on_opened)
        snapshot = await manager.start(
            11, api_token="faux-token", symbol="R_75", stake=1.0,
            stop_loss=5.0, take_profit=0.5, strategy_type="RISE_FALL",
            account_type="demo",
        )
        assert snapshot["state"] == "RUNNING"
        assert snapshot["account_type"] == "demo"
        await wait_until(lambda: manager.get(11) is None)
        final = manager.status(11)
        await manager.shutdown()
        return kinds, opened, final

    kinds, opened, final = run(scenario())
    assert final["state"] == "TAKE_PROFIT_REACHED"
    assert final["account_type"] == "demo"
    assert kinds == ["started", "stopped"]
    assert [(event.user_id, event.account_type) for event in opened] == [(11, "demo")]
    assert fake_deriv.clients[0].init_kwargs["preferred_account_type"] == "demo"


# ======================================================================
# DerivClient._discover_account : correspondance stricte du type
# ======================================================================
def _accounts(*types: str) -> dict[str, Any]:
    ids = {"real": "CR900001", "demo": "VRTC900002"}
    balances = {"real": 25.5, "demo": 10000}
    return {
        "data": [
            {
                "account_id": ids[account_type],
                "account_type": account_type,
                "balance": balances[account_type],
                "currency": "USD",
                "status": "active",
            }
            for account_type in types
        ]
    }


@pytest.fixture
def accounts_api(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Remplace le transport HTTP de deriv_client par un faux (aucun réseau)."""
    state: dict[str, Any] = {"status": 200, "payload": _accounts("real", "demo"), "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return httpx.Response(state["status"], json=state["payload"])

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(deriv_client.httpx, "AsyncClient", fake_async_client)
    return state


def _discover(preferred_account_type: str) -> dict[str, Any]:
    client = DerivClient(
        app_id="test-app-id",
        rest_base_url="https://deriv.test",
        preferred_account_type=preferred_account_type,
    )
    client._pat_token = "faux-token"
    return asyncio.run(client._discover_account())


def test_discover_account_choisit_le_type_demande(accounts_api: dict[str, Any]) -> None:
    # Le compte réel est listé en premier : il ne doit pas être pris pour la démo.
    demo = _discover("demo")
    assert demo["account_id"] == "VRTC900002"
    assert demo["account_type"] == "demo"
    assert demo["balance"] == 10000.0

    real = _discover("real")
    assert real["account_id"] == "CR900001"
    assert real["account_type"] == "real"

    request = accounts_api["requests"][0]
    assert request.url.path == "/trading/v1/options/accounts"
    assert request.headers["Deriv-App-ID"] == "test-app-id"


@pytest.mark.parametrize(
    ("available", "requested"),
    [(("real",), "demo"), (("demo",), "real"), ((), "demo")],
)
def test_discover_account_ne_retombe_jamais_sur_un_autre_type(
    accounts_api: dict[str, Any], available: tuple[str, ...], requested: str
) -> None:
    accounts_api["payload"] = _accounts(*available)
    with pytest.raises(DerivError) as excinfo:
        _discover(requested)
    assert excinfo.value.code == "NoAccount"


def test_discover_account_token_refuse(accounts_api: dict[str, Any]) -> None:
    accounts_api["status"] = 401
    accounts_api["payload"] = {"error": "invalid token"}
    with pytest.raises(DerivError) as excinfo:
        _discover("demo")
    assert excinfo.value.code == "Unauthorized"
