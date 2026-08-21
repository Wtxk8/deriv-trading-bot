"""API FastAPI : télécommande légère du BotEngine pour l'app Flutter.

Endpoints:
- POST /api/bot/start   : démarre une session de trading.
- POST /api/bot/stop    : arrête immédiatement le bot.
- GET  /api/bot/status  : snapshot d'état + PnL.
- WS   /ws/bot/status   : flux temps réel du snapshot (push ~1s).

Une seule instance globale `BotEngine` est partagée (bot 24/7 côté serveur).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import auth
import models
import schemas
from bot_engine import BotEngine, StrategyType
from deriv_client import DerivError
from database import SessionLocal, get_db
from database import engine as db_engine

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("main")

# Intervalle de push du flux WebSocket (secondes).
WS_PUSH_INTERVAL: float = 1.0

# Instance unique du moteur (partagée entre requêtes).
engine: BotEngine = BotEngine()


# ----------------------------------------------------------------------
# Modèles Pydantic
# ----------------------------------------------------------------------
class StartBotRequest(BaseModel):
    api_token: str = Field(..., min_length=1, description="Token API Deriv")
    symbol: str = Field("R_100", min_length=1, description="Indice synthétique")
    stake: float = Field(..., gt=0, description="Mise par trade")
    stop_loss: float = Field(..., gt=0, description="Perte journalière max (abs)")
    take_profit: float = Field(..., gt=0, description="Gain journalier cible (abs)")
    strategy_type: str = Field("RISE_FALL", description="RISE_FALL | OVER_UNDER")


class ActionResponse(BaseModel):
    ok: bool
    state: str
    detail: str | None = None


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Démarrage serveur")
    models.Base.metadata.create_all(bind=db_engine)
    with SessionLocal() as db:
        auth.ensure_default_admin(db)
    try:
        yield
    finally:
        logger.info("Arrêt serveur : coupure du bot")
        try:
            await engine.stop()
        except Exception:  # noqa: BLE001
            logger.exception("Erreur à l'arrêt du bot")


app = FastAPI(title="Deriv Trading Bot API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------
# Endpoints REST
# ----------------------------------------------------------------------
@app.post("/api/bot/start", response_model=ActionResponse)
async def start_bot(req: StartBotRequest) -> ActionResponse:
    try:
        strategy = StrategyType(req.strategy_type)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"strategy_type invalide: {req.strategy_type}"
        ) from exc

    try:
        await engine.start(
            api_token=req.api_token,
            symbol=req.symbol,
            stake=req.stake,
            stop_loss=req.stop_loss,
            take_profit=req.take_profit,
            strategy_type=strategy,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DerivError as exc:
        # 4xx : Cloudflare remplace tout 5xx origine par sa page générique,
        # masquant le détail (token invalide, etc.) — jamais utiliser 5xx ici.
        raise HTTPException(status_code=400, detail=f"Deriv a refusé la requête: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Échec démarrage bot")
        raise HTTPException(status_code=400, detail=f"Échec démarrage: {exc}") from exc

    status = engine.get_status()
    return ActionResponse(ok=True, state=status["state"], detail="Bot démarré")


@app.post("/api/bot/stop", response_model=ActionResponse)
async def stop_bot() -> ActionResponse:
    try:
        await engine.stop()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Échec arrêt bot")
        raise HTTPException(status_code=400, detail=f"Échec arrêt: {exc}") from exc
    status = engine.get_status()
    return ActionResponse(ok=True, state=status["state"], detail="Bot arrêté")


@app.get("/api/bot/status")
async def bot_status() -> dict[str, Any]:
    return engine.get_status()


# ----------------------------------------------------------------------
# Endpoint WebSocket (flux temps réel)
# ----------------------------------------------------------------------
@app.websocket("/ws/bot/status")
async def ws_bot_status(websocket: WebSocket) -> None:
    await websocket.accept()
    logger.info("Client WS connecté")
    try:
        while True:
            await websocket.send_json(engine.get_status())
            await asyncio.sleep(WS_PUSH_INTERVAL)
    except WebSocketDisconnect:
        logger.info("Client WS déconnecté")
    except Exception:  # noqa: BLE001
        logger.exception("Erreur flux WS")
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------
# Authentification
# ----------------------------------------------------------------------
@app.post("/register", response_model=schemas.UserOut, status_code=201)
def register(payload: schemas.UserCreate, db: Session = Depends(get_db)) -> models.User:
    if db.query(models.User).filter(models.User.email == payload.email).first() is not None:
        raise HTTPException(status_code=409, detail="Email déjà utilisé")
    user = models.User(
        name=payload.name or payload.email.split("@")[0],
        email=payload.email,
        hashed_password=auth.hash_password(payload.password),
        role="user",
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/login", response_model=schemas.Token)
def login(payload: schemas.UserLogin, db: Session = Depends(get_db)) -> schemas.Token:
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if user is None or not auth.verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Identifiants invalides")
    if not user.active:
        raise HTTPException(status_code=403, detail="Compte suspendu")
    token = auth.create_access_token(user)
    return schemas.Token(access_token=token, user=schemas.UserOut.model_validate(user))


@app.get("/me", response_model=schemas.UserOut)
def me(current_user: models.User = Depends(auth.get_current_user)) -> models.User:
    return current_user


# ----------------------------------------------------------------------
# Administration (réservé au rôle 'admin')
# ----------------------------------------------------------------------
@app.get("/admin/users", response_model=list[schemas.UserOut])
def list_users(
    _: models.User = Depends(auth.require_admin), db: Session = Depends(get_db)
) -> list[models.User]:
    return db.query(models.User).order_by(models.User.id).all()


@app.patch("/admin/users/{user_id}", response_model=schemas.UserOut)
def update_user(
    user_id: int,
    payload: schemas.AdminUserUpdate,
    admin_user: models.User = Depends(auth.require_admin),
    db: Session = Depends(get_db),
) -> models.User:
    user = db.get(models.User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")

    if payload.role is not None:
        if user.id == admin_user.id and payload.role != "admin":
            raise HTTPException(status_code=409, detail="Impossible de vous rétrograder vous-même")
        user.role = payload.role

    if payload.active is not None:
        if user.id == admin_user.id and not payload.active:
            raise HTTPException(status_code=409, detail="Impossible de suspendre votre propre compte")
        user.active = payload.active

    db.commit()
    db.refresh(user)
    return user


@app.delete("/admin/users/{user_id}", status_code=204, response_model=None)
def delete_user(
    user_id: int,
    admin_user: models.User = Depends(auth.require_admin),
    db: Session = Depends(get_db),
) -> None:
    if user_id == admin_user.id:
        raise HTTPException(status_code=409, detail="Impossible de supprimer votre propre compte")
    user = db.get(models.User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    db.delete(user)
    db.commit()


# ----------------------------------------------------------------------
# Console admin web (SPA statique)
# ----------------------------------------------------------------------
_ADMIN_HTML = Path(__file__).parent / "static" / "admin.html"


@app.get("/admin", include_in_schema=False)
async def admin_console_root() -> FileResponse:
    return FileResponse(_ADMIN_HTML, media_type="text/html")


@app.get("/admin/", include_in_schema=False)
async def admin_console() -> FileResponse:
    return FileResponse(_ADMIN_HTML, media_type="text/html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
