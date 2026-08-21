"""Hachage de mot de passe, émission/vérification JWT, dépendances d'accès."""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

import models
from database import get_db

load_dotenv()

logger = logging.getLogger("auth")

JWT_SECRET = os.environ.get("JWT_SECRET")
if not JWT_SECRET:
    JWT_SECRET = secrets.token_urlsafe(32)
    logger.warning(
        "JWT_SECRET non défini : clé générée à la volée (les sessions ne "
        "survivent pas à un redémarrage). Définir JWT_SECRET en production."
    )

JWT_ALGORITHM = "HS256"
JWT_EXPIRES_MINUTES = int(os.environ.get("JWT_EXPIRES_MINUTES", "720"))

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return _pwd_context.verify(password, hashed)


def create_access_token(user: models.User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=JWT_EXPIRES_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalide ou expiré",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentification requise",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    user = db.get(models.User, int(user_id)) if user_id is not None else None
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Utilisateur introuvable")
    if not user.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Compte suspendu")
    return user


def require_admin(user: models.User = Depends(get_current_user)) -> models.User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Accès réservé aux administrateurs")
    return user


def ensure_default_admin(db: Session) -> None:
    """Crée un compte admin par défaut au premier démarrage si aucun n'existe."""
    if db.query(models.User).filter(models.User.role == "admin").first() is not None:
        return

    email = os.environ.get("DEFAULT_ADMIN_EMAIL", "admin@innovahub226.com")
    password = os.environ.get("DEFAULT_ADMIN_PASSWORD")
    generated = password is None
    if generated:
        password = secrets.token_urlsafe(12)

    admin = models.User(
        name="Admin",
        email=email,
        hashed_password=hash_password(password),
        role="admin",
        active=True,
    )
    db.add(admin)
    db.commit()

    if generated:
        logger.warning(
            "Admin par défaut créé : %s / %s — mot de passe généré aléatoirement, "
            "à changer immédiatement (ou définir DEFAULT_ADMIN_PASSWORD avant le premier démarrage).",
            email,
            password,
        )
    else:
        logger.info("Admin par défaut créé : %s (mot de passe fourni via DEFAULT_ADMIN_PASSWORD).", email)
