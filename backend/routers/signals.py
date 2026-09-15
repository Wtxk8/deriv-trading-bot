"""Signaux de trading : historique REST, statistiques réelles et flux WebSocket.

Accès live (signaux actifs en temps réel) : premium actif, essai actif ou
admin. Les autres ne voient que les signaux clos (TP, SL ou expirés) : on
publie le résultat réel de chaque signal, jamais une promesse de gain.

Le moteur est lu dans `app.state.signal_engine` ; absent (fonction
désactivée), le REST renvoie des listes vides et le WebSocket se contente du
message "hello".
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect

import auth
import models
import payments as pay
from signal_engine import empty_stats
from ws_auth import authenticate_websocket

logger = logging.getLogger("routers.signals")

router = APIRouter(tags=["signals"])

WS_CLOSE_TOO_SLOW = 1013  # "Try Again Later" : le client se reconnecte


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


def _allowed(message: dict[str, Any], live: bool) -> bool:
    kind = message.get("type")
    if kind == "signal":
        return live
    if kind == "update":
        signal = message.get("signal") or {}
        return live or signal.get("status") != "active"
    return False


async def _wait_disconnect(websocket: WebSocket) -> None:
    """Consomme (et ignore) les messages du client jusqu'à sa déconnexion."""
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return


async def _pump(websocket: WebSocket, queue: asyncio.Queue, live: bool,
                live_until: Optional[datetime]) -> None:
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
        if _allowed(message, still_live):
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
    queue = engine.register() if engine is not None else None
    try:
        await websocket.send_json({"type": "hello", "live_access": live})
        if queue is None:
            await _wait_disconnect(websocket)
            return
        pump = asyncio.create_task(_pump(websocket, queue, live, live_until))
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
