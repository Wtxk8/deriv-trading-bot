"""Authentification des WebSockets par premier message (et non par query string).

Un JWT passé en query string finit en clair dans les logs d'accès d'uvicorn
("WebSocket /ws/...?token=..."). Protocole imposé à tous les WebSockets
authentifiés :

  1. le client se connecte ;
  2. il envoie, dans les AUTH_TIMEOUT_SECONDS, un premier message texte
     {"type": "auth", "token": "<JWT>"} ;
  3. le serveur répond {"type": "auth_ok", "user_id": <id>} puis commence
     le flux, ou ferme la connexion avec le code 4401.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

import auth
import models
from database import SessionLocal

logger = logging.getLogger("ws_auth")

WS_CLOSE_UNAUTHORIZED = 4401
AUTH_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class WsIdentity:
    """Instantané de l'utilisateur authentifié (évite les objets ORM détachés)."""

    user_id: int
    email: str
    role: str
    subscription_tier: str
    subscription_expires_at: Optional[datetime]
    trial_started_at: Optional[datetime]


def user_from_token(token: Optional[str], db: Session) -> Optional[models.User]:
    """Résout un JWT en utilisateur actif, ou None si invalide/expiré/suspendu."""
    if not token or not isinstance(token, str):
        return None
    try:
        payload = auth.decode_access_token(token)
    except HTTPException:
        return None
    sub = payload.get("sub")
    try:
        user = db.get(models.User, int(sub)) if sub is not None else None
    except (TypeError, ValueError):
        return None
    if user is None or not user.active:
        return None
    return user


async def _reject(websocket: WebSocket) -> None:
    try:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
    except Exception:  # noqa: BLE001
        pass


async def authenticate_websocket(websocket: WebSocket) -> Optional[WsIdentity]:
    """À appeler juste après `websocket.accept()`.

    Retourne l'identité et envoie {"type": "auth_ok"}, ou ferme en 4401 et
    retourne None — l'appelant doit alors simplement `return`.
    """
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT_SECONDS)
        message = json.loads(raw)
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect, RuntimeError):
        await _reject(websocket)
        return None

    if not isinstance(message, dict) or message.get("type") != "auth":
        await _reject(websocket)
        return None

    with SessionLocal() as db:
        user = user_from_token(message.get("token"), db)
        if user is None:
            await _reject(websocket)
            return None
        identity = WsIdentity(
            user_id=user.id,
            email=user.email,
            role=user.role,
            subscription_tier=user.subscription_tier,
            subscription_expires_at=user.subscription_expires_at,
            trial_started_at=user.trial_started_at,
        )

    try:
        await websocket.send_json({"type": "auth_ok", "user_id": identity.user_id})
    except Exception:  # noqa: BLE001
        return None
    return identity
