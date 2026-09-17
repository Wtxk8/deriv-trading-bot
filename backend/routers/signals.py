"""Signaux de trading : historique REST, statistiques réelles et flux WebSocket.

Accès live (signaux actifs en temps réel) : premium actif, essai actif ou
admin. Les autres ne voient que les signaux clos (TP, SL ou expirés) : on
publie le résultat réel de chaque signal, jamais une promesse de gain.

Le moteur est lu dans `app.state.signal_engine` ; absent (fonction
désactivée), le REST renvoie des listes vides et le WebSocket se contente du
message "hello".

Préférences (/signals/preferences) : chaque utilisateur choisit les symboles
et stratégies reçus en direct sur le WebSocket. L'historique et les
statistiques REST restent globaux (taux de réussite publié honnête).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import auth
import database
import models
import payments as pay
from models_signals import SignalPreference
from signal_engine import DEFAULT_SYMBOL_NAMES, DEFAULT_SYMBOLS, STRATEGIES, empty_stats
from ws_auth import authenticate_websocket

logger = logging.getLogger("routers.signals")

router = APIRouter(tags=["signals"])

WS_CLOSE_TOO_SLOW = 1013  # "Try Again Later" : le client se reconnecte

STRATEGY_LABELS: dict[str, str] = {"MA_CROSS": "Croisement MM", "RSI": "RSI", "SPIKE": "Spike"}


def live_access_window(
    role: str,
    tier: str,
    expires_at: Optional[datetime],
    trial_started_at: Optional[datetime],
) -> tuple[bool, Optional[datetime]]:
    """(accès live ?, fin de cet accès — None si illimité ou sans accès)."""
    if role == "admin":
        return True, None
    ends: list[datetime] = []
    if pay.is_premium_active(tier, expires_at):
        if expires_at is None:
            return True, None  # premium à vie
        ends.append(expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc))
    if pay.is_trial_active(trial_started_at):
        ends.append(pay.trial_expires_at(trial_started_at))
    if not ends:
        return False, None
    return True, max(ends)


def has_live_access(user: models.User) -> bool:
    return live_access_window(
        user.role, user.subscription_tier, user.subscription_expires_at, user.trial_started_at
    )[0]


def _engine(app: Any):
    return getattr(app.state, "signal_engine", None)


@router.get("/signals")
def list_signals(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    user: models.User = Depends(auth.get_current_user),
) -> dict[str, Any]:
    live = has_live_access(user)
    engine = _engine(request.app)
    if engine is None:
        return {"live_access": live, "signals": []}
    return {"live_access": live, "signals": engine.recent(limit, closed_only=not live)}


@router.get("/signals/stats")
def signal_stats(
    request: Request,
    days: int = Query(7, ge=1, le=365),
    user: models.User = Depends(auth.get_current_user),
) -> dict[str, Any]:
    engine = _engine(request.app)
    if engine is None:
        return empty_stats(days)
    return engine.stats(days)


# ----- préférences -----

class PreferencesIn(BaseModel):
    symbols: list[Annotated[str, Field(max_length=40)]] = Field(max_length=100)
    strategies: list[Annotated[str, Field(max_length=40)]] = Field(max_length=20)
    notify: bool


def available_symbols(engine: Any) -> list[dict[str, str]]:
    """Symboles suivis par le moteur ; liste par défaut si le moteur est absent."""
    if engine is None:
        return [{"symbol": symbol, "name": DEFAULT_SYMBOL_NAMES.get(symbol, symbol)}
                for symbol in DEFAULT_SYMBOLS]
    return engine.symbol_catalog()


def _decode_list(raw: Optional[str]) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _dedupe(values: list[str]) -> list[str]:
    """Majuscules, sans doublon, ordre conservé (une chaîne vide reste invalide)."""
    out: list[str] = []
    for raw in values:
        value = raw.strip().upper()
        if value not in out:
            out.append(value)
    return out


def load_preferences(db: Session, user_id: int, available: list[str]) -> dict[str, Any]:
    """Préférences effectives (défaut : tout), limitées aux symboles disponibles."""
    row = db.get(SignalPreference, user_id)
    if row is None:
        return {"symbols": list(available), "strategies": list(STRATEGIES), "notify": True}
    symbols = [symbol for symbol in _decode_list(row.symbols) if symbol in available]
    strategies = [key for key in _decode_list(row.strategies) if key in STRATEGIES]
    return {"symbols": symbols, "strategies": strategies, "notify": bool(row.notify)}


def _read_preferences(user_id: int, available: list[str]) -> dict[str, Any]:
    with database.SessionLocal() as db:
        return load_preferences(db, user_id, available)


def _preferences_out(preferences: dict[str, Any], catalog: list[dict[str, str]]) -> dict[str, Any]:
    return {
        **preferences,
        "available_symbols": catalog,
        "available_strategies": [
            {"key": key, "label": STRATEGY_LABELS.get(key, key)} for key in STRATEGIES
        ],
    }


@router.get("/signals/preferences")
def get_preferences(
    request: Request,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(database.get_db),
) -> dict[str, Any]:
    catalog = available_symbols(_engine(request.app))
    preferences = load_preferences(db, user.id, [item["symbol"] for item in catalog])
    return _preferences_out(preferences, catalog)


@router.put("/signals/preferences")
def put_preferences(
    body: PreferencesIn,
    request: Request,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(database.get_db),
) -> dict[str, Any]:
    catalog = available_symbols(_engine(request.app))
    available = [item["symbol"] for item in catalog]
    symbols = _dedupe(body.symbols)
    strategies = _dedupe(body.strategies)
    unknown_symbols = [symbol for symbol in symbols if symbol not in available]
    if unknown_symbols:
        raise HTTPException(422, f"Symbole indisponible : {', '.join(unknown_symbols)}")
    unknown_strategies = [key for key in strategies if key not in STRATEGIES]
    if unknown_strategies:
        raise HTTPException(422, f"Stratégie inconnue : {', '.join(unknown_strategies)}")

    row = db.get(SignalPreference, user.id)
    if row is None:
        row = SignalPreference(user_id=user.id)
        db.add(row)
    row.symbols = json.dumps(symbols)
    row.strategies = json.dumps(strategies)
    row.notify = body.notify
    db.commit()
    preferences = {"symbols": symbols, "strategies": strategies, "notify": body.notify}
    return _preferences_out(preferences, catalog)


# ----- WebSocket -----

def _allowed(message: dict[str, Any], live: bool) -> bool:
    kind = message.get("type")
    if kind == "signal":
        return live
    if kind == "update":
        signal = message.get("signal") or {}
        return live or signal.get("status") != "active"
    return False


def _matches(message: dict[str, Any], preferences: dict[str, Any]) -> bool:
    signal = message.get("signal") or {}
    return (signal.get("symbol") in preferences["symbols"]
            and signal.get("strategy") in preferences["strategies"])


async def _wait_disconnect(websocket: WebSocket) -> None:
    """Consomme (et ignore) les messages du client jusqu'à sa déconnexion."""
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return


async def _pump(websocket: WebSocket, queue: asyncio.Queue, live: bool,
                live_until: Optional[datetime], user_id: int, available: list[str]) -> None:
    while True:
        message = await queue.get()
        if message is None:  # sentinelle du hub : client trop lent, déjà retiré
            try:
                await websocket.close(code=WS_CLOSE_TOO_SLOW)
            except Exception:  # noqa: BLE001
                pass
            return
        # Un essai ou un abonnement peut expirer pendant la connexion.
        still_live = live and (live_until is None or datetime.now(timezone.utc) < live_until)
        if not _allowed(message, still_live):
            continue
        # Relues à chaque message candidat : un PUT s'applique sans reconnexion.
        preferences = await asyncio.to_thread(_read_preferences, user_id, available)
        if _matches(message, preferences):
            await websocket.send_json(message)


@router.websocket("/ws/signals")
async def signals_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    identity = await authenticate_websocket(websocket)
    if identity is None:
        return
    live, live_until = live_access_window(
        identity.role,
        identity.subscription_tier,
        identity.subscription_expires_at,
        identity.trial_started_at,
    )
    engine = _engine(websocket.app)
    available = [item["symbol"] for item in available_symbols(engine)]
    queue = engine.register() if engine is not None else None
    try:
        preferences = await asyncio.to_thread(_read_preferences, identity.user_id, available)
        await websocket.send_json({"type": "hello", "live_access": live, "preferences": preferences})
        if queue is None:
            await _wait_disconnect(websocket)
            return
        pump = asyncio.create_task(_pump(websocket, queue, live, live_until, identity.user_id, available))
        watcher = asyncio.create_task(_wait_disconnect(websocket))
        try:
            await asyncio.wait({pump, watcher}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (pump, watcher):
                task.cancel()
            await asyncio.gather(pump, watcher, return_exceptions=True)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if queue is not None:
            engine.unregister(queue)
