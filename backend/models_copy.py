"""Modèles SQLAlchemy du copy trading (tables séparées de models.py).

- copy_masters       : utilisateurs désignés par un admin comme comptes maîtres ;
- copy_follows       : abonnement d'un suiveur à un maître (un seul par suiveur),
                       avec son token Deriv CHIFFRÉ (token_crypto) et ses limites ;
- copy_master_trades : contrats achetés par le bot d'un maître (stats publiques) ;
- copied_trades      : réplication de chaque contrat maître sur chaque suiveur.

Toutes les dates sont en UTC. Les montants sont dans la devise du compte Deriv
concerné. Importer ce module AVANT `Base.metadata.create_all` pour créer les
tables.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

import models  # noqa: F401  (déclare la table users, cible des clés étrangères)
from database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CopyMaster(Base):
    """Compte maître copiable (désigné par un admin, trade via NOTRE bot)."""

    __tablename__ = "copy_masters"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    bio: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    # Retrait par l'admin = désactivation (l'historique et les stats restent).
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class CopyFollow(Base):
    """Abonnement d'un suiveur à un maître (un seul maître par suiveur)."""

    __tablename__ = "copy_follows"

    id: Mapped[int] = mapped_column(primary_key=True)
    follower_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), unique=True, index=True, nullable=False
    )
    master_user_id: Mapped[int] = mapped_column(
        ForeignKey("copy_masters.user_id"), index=True, nullable=False
    )
    # Token API Deriv chiffré (Fernet) — JAMAIS en clair.
    encrypted_token: Mapped[str] = mapped_column(Text, nullable=False)
    account_type: Mapped[str] = mapped_column(String(10), nullable=False)  # demo | real
    account_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    account_currency: Mapped[str] = mapped_column(String(10), default="USD", nullable=False)
    multiplier: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    max_stake: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    daily_stop_loss: Mapped[float] = mapped_column(Float, default=20.0, nullable=False)
    # Choix de l'utilisateur (PATCH active) ; les pauses automatiques sont dans
    # paused_reason : daily_stop_loss | token_invalid | subscription_expired.
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    today_pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    pnl_day: Mapped[Optional[date]] = mapped_column(Date, nullable=True)  # jour UTC du compteur
    paused_reason: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class CopyMasterTrade(Base):
    """Contrat acheté par le bot d'un maître (base des statistiques publiques)."""

    __tablename__ = "copy_master_trades"
    __table_args__ = (
        # Idempotence : un même contrat maître n'est jamais copié deux fois.
        UniqueConstraint("master_user_id", "contract_id", name="uq_copy_master_trades_contract"),
        Index("ix_copy_master_trades_master_created", "master_user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    master_user_id: Mapped[int] = mapped_column(
        ForeignKey("copy_masters.user_id"), nullable=False
    )
    contract_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    contract_type: Mapped[str] = mapped_column(String(20), nullable=False)
    stake: Mapped[float] = mapped_column(Float, nullable=False)
    account_type: Mapped[str] = mapped_column(String(10), default="demo", nullable=False)
    profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class CopiedTrade(Base):
    """Réplication d'un contrat maître sur le compte d'un suiveur."""

    __tablename__ = "copied_trades"
    __table_args__ = (
        UniqueConstraint(
            "follower_user_id", "master_contract_id", name="uq_copied_trades_follower_contract"
        ),
        Index("ix_copied_trades_follow_status", "follow_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL une fois l'abonnement supprimé : l'historique du suiveur est conservé.
    follow_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("copy_follows.id", ondelete="SET NULL"), nullable=True
    )
    follower_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=False
    )
    master_contract_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    follower_contract_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    contract_type: Mapped[str] = mapped_column(String(20), nullable=False)
    stake: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # open | won | lost | failed | skipped
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
