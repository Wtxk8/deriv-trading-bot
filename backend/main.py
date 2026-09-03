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
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

import auth
import models
import payments as pay
import rate_limit
import schemas
from bot_engine import BotEngine, StrategyType
from deriv_client import DerivError
from database import SessionLocal, get_db
from database import engine as db_engine
from fastapi import Request

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
    strategy_type: str = Field("RISE_FALL", description="RISE_FALL | OVER_UNDER | MARTINGALE")
    account_type: str = Field("demo", description="demo | real — gate premium sur real")


class ActionResponse(BaseModel):
    ok: bool
    state: str
    detail: str | None = None


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------
def _ensure_migrations() -> None:
    """Ajoute les colonnes business-model manquantes (migration SQLite in-place).

    SQLite refuse ADD COLUMN avec DEFAULT non-constant : on ajoute la colonne
    nullable puis on remplit via UPDATE si nécessaire.
    """
    with db_engine.begin() as conn:
        try:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(users)"))}
        except Exception:  # noqa: BLE001
            return

        # subscription_tier : NOT NULL avec default 'free'
        if "subscription_tier" not in cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN subscription_tier VARCHAR(20) NOT NULL DEFAULT 'free'"
            ))
            logger.info("Migration : users.subscription_tier ajoutée")

        if "subscription_expires_at" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN subscription_expires_at TIMESTAMP"))
            logger.info("Migration : users.subscription_expires_at ajoutée")

        if "referred_by_user_id" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN referred_by_user_id INTEGER"))
            logger.info("Migration : users.referred_by_user_id ajoutée")

        if "trial_started_at" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN trial_started_at TIMESTAMP"))
            conn.execute(text(
                "UPDATE users SET trial_started_at = CURRENT_TIMESTAMP WHERE trial_started_at IS NULL"
            ))
            logger.info("Migration : users.trial_started_at ajoutée + backfill")

        # Table payments : SQLAlchemy create_all l'aura créée si absente ;
        # on ne fait rien de plus ici.


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Démarrage serveur")
    models.Base.metadata.create_all(bind=db_engine)
    _ensure_migrations()
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


