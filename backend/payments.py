"""Module paiements Mobile Money (FedaPay + CinetPay) + gestion des abonnements.

Deux endpoints principaux :
- POST /billing/checkout : initie un paiement Mobile Money (renvoie une URL de checkout).
- POST /billing/webhook  : callback signé du fournisseur → active l'abonnement.

Un provider de dev (`manual`) est disponible pour tester sans compte marchand.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger("payments")

# ----- Tarifs (en XOF, franc CFA) -----
PLANS: dict[str, dict[str, Any]] = {
    "premium_monthly": {
        "amount_xof": 6000,
        "duration_days": 30,
        "label": "Premium mensuel",
    },
    "premium_yearly": {
        "amount_xof": 55000,
        "duration_days": 365,
        "label": "Premium annuel (2 mois offerts)",
    },
}


def resolve_plan(plan: str) -> dict[str, Any]:
    """Retourne la config d'un plan ou lève KeyError."""
    return PLANS[plan]


class PaymentError(RuntimeError):
    pass


class PaymentProvider:
    """Interface commune. Implémentations : FedaPay, CinetPay, Manual."""

    name: str = "base"

    async def create_checkout(
        self,
        *,
        amount_xof: int,
        description: str,
        callback_url: str,
        customer_email: str | None,
        customer_phone: str | None,
    ) -> tuple[str, str]:
        """Renvoie (provider_ref, checkout_url)."""
        raise NotImplementedError

    def verify_webhook(self, headers: dict[str, str], raw_body: bytes) -> dict[str, Any]:
        """Vérifie la signature d'un webhook et parse le corps. Lève PaymentError si invalide."""
        raise NotImplementedError


class ManualProvider(PaymentProvider):
    """Provider de dev — pas d'API externe, checkout renvoyé vers /billing/dev-pay."""

    name = "manual"

    async def create_checkout(self, **kwargs: Any) -> tuple[str, str]:
        ref = "manual_" + secrets.token_urlsafe(10)
        base = os.environ.get("PUBLIC_BASE_URL", "https://api1.innovahub226.com")
        return ref, f"{base}/billing/dev-pay?ref={ref}"

    def verify_webhook(self, headers: dict[str, str], raw_body: bytes) -> dict[str, Any]:
        # Le provider manuel ne reçoit pas de webhook — la route dev-pay valide directement.
        raise PaymentError("Provider manuel : pas de webhook.")


class FedaPayProvider(PaymentProvider):
    """FedaPay — populaire au Bénin, Sénégal, Côte d'Ivoire. Doc: docs.fedapay.com/"""

    name = "fedapay"
    BASE_URL = "https://api.fedapay.com/v1"

    def __init__(self, secret_key: str, webhook_secret: str) -> None:
        if not secret_key:
            raise PaymentError("FEDAPAY_SECRET_KEY non configuré")
        self._secret_key = secret_key
        self._webhook_secret = webhook_secret

    async def create_checkout(
        self,
        *,
        amount_xof: int,
        description: str,
        callback_url: str,
        customer_email: str | None,
        customer_phone: str | None,
    ) -> tuple[str, str]:
        payload = {
            "description": description,
            "amount": amount_xof,
            "currency": {"iso": "XOF"},
            "callback_url": callback_url,
            "customer": {
                "email": customer_email or "",
                "phone_number": {"number": customer_phone or "", "country": "CI"},
            },
        }
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.post(
                f"{self.BASE_URL}/transactions",
                headers={
                    "Authorization": f"Bearer {self._secret_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if r.status_code >= 400:
            raise PaymentError(f"FedaPay HTTP {r.status_code}: {r.text[:200]}")
        data = r.json().get("v1/transaction") or r.json().get("transaction") or {}
        ref = str(data.get("id") or data.get("reference") or "")
        # Génère un token de paiement pour construire l'URL de checkout hébergée.
        async with httpx.AsyncClient(timeout=20) as http:
            tk = await http.post(
                f"{self.BASE_URL}/transactions/{ref}/token",
                headers={"Authorization": f"Bearer {self._secret_key}"},
            )
        if tk.status_code >= 400:
            raise PaymentError(f"FedaPay token HTTP {tk.status_code}: {tk.text[:200]}")
        token_data = tk.json()
        checkout_url = token_data.get("url") or token_data.get("token", {}).get("url", "")
        if not checkout_url:
            raise PaymentError("FedaPay : url de checkout absente")
        return ref, checkout_url

    def verify_webhook(self, headers: dict[str, str], raw_body: bytes) -> dict[str, Any]:
        signature = headers.get("x-fedapay-signature") or headers.get("X-FedaPay-Signature", "")
        if not signature or not self._webhook_secret:
            raise PaymentError("Signature webhook manquante")
        expected = hmac.new(
            self._webhook_secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise PaymentError("Signature invalide")
        try:
            import json

            return json.loads(raw_body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise PaymentError(f"Payload JSON invalide: {exc}") from exc


def get_provider(name: str) -> PaymentProvider:
    """Instancie un provider selon la config d'env."""
    name = (name or "").lower().strip()
    if name in ("manual", "dev", ""):
        return ManualProvider()
    if name == "fedapay":
        return FedaPayProvider(
            secret_key=os.environ.get("FEDAPAY_SECRET_KEY", ""),
            webhook_secret=os.environ.get("FEDAPAY_WEBHOOK_SECRET", ""),
        )
    raise PaymentError(f"Provider inconnu: {name}")


def is_premium_active(user_tier: str, expires_at: datetime | None) -> bool:
    """True si l'utilisateur a un abonnement premium actif à cette seconde."""
    if user_tier != "premium":
        return False
    if expires_at is None:
        return True  # premium lifetime (offert par admin, cadeau, etc.)
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return now < expires_at


def extend_subscription(current_expires_at: datetime | None, duration_days: int) -> datetime:
    """Calcule la nouvelle date d'expiration après paiement."""
    now = datetime.now(timezone.utc)
    base = current_expires_at
    if base is None or base < now:
        base = now
    elif base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base + timedelta(days=duration_days)


# ---------------------------------------------------------------------
# Affiliation Deriv (IB)
# ---------------------------------------------------------------------
def deriv_signup_url(referrer_token: str | None = None) -> str:
    """Retourne l'URL de sign-up Deriv avec le token d'affiliation configuré."""
    base = "https://track.deriv.com/_"
    token = referrer_token or os.environ.get("DERIV_AFFILIATE_TOKEN", "")
    if not token:
        return "https://deriv.com/signup/"
    return f"{base}{token}/1/"
