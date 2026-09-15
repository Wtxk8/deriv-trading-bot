"""Configuration commune des tests backend.

L'environnement de test est fixé AVANT tout import applicatif (database.py et
auth.py lisent leurs variables à l'import) :
- TRADING_DATABASE_URL : base SQLite temporaire, jamais backend/app.db ;
- JWT_SECRET : secret de test aléatoire.

Fixtures génériques fournies :
- make_user(**champs)   : crée un utilisateur en base (email unique) ;
- auth_headers(user)    : en-têtes "Authorization: Bearer <jwt>" ;
- make_app(*routeurs, **state) : FastAPI de test montant les routeurs voulus,
  avec les attributs de `app.state` donnés. main.py n'est jamais importé :
  son lifespan et ses routes restent hors des tests.

La base est partagée par tous les fichiers de test : emails uniques, et aucune
supposition de tables vides.
"""

from __future__ import annotations

import os
import secrets
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --- Environnement de test : AVANT tout import du backend -------------------
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

_TEST_DB_DIR = tempfile.mkdtemp(prefix="trading-tests-")
os.environ["TRADING_DATABASE_URL"] = (
    "sqlite:///" + Path(_TEST_DB_DIR, "test.db").as_posix()
)
os.environ["JWT_SECRET"] = "test-" + secrets.token_urlsafe(32)

import pytest  # noqa: E402
from fastapi import APIRouter, FastAPI  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
import models  # noqa: E402

# Mot de passe commun des utilisateurs de test (haché une seule fois : bcrypt
# est volontairement lent).
TEST_PASSWORD = "mot-de-passe-de-test"
_password_hash: str | None = None

# Sentinelle : « essai gratuit expiré » par défaut dans make_user.
_EXPIRED_TRIAL = object()

database.Base.metadata.create_all(bind=database.engine)


@pytest.fixture(scope="session", autouse=True)
def _test_database() -> Iterator[None]:
    """(Re)crée les tables une fois tous les modules de test importés."""
    database.Base.metadata.create_all(bind=database.engine)
    yield
    database.engine.dispose()
    shutil.rmtree(_TEST_DB_DIR, ignore_errors=True)


def _hashed_test_password() -> str:
    global _password_hash
    if _password_hash is None:
        _password_hash = auth.hash_password(TEST_PASSWORD)
    return _password_hash


@pytest.fixture
def make_user() -> Callable[..., models.User]:
    """Fabrique d'utilisateurs créés directement en base (sans /register).

    Par défaut : rôle "user", tier "free", essai gratuit EXPIRÉ (démarré il y
    a 30 jours). Passer trial_started_at=datetime.now(timezone.utc) pour un
    essai actif, ou subscription_tier="premium" pour un premium (expires_at
    None = premium à vie). L'objet retourné est détaché de sa session.
    """

    def _make(
        *,
        email: str | None = None,
        name: str = "Testeur",
        role: str = "user",
        active: bool = True,
        subscription_tier: str = "free",
        subscription_expires_at: datetime | None = None,
        trial_started_at: Any = _EXPIRED_TRIAL,
    ) -> models.User:
        if trial_started_at is _EXPIRED_TRIAL:
            trial_started_at = datetime.now(timezone.utc) - timedelta(days=30)
        with database.SessionLocal() as db:
            user = models.User(
                email=email or f"test-{uuid.uuid4().hex[:12]}@example.com",
                name=name,
                hashed_password=_hashed_test_password(),
                role=role,
                active=active,
                subscription_tier=subscription_tier,
                subscription_expires_at=subscription_expires_at,
                trial_started_at=trial_started_at,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            db.expunge(user)
            return user

    return _make


@pytest.fixture
def auth_headers() -> Callable[[models.User], dict[str, str]]:
    """En-têtes d'authentification JWT pour un utilisateur."""

    def _headers(user: models.User) -> dict[str, str]:
        return {"Authorization": f"Bearer {auth.create_access_token(user)}"}

    return _headers


@pytest.fixture
def make_app() -> Callable[..., FastAPI]:
    """FastAPI de test : make_app(router1, router2, bot_manager=..., ...)."""

    def _make(*routers: APIRouter, **state: Any) -> FastAPI:
        app = FastAPI()
        for router in routers:
            app.include_router(router)
        for key, value in state.items():
            setattr(app.state, key, value)
        return app

    return _make