def _require_premium_if_real(
    req: "StartBotRequest",
    user: models.User | None,
) -> None:
    """Bloque le démarrage sur compte réel si l'utilisateur n'a ni essai ni premium.

    Modèle :
    - Démo : libre pour tous.
    - Réel : essai gratuit 7 jours à l'inscription, puis abonnement premium requis.
    """
    if not (req.account_type or "").lower().startswith("real"):
        return
    if user is None:
        raise HTTPException(
            status_code=402,
            detail="Compte réel : connexion + abonnement premium requis.",
        )
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
@app.post("/api/bot/start", response_model=ActionResponse)
async def start_bot(
    req: StartBotRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ActionResponse:
    try:
        strategy = StrategyType(req.strategy_type)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"strategy_type invalide: {req.strategy_type}"
        ) from exc

    # Gate abonnement : compte réel Deriv → premium requis (démo reste libre).
    current_user: models.User | None = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        try:
            payload = auth.decode_access_token(auth_header[7:])
            sub = payload.get("sub")
            if sub is not None:
                current_user = db.get(models.User, int(sub))
        except HTTPException:
            current_user = None
    _require_premium_if_real(req, current_user)

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
def register(
    payload: schemas.UserCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> models.User:
    rate_limit.register_limiter.check(rate_limit.client_ip(request))
    if db.query(models.User).filter(models.User.email == payload.email).first() is not None:
        raise HTTPException(status_code=409, detail="Email déjà utilisé")
    user = models.User(
        name=payload.name or payload.email.split("@")[0],
        email=payload.email,
        hashed_password=auth.hash_password(payload.password),
        role="user",
        active=True,
        trial_started_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/login", response_model=schemas.Token)
def login(
    payload: schemas.UserLogin,
    request: Request,
    db: Session = Depends(get_db),
) -> schemas.Token:
    # Double garde : par IP (bruteforce large) et par compte (bruteforce ciblé).
    ip = rate_limit.client_ip(request)
    account_key = payload.email.lower()
    rate_limit.login_limiter.check(ip)
    rate_limit.login_account_limiter.check(account_key)

    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if user is None or not auth.verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Identifiants invalides")
    if not user.active:
        raise HTTPException(status_code=403, detail="Compte suspendu")

    # Connexion réussie : on libère les compteurs pour ne pas pénaliser l'usage normal.
    rate_limit.login_limiter.reset(ip)
    rate_limit.login_account_limiter.reset(account_key)

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

    if payload.name is not None:
        user.name = payload.name.strip() or user.name

    if payload.email is not None and payload.email != user.email:
        exists = db.query(models.User).filter(models.User.email == payload.email).first()
        if exists is not None:
            raise HTTPException(status_code=409, detail="Email déjà utilisé")
        user.email = payload.email

    db.commit()
    db.refresh(user)
    return user


@app.post("/admin/users/{user_id}/grant-premium", response_model=schemas.UserOut)
def admin_grant_premium(
    user_id: int,
    payload: schemas.AdminGrantPremium,
    _: models.User = Depends(auth.require_admin),
    db: Session = Depends(get_db),
) -> models.User:
    """Offre N jours de premium à un utilisateur (cumulable avec un abonnement existant)."""
    user = db.get(models.User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    user.subscription_tier = "premium"
    user.subscription_expires_at = pay.extend_subscription(
        user.subscription_expires_at, payload.days
    )
    db.commit()
    db.refresh(user)
    return user


@app.post("/admin/users/{user_id}/revoke-premium", response_model=schemas.UserOut)
def admin_revoke_premium(
    user_id: int,
    _: models.User = Depends(auth.require_admin),
    db: Session = Depends(get_db),
) -> models.User:
    """Retire l'abonnement premium (repasse en free, expire l'abonnement)."""
    user = db.get(models.User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    user.subscription_tier = "free"
    user.subscription_expires_at = None
    db.commit()
    db.refresh(user)
    return user


@app.post("/admin/users/{user_id}/reset-trial", response_model=schemas.UserOut)
def admin_reset_trial(
    user_id: int,
    _: models.User = Depends(auth.require_admin),
    db: Session = Depends(get_db),
) -> models.User:
    """Redémarre l'essai gratuit 7 jours de l'utilisateur (trial_started_at = maintenant)."""
    user = db.get(models.User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    user.trial_started_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)
    return user


@app.post("/admin/users/{user_id}/reset-password", response_model=schemas.AdminResetPasswordOut)
def admin_reset_password(
    user_id: int,
    admin_user: models.User = Depends(auth.require_admin),
    db: Session = Depends(get_db),
) -> schemas.AdminResetPasswordOut:
    """Réinitialise le mot de passe et renvoie le nouveau (à transmettre à l'utilisateur)."""
    import secrets as _secrets
    user = db.get(models.User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    new_password = _secrets.token_urlsafe(10)
    user.hashed_password = auth.hash_password(new_password)
    db.commit()
    return schemas.AdminResetPasswordOut(new_password=new_password)


@app.get("/admin/users/{user_id}/payments", response_model=list[schemas.AdminPaymentOut])
def admin_user_payments(
    user_id: int,
    _: models.User = Depends(auth.require_admin),
    db: Session = Depends(get_db),
) -> list[models.Payment]:
    """Historique des paiements d'un utilisateur."""
    user = db.get(models.User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    return (
        db.query(models.Payment)
        .filter(models.Payment.user_id == user_id)
        .order_by(models.Payment.created_at.desc())
        .all()
    )


@app.get("/admin/stats", response_model=schemas.AdminStats)
def admin_stats(
    _: models.User = Depends(auth.require_admin),
    db: Session = Depends(get_db),
) -> schemas.AdminStats:
    """Statistiques globales pour le tableau de bord admin."""
    from sqlalchemy import func as sa_func
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)

    users = db.query(models.User).all()
    users_total = len(users)
    users_active = sum(1 for u in users if u.active)
    users_suspended = users_total - users_active
    admins_total = sum(1 for u in users if u.role == "admin")
    trial_active = sum(1 for u in users if pay.is_trial_active(u.trial_started_at))
    premium_active = sum(
        1 for u in users if pay.is_premium_active(u.subscription_tier, u.subscription_expires_at)
    )

    payments_paid = (
        db.query(models.Payment).filter(models.Payment.status == "paid").count()
    )
    revenue_total = (
        db.query(sa_func.coalesce(sa_func.sum(models.Payment.amount_xof), 0))
        .filter(models.Payment.status == "paid")
        .scalar()
    ) or 0
    revenue_30d = (
        db.query(sa_func.coalesce(sa_func.sum(models.Payment.amount_xof), 0))
        .filter(models.Payment.status == "paid")
        .filter(models.Payment.paid_at >= thirty_days_ago)
        .scalar()
    ) or 0

    return schemas.AdminStats(
        users_total=users_total,
        users_active=users_active,
        users_suspended=users_suspended,
        admins_total=admins_total,
        trial_active=trial_active,
        premium_active=premium_active,
        payments_paid=payments_paid,
        revenue_xof_total=int(revenue_total),
        revenue_xof_30d=int(revenue_30d),
    )


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
# Abonnements & paiements Mobile Money
# ----------------------------------------------------------------------
@app.get("/billing/plans")
def billing_plans() -> dict[str, Any]:
    return {"plans": pay.PLANS}


@app.get("/billing/status", response_model=schemas.SubscriptionStatus)
def billing_status(
    user: models.User = Depends(auth.get_current_user),
) -> schemas.SubscriptionStatus:
    """Retourne l'état d'abonnement + essai gratuit de l'utilisateur courant."""
    premium_active = pay.is_premium_active(user.subscription_tier, user.subscription_expires_at)
    trial_active = pay.is_trial_active(user.trial_started_at)
    trial_end = pay.trial_expires_at(user.trial_started_at)
    days_remaining = 0
    if trial_end is not None:
        delta = trial_end - datetime.now(timezone.utc)
        days_remaining = max(0, delta.days + (1 if delta.seconds > 0 else 0))
    return schemas.SubscriptionStatus(
        tier=user.subscription_tier,
        premium_active=premium_active,
        premium_expires_at=user.subscription_expires_at,
        trial_active=trial_active,
        trial_expires_at=trial_end,
        trial_days_remaining=days_remaining if trial_active else 0,
        can_trade_real=premium_active or trial_active or user.role == "admin",
    )


@app.post("/billing/checkout", response_model=schemas.PaymentOut)
async def billing_checkout(
    payload: schemas.PaymentInit,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> schemas.PaymentOut:
    try:
        plan = pay.resolve_plan(payload.plan)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"Plan inconnu: {payload.plan}") from exc

    try:
        provider = pay.get_provider(payload.provider)
    except pay.PaymentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    base_url = os.environ.get("PUBLIC_BASE_URL", "https://api1.innovahub226.com")
    p = models.Payment(
        user_id=user.id,
        provider=provider.name,
        provider_ref="",
        amount_xof=int(plan["amount_xof"]),
        plan=payload.plan,
        status="pending",
    )
    db.add(p)
    db.flush()

    try:
        ref, checkout_url = await provider.create_checkout(
            amount_xof=int(plan["amount_xof"]),
            description=f"Deriv Trading Bot — {plan['label']}",
            callback_url=f"{base_url}/billing/webhook?payment_id={p.id}",
            customer_email=user.email,
            customer_phone=payload.phone,
        )
    except pay.PaymentError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    p.provider_ref = ref
    db.commit()
    db.refresh(p)

    out = schemas.PaymentOut.model_validate(p)
    return out.model_copy(update={"checkout_url": checkout_url})


@app.post("/billing/webhook", include_in_schema=False)
async def billing_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    # Récupère le payment_id ciblé (posé dans le callback_url à la création).
    payment_id = request.query_params.get("payment_id")
    if not payment_id:
        raise HTTPException(status_code=400, detail="payment_id manquant")
    payment = db.get(models.Payment, int(payment_id))
    if payment is None:
        raise HTTPException(status_code=404, detail="Paiement introuvable")

    raw = await request.body()
    try:
        provider = pay.get_provider(payment.provider)
        event = provider.verify_webhook(
            {k.lower(): v for k, v in request.headers.items()}, raw
        )
    except pay.PaymentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if payment.status == "paid":
        return {"status": "already_paid"}

    # La signature prouve l'origine, pas l'encaissement : on exige un statut de succès.
    if not pay.webhook_indicates_payment(event):
        payment.status = "failed"
        db.commit()
        logger.warning(
            "Webhook paiement %s sans statut de succès — abonnement non activé", payment.id
        )
        return {"status": "ignored"}

    plan = pay.resolve_plan(payment.plan)
    user = db.get(models.User, payment.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")

    payment.status = "paid"
    payment.paid_at = datetime.now(timezone.utc)
    user.subscription_tier = "premium"
    user.subscription_expires_at = pay.extend_subscription(
        user.subscription_expires_at, int(plan["duration_days"])
    )
    db.commit()
    return {"status": "activated"}


@app.get("/billing/dev-pay", include_in_schema=False)
async def billing_dev_pay(ref: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Endpoint de dev (provider `manual`) : confirme un paiement sans passerelle.

    Désactivé sauf si ALLOW_MANUAL_PAYMENTS=true : sans ce garde-fou, n'importe
    qui pourrait s'activer un abonnement premium sans payer.
    """
    if not pay.manual_payments_allowed():
        raise HTTPException(status_code=404, detail="Not Found")

    payment = db.query(models.Payment).filter(models.Payment.provider_ref == ref).first()
    if payment is None:
        raise HTTPException(status_code=404, detail="Paiement introuvable")
    if payment.status == "paid":
        return {"status": "already_paid"}
    plan = pay.resolve_plan(payment.plan)
    user = db.get(models.User, payment.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    payment.status = "paid"
    payment.paid_at = datetime.now(timezone.utc)
    user.subscription_tier = "premium"
    user.subscription_expires_at = pay.extend_subscription(
        user.subscription_expires_at, int(plan["duration_days"])
    )
    db.commit()
    return {"status": "activated", "plan": payment.plan}


# ----------------------------------------------------------------------
# Affiliation Deriv (IB)
# ----------------------------------------------------------------------
@app.get("/affiliate/deriv")
def affiliate_deriv_url() -> dict[str, str]:
    """URL de sign-up Deriv avec le token d'affiliation IB de l'app."""
    return {"url": pay.deriv_signup_url()}


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
