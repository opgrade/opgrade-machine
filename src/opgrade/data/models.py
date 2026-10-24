"""Local database schema (SQLite). Every table carries a UUID for future PostgreSQL sync."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, default=_uuid)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(50))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    cards: Mapped[list["Card"]] = relationship(back_populates="client")


class CardType(Base):
    """Analysis profile for a card category (dimensions, category)."""

    __tablename__ = "card_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(50))  # tcg | sport | other
    width_mm: Mapped[float] = mapped_column(Float)
    height_mm: Mapped[float] = mapped_column(Float)
    thickness_mm: Mapped[float | None] = mapped_column(Float)
    has_border: Mapped[bool] = mapped_column(Boolean, default=True)
    # Online catalog used to identify cards of this type (see identify.CATALOGS); None = manual entry.
    catalog: Mapped[str | None] = mapped_column(String(30))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Card(Base):
    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, default=_uuid)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    card_type_id: Mapped[int] = mapped_column(ForeignKey("card_types.id"))
    name: Mapped[str] = mapped_column(String(200))
    set_name: Mapped[str | None] = mapped_column(String(200))
    year: Mapped[int | None] = mapped_column(Integer)
    card_number: Mapped[str | None] = mapped_column(String(50))
    language: Mapped[str | None] = mapped_column(String(20))
    rarity: Mapped[str | None] = mapped_column(String(100))
    # Catalog reference when the card was identified online (None = typed by the operator).
    external_source: Mapped[str | None] = mapped_column(String(30))
    external_id: Mapped[str | None] = mapped_column(String(100))
    reference_image_url: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    client: Mapped[Client] = relationship(back_populates="cards")
    card_type: Mapped[CardType] = relationship()
    sessions: Mapped[list["GradingSession"]] = relationship(back_populates="card")


class GradingSession(Base):
    __tablename__ = "grading_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, default=_uuid)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id"))
    machine_id: Mapped[str] = mapped_column(String(50))
    state: Mapped[str] = mapped_column(String(30))
    simulated: Mapped[bool] = mapped_column(Boolean)
    operator: Mapped[str | None] = mapped_column(String(100))
    rules_version: Mapped[str] = mapped_column(String(50))
    model_version: Mapped[str | None] = mapped_column(String(50))
    final_grade: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=True)
    explanation: Mapped[list | None] = mapped_column(JSON)
    last_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    card: Mapped[Card] = relationship(back_populates="sessions")
    captures: Mapped[list["Capture"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    measurements: Mapped[list["Measurement"]] = relationship(cascade="all, delete-orphan")
    subgrades: Mapped[list["Subgrade"]] = relationship(cascade="all, delete-orphan")


class Capture(Base):
    __tablename__ = "captures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, default=_uuid)
    session_id: Mapped[int] = mapped_column(ForeignKey("grading_sessions.id"))
    side: Mapped[str] = mapped_column(String(10))
    lighting: Mapped[str] = mapped_column(String(30))
    raw_path: Mapped[str] = mapped_column(String(500))
    warped_path: Mapped[str] = mapped_column(String(500))
    width_px: Mapped[int] = mapped_column(Integer)
    height_px: Mapped[int] = mapped_column(Integer)
    sharpness: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    session: Mapped[GradingSession] = relationship(back_populates="captures")


class Measurement(Base):
    __tablename__ = "measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("grading_sessions.id"))
    side: Mapped[str | None] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(50))
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(20))


class Subgrade(Base):
    __tablename__ = "subgrades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("grading_sessions.id"))
    criterion: Mapped[str] = mapped_column(String(30))
    value: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(20))  # auto | operator | model
    confidence: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict | None] = mapped_column(JSON)
