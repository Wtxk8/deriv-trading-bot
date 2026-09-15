"""Événements de trading partagés entre le moteur de bot et ses abonnés.

Le moteur (BotEngine) émet ces événements ; le gestionnaire multi-utilisateurs
(BotManager) les relaie aux abonnés enregistrés, notamment le service de copy
trading qui réplique les ordres d'un compte maître sur ses comptes suiveurs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Optional


@dataclass(frozen=True, slots=True)
class TradeOpenedEvent:
    """Un contrat vient d'être acheté avec succès par le bot d'un utilisateur."""

    user_id: int
    contract_id: int
    contract_type: str  # CALLE | PUTE | DIGITOVER | DIGITUNDER
    symbol: str
    stake: float
    duration: int
    duration_unit: str  # "t" (ticks) | "s" | "m"
    barrier: Optional[str]
    currency: str
    account_type: str  # demo | real
    account_balance: float  # solde estimé du compte au moment de l'achat


@dataclass(frozen=True, slots=True)
class TradeSettledEvent:
    """Un contrat du bot d'un utilisateur vient d'être réglé (gagné ou perdu)."""

    user_id: int
    contract_id: int
    profit: float
    payout: float


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """Début ou fin d'une session de bot (arrêt manuel, SL/TP atteint, erreur)."""

    user_id: int
    kind: Literal["started", "stopped"]
    account_type: str  # demo | real


TradeOpenedListener = Callable[[TradeOpenedEvent], Awaitable[None]]
TradeSettledListener = Callable[[TradeSettledEvent], Awaitable[None]]
SessionListener = Callable[[SessionEvent], Awaitable[None]]
