"""Routes du bot de trading : une session par utilisateur authentifié.

- POST /api/bot/start   : démarre la session de l'utilisateur (JWT requis).
- POST /api/bot/stop    : arrête la session de l'utilisateur.
- GET  /api/bot/status  : snapshot de la session de l'utilisateur.
- WS   /ws/bot/status   : auth par premier message (voir ws_auth), puis le
                          snapshot de l'utilisateur toutes les secondes.

Les sessions sont portées par `app.state.bot_manager` (un `BotManager` créé
dans le lifespan de l'application) : chaque utilisateur ne voit et ne pilote
que SA session.

Aucune erreur ne remonte en 5xx : Cloudflare remplace tout 5xx de l'origine
par sa page générique, ce qui masquerait le détail (token invalide, etc.).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

import auth
import models
import payments as pay
from bot_engine import StrategyType, normalize_account_type
from bot_manager import BotManager
from deriv_client import DerivError
from ws_auth import authenticate_websocket

logger = logging.getLogger("routers.bot")

router = APIRouter(tags=["bot"])

# Intervalle de push du flux WebSocket (secondes).
WS_PUSH_INTERVAL: float = 1.0


# ----------------------------------------------------------------------
# Modèles Pydantic
# ----------------------------------------------------------------------
class StartBotRequest(BaseModel):
    api_token: str = Field(..., min_length=1, description="Token API Deriv")
    symbol: str = Field("R_100", min_length=1, description="Indice synthétique")
    stake: float = Field(..., gt=0, description="Mise par trade")
    stop_loss: float = Field(..., gt=0, description="Perte journalière max (abs)")
    take_profit: float = Field(..., gt=0, description="Gain journalier cible (abs)")
    strategy_type: str = Field("RISE_FALL", description="RISE_FALL | OVER_UNDER | MARTINGALE")
    account_type: str = Field("demo", description="demo | real — gate premium sur real")


class ActionResponse(BaseModel):
    ok: bool
    state: str
    detail: str | None = None


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _bot_manager(connection: HTTPConnection) -> BotManager:
    """Gestionnaire de sessions créé par le lifespan de l'application."""
    manager = getattr(connection.app.state, "bot_manager", None)
    if manager is None:
        raise RuntimeError(
            "app.state.bot_manager absent : créer un BotManager dans le lifespan"
        )
    return manager


def _require_premium_if_real(account_type: str, user: models.User) -> None:
    """Bloque le démarrage sur compte réel si l'utilisateur n'a ni essai ni premium.

    Modèle :
    - Démo : libre pour tous.
    - Réel : essai gratuit 7 jours à l'inscription, puis abonnement premium
      requis. Les administrateurs sont exemptés.
    """
    if account_type != "real":
        return
    if user.role == "admin":
        return
    if pay.can_trade_real(
        user.subscription_tier, user.subscription_expires_at, user.trial_started_at
    ):
        return
    raise HTTPException(
        status_code=402,
        detail="Votre essai gratuit de 7 jours est terminé. Passez au premium pour continuer à trader en compte réel.",
    )


# ----------------------------------------------------------------------
# Endpoints REST
# ----------------------------------------------------------------------
@router.post("/api/bot/start", response_model=ActionResponse)
async def start_bot(
    req: StartBotRequest,
    request: Request,
    current_user: models.User = Depends(auth.get_current_user),
) -> ActionResponse:
    try:
        strategy = StrategyType(req.strategy_type)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"strategy_type invalide: {req.strategy_type}"
        ) from exc
    try:
        account_type = normalize_account_type(req.account_type)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"account_type invalide: {req.account_type} (attendu : demo | real)",
        ) from exc

    # Gate abonnement : compte réel Deriv → essai ou premium requis (démo libre).
    _require_premium_if_real(account_type, current_user)

    manager = _bot_manager(request)
    try:
        snapshot = await manager.start(
            current_user.id,
            api_token=req.api_token,
            symbol=req.symbol,
            stake=req.stake,
            stop_loss=req.stop_loss,
            take_profit=req.take_profit,
            strategy_type=strategy,
            account_type=account_type,
        )
    except DerivError as exc:
        # 4xx : jamais de 5xx (masqué par Cloudflare), voir docstring du module.
        raise HTTPException(
            status_code=400, detail=f"Deriv a refusé la requête: {exc}"
        ) from exc
    except RuntimeError as exc:
        # Session déjà active pour CET utilisateur (ou serveur en cours d'arrêt).
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Échec démarrage bot (user=%s)", current_user.id)
        raise HTTPException(status_code=400, detail=f"Échec démarrage: {exc}") from exc

    return ActionResponse(ok=True, state=snapshot["state"], detail="Bot démarré")


@router.post("/api/bot/stop", response_model=ActionResponse)
async def stop_bot(
    request: Request,
    current_user: models.User = Depends(auth.get_current_user),
) -> ActionResponse:
    manager = _bot_manager(request)
    try:
        snapshot = await manager.stop(current_user.id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Échec arrêt bot (user=%s)", current_user.id)
        raise HTTPException(status_code=400, detail=f"Échec arrêt: {exc}") from exc
    return ActionResponse(ok=True, state=snapshot["state"], detail="Bot arrêté")


@router.get("/api/bot/status")
async def bot_status(
    request: Request,
    current_user: models.User = Depends(auth.get_current_user),
) -> dict[str, Any]:
    return _bot_manager(request).status(current_user.id)


# ----------------------------------------------------------------------
# Endpoint WebSocket (flux temps réel)
# ----------------------------------------------------------------------
async def _wait_disconnect(websocket: WebSocket) -> None:
    """Consomme (et ignore) les messages du client jusqu'à sa déconnexion."""
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return


@router.websocket("/ws/bot/status")
async def ws_bot_status(websocket: WebSocket) -> None:
    await websocket.accept()
    identity = await authenticate_websocket(websocket)
    if identity is None:
        return

    manager = _bot_manager(websocket)
    user_id = identity.user_id
    logger.info("Client WS bot connecté (user=%s)", user_id)
    # Lecteur dédié : détecte la déconnexion sans attendre l'échec d'un envoi,
    # et un client bavard ne peut pas accélérer le rythme des pushs.
    reader = asyncio.create_task(_wait_disconnect(websocket))
    try:
        while not reader.done():
            await websocket.send_json(manager.status(user_id))
            await asyncio.wait({reader}, timeout=WS_PUSH_INTERVAL)
        logger.info("Client WS bot déconnecté (user=%s)", user_id)
    except WebSocketDisconnect:
        logger.info("Client WS bot déconnecté (user=%s)", user_id)
    except Exception:  # noqa: BLE001
        logger.exception("Erreur flux WS bot (user=%s)", user_id)
        with contextlib.suppress(Exception):
            await websocket.close()
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader
