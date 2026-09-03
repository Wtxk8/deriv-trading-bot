"""Limitation de débit en mémoire (fenêtre glissante) pour les routes sensibles.

Protège contre le bruteforce sur /login, /register et les resets de mot de passe.
Un seul conteneur backend tourne : un compteur en mémoire suffit et évite
d'ajouter Redis. Si l'app passe à plusieurs répliques, migrer vers Redis.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from fastapi import HTTPException, Request

logger = logging.getLogger("rate_limit")

# Nombre max de tentatives conservées par clé (garde-fou mémoire).
_MAX_TRACKED_KEYS = 10_000


class SlidingWindowLimiter:
    """Compteur de tentatives par clé sur une fenêtre glissante."""

    def __init__(self, *, max_attempts: int, window_seconds: int, name: str) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.name = name
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        window = self._hits.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while window and window[0] < cutoff:
            window.popleft()
        return window

    def check(self, key: str) -> None:
        """Enregistre une tentative. Lève HTTPException 429 si le quota est dépassé."""
        now = time.monotonic()
        with self._lock:
            # Garde-fou : si la table explose, on purge les clés vides.
            if len(self._hits) > _MAX_TRACKED_KEYS:
                self._gc(now)

            window = self._prune(key, now)
            if len(window) >= self.max_attempts:
                retry_after = int(self.window_seconds - (now - window[0])) + 1
                logger.warning("Rate limit %s dépassé pour %s", self.name, key)
                raise HTTPException(
                    status_code=429,
                    detail=(
                        "Trop de tentatives. Réessayez dans "
                        f"{retry_after} seconde(s)."
                    ),
                    headers={"Retry-After": str(retry_after)},
                )
            window.append(now)

    def reset(self, key: str) -> None:
        """Efface le compteur d'une clé (appelé après un succès légitime)."""
        with self._lock:
            self._hits.pop(key, None)

    def _gc(self, now: float) -> None:
        cutoff = now - self.window_seconds
        stale = [k for k, w in self._hits.items() if not w or w[-1] < cutoff]
        for k in stale:
            self._hits.pop(k, None)


def client_ip(request: Request) -> str:
    """IP réelle du client derrière le tunnel Cloudflare.

    Cloudflare pose CF-Connecting-IP ; on retombe sur X-Forwarded-For puis
    sur l'IP de la socket. Les en-têtes ne sont fiables que parce que l'origine
    n'est joignable QUE via le tunnel (port 8000 non exposé publiquement).
    """
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --- Limiteurs configurés ---

# Bruteforce mot de passe : 8 essais / 5 min par IP.
login_limiter = SlidingWindowLimiter(max_attempts=8, window_seconds=300, name="login-ip")

# Bruteforce ciblé sur un compte précis : 5 essais / 15 min par email.
login_account_limiter = SlidingWindowLimiter(
    max_attempts=5, window_seconds=900, name="login-account"
)

# Création de comptes en masse : 5 inscriptions / heure par IP.
register_limiter = SlidingWindowLimiter(
    max_attempts=5, window_seconds=3600, name="register-ip"
)
