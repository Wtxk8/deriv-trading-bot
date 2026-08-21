"""Schémas Pydantic pour l'auth et l'administration des utilisateurs."""

from __future__ import annotations

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
