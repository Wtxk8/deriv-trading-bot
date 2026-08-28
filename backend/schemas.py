"""Schémas Pydantic pour l'auth et l'administration des utilisateurs."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

_VALID_ROLES = {"user", "admin"}


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    name: Optional[str] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    role: str
    active: bool
    subscription_tier: str = "free"
    subscription_expires_at: Optional[datetime] = None
    trial_started_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SubscriptionStatus(BaseModel):
    tier: str  # free | premium
    premium_active: bool
    premium_expires_at: Optional[datetime] = None
    trial_active: bool
    trial_expires_at: Optional[datetime] = None
    trial_days_remaining: int = 0
    can_trade_real: bool


class PaymentInit(BaseModel):
    plan: str  # premium_monthly | premium_yearly
    provider: str = "fedapay"  # fedapay | cinetpay
    phone: Optional[str] = None  # numéro Mobile Money


class PaymentOut(BaseModel):
    id: int
    plan: str
    provider: str
    provider_ref: str
    amount_xof: int
    status: str
    created_at: datetime
    checkout_url: Optional[str] = None  # renvoyé pour rediriger l'utilisateur

    model_config = {"from_attributes": True}


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class AdminUserUpdate(BaseModel):
    role: Optional[str] = None
    active: Optional[bool] = None

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in _VALID_ROLES:
            raise ValueError(f"Rôle invalide: {value}")
        return value
