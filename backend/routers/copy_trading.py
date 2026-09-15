"""Routes du copy trading : suiveurs (/copy/...) et administration des maîtres.

Le service est lu dans `request.app.state.copy_service`. Absent ou désactivé
(COPY_TRADING_ENABLED != true ou COPY_TOKEN_KEY absente), les routes répondent
503 — sauf GET /copy/me qui renvoie simplement enabled=false.

Aucune erreur Deriv ne remonte en 5xx : token refusé ou compte introuvable
donnent un 400 avec un message affichable.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

import auth
import models
import rate_limit
from copy_trading import (
    AlreadyFollowing,
    CopyTradingService,
    CopyTradingUnavailable,
    MasterNotFound,
    NotFollowing,
    has_copy_access,
)

router = APIRouter(tags=["copy-trading"])

# Chaque tentative d'abonnement contacte Deriv avec le token fourni : débit
# limité pour que la route ne serve pas de banc d'essai de tokens.
_follow_limiter = rate_limit.SlidingWindowLimiter(
    max_attempts=10, window_seconds=600, name="copy_follow"
)


class FollowIn(BaseModel):
    master_id: int
    api_token: str = Field(min_length=1, max_length=512)
    account_type: Literal["demo", "real"]
    multiplier: float = Field(1.0, ge=0.1, le=10)
    max_stake: float = Field(10.0, ge=0.35, le=1000)
    daily_stop_loss: float = Field(20.0, ge=1, le=100000)
    consent: bool = False


class FollowPatch(BaseModel):
    multiplier: Optional[float] = Field(None, ge=0.1, le=10)
    max_stake: Optional[float] = Field(None, ge=0.35, le=1000)
    daily_stop_loss: Optional[float] = Field(None, ge=1, le=100000)
    active: Optional[bool] = None


class MasterIn(BaseModel):
    user_id: int
    display_name: str = Field(min_length=1, max_length=80)
    bio: str = Field("", max_length=500)


def _service_or_none(request: Request) -> Optional[CopyTradingService]:
    return getattr(request.app.state, "copy_service", None)


def require_copy_service(request: Request) -> CopyTradingService:
    service = _service_or_none(request)
    if service is None or not service.enabled:
        raise HTTPException(status_code=503, detail="Copy trading indisponible sur ce serveur")
    return service


# ---------------------------------------------------------------------------
# Suiveurs
# ---------------------------------------------------------------------------
@router.get("/copy/me")
def copy_me(
    request: Request, user: models.User = Depends(auth.get_current_user)
) -> dict[str, Any]:
    service = _service_or_none(request)
    if service is None or not service.enabled:
        return {"enabled": False, "is_master": False, "following": None, "followers_count": 0}
    return service.me(user.id)


@router.get("/copy/masters")
def copy_masters(
    user: models.User = Depends(auth.get_current_user),
    service: CopyTradingService = Depends(require_copy_service),
) -> list[dict[str, Any]]:
    return service.masters_with_stats()


@router.post("/copy/follow")
async def copy_follow(
    payload: FollowIn,
    user: models.User = Depends(auth.get_current_user),
    service: CopyTradingService = Depends(require_copy_service),
) -> dict[str, Any]:
    if not has_copy_access(user):
        raise HTTPException(
            status_code=402,
            detail="Essai gratuit ou abonnement premium requis pour le copy trading",
        )
    if payload.consent is not True:
        raise HTTPException(
            status_code=422,
            detail="Consentement requis : les ordres du maître seront répliqués sur votre compte Deriv",
        )
    if payload.master_id == user.id:
        raise HTTPException(status_code=400, detail="Impossible de vous suivre vous-même")
    _follow_limiter.check(f"user:{user.id}")
    try:
        return await service.validate_and_store_follow(
            user.id,
            master_id=payload.master_id,
            api_token=payload.api_token,
            account_type=payload.account_type,
            multiplier=payload.multiplier,
            max_stake=payload.max_stake,
            daily_stop_loss=payload.daily_stop_loss,
        )
    except MasterNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except AlreadyFollowing as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except CopyTradingUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.patch("/copy/follow")
async def copy_follow_update(
    payload: FollowPatch,
    user: models.User = Depends(auth.get_current_user),
    service: CopyTradingService = Depends(require_copy_service),
) -> dict[str, Any]:
    try:
        return await service.update_follow(user.id, **payload.model_dump(exclude_unset=True))
    except NotFollowing as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except CopyTradingUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.delete("/copy/follow", status_code=204, response_model=None)
async def copy_unfollow(
    user: models.User = Depends(auth.get_current_user),
    service: CopyTradingService = Depends(require_copy_service),
) -> Response:
    # Idempotent : 204 même si aucun abonnement n'existait.
    await service.delete_follow(user.id)
    return Response(status_code=204)


@router.get("/copy/trades")
def copy_trades(
    limit: int = Query(50, ge=1, le=200),
    user: models.User = Depends(auth.get_current_user),
    service: CopyTradingService = Depends(require_copy_service),
) -> list[dict[str, Any]]:
    return service.trades(user.id, limit)


# ---------------------------------------------------------------------------
# Administration des maîtres
# ---------------------------------------------------------------------------
@router.get("/admin/copy/masters")
def admin_copy_masters(
    admin: models.User = Depends(auth.require_admin),
    service: CopyTradingService = Depends(require_copy_service),
) -> list[dict[str, Any]]:
    return service.admin_list_masters()


@router.post("/admin/copy/masters")
def admin_copy_set_master(
    payload: MasterIn,
    admin: models.User = Depends(auth.require_admin),
    service: CopyTradingService = Depends(require_copy_service),
) -> dict[str, Any]:
    try:
        return service.admin_set_master(payload.user_id, payload.display_name, payload.bio)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.delete("/admin/copy/masters/{user_id}", status_code=204, response_model=None)
async def admin_copy_remove_master(
    user_id: int,
    admin: models.User = Depends(auth.require_admin),
    service: CopyTradingService = Depends(require_copy_service),
) -> Response:
    try:
        await service.admin_remove_master(user_id)
    except MasterNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return Response(status_code=204)
