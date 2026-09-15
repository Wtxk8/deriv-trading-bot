"""Modèle SQLAlchemy des signaux de trading publiés par signal_engine.

Séparé de models.py : il suffit d'importer ce module avant
`Base.metadata.create_all` pour créer la table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

SIGNAL_STATUSES = ("active", "hit_tp", "hit_sl", "expired")


class Signal(Base):
    """Signal publié (statut suivi tick par tick jusqu'à TP, SL ou expiration)."""

    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signals_created_at", "created_at"),
        Index("ix_signals_status", "status"),
        Index("ix_signals_symbol_status", "symbol", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    symbol_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    strategy: Mapped[str] = mapped_column(String(20), nullable=False)  # MA_CROSS | RSI | SPIKE
    direction: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY | SELL
    entry: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit: Mapped[float] = mapped_column(Float, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), default="1m", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # UTC
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="active", nullable=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    close_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(String(255), default="", nullable=False)
