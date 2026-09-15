"""Chiffrement symétrique des tokens API Deriv stockés en base (copy trading).

Les suiveurs confient leur token Deriv au serveur pour que leurs ordres soient
répliqués pendant les sessions du maître : il ne doit JAMAIS être stocké ni
journalisé en clair. On utilise Fernet (AES-128-CBC + HMAC-SHA256, package
`cryptography`) avec la clé fournie par la variable d'environnement
COPY_TOKEN_KEY.

Génération d'une clé :
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Rotation : COPY_TOKEN_KEY accepte plusieurs clés séparées par des virgules.
La première chiffre, toutes déchiffrent (MultiFernet) : on ajoute la nouvelle
clé en tête, puis on retire l'ancienne une fois les tokens re-chiffrés.

Les messages d'erreur ne contiennent jamais le token ni la clé.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

ENV_KEY = "COPY_TOKEN_KEY"


class TokenCryptoError(RuntimeError):
    """Erreur de chiffrement/déchiffrement d'un token (message sans secret)."""


class TokenCryptoNotConfigured(TokenCryptoError):
    """COPY_TOKEN_KEY absente ou invalide."""


class TokenDecryptError(TokenCryptoError):
    """Donnée chiffrée illisible : clé différente ou donnée corrompue."""


def _raw_keys() -> list[str]:
    raw = os.environ.get(ENV_KEY, "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _load() -> MultiFernet:
    """Construit le chiffreur depuis l'environnement (relu à chaque appel)."""
    keys = _raw_keys()
    if not keys:
        raise TokenCryptoNotConfigured(
            f"{ENV_KEY} non définie : chiffrement des tokens indisponible."
        )
    try:
        return MultiFernet([Fernet(key.encode("ascii")) for key in keys])
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        # Ne pas chaîner le message d'origine : il pourrait citer la clé.
        raise TokenCryptoNotConfigured(
            f"{ENV_KEY} invalide : attendu une clé Fernet (32 octets en base64 urlsafe)."
        ) from None


def is_configured() -> bool:
    """True si une clé Fernet valide est disponible."""
    try:
        _load()
    except TokenCryptoNotConfigured:
        return False
    return True


def encrypt(plaintext: str) -> str:
    """Chiffre une chaîne et renvoie le jeton Fernet (ASCII) à stocker."""
    if not isinstance(plaintext, str) or not plaintext:
        raise TokenCryptoError("Valeur à chiffrer vide ou invalide.")
    return _load().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Déchiffre un jeton produit par `encrypt`."""
    if not isinstance(ciphertext, str) or not ciphertext:
        raise TokenDecryptError("Donnée chiffrée vide ou invalide.")
    fernet = _load()
    try:
        return fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError):
        raise TokenDecryptError(
            f"Token chiffré illisible ({ENV_KEY} différente ou donnée corrompue)."
        ) from None


def generate_key() -> str:
    """Nouvelle clé Fernet (pour l'installation ou la rotation)."""
    return Fernet.generate_key().decode("ascii")
