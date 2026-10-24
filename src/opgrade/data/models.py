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
    # Loi 25: current consent state. The full history is in consent_records.
    consent_service_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_policy_version: Mapped[str | None] = mapped_column(String(30))
    consent_marketing: Mapped[bool | None] = mapped_column(Boolean, default=False)
    anonymized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    cards: Mapped[list["Card"]] = relationship(back_populates="client")
    orders: Mapped[list["Order"]] = relationship(back_populates="client")


class ConsentRecord(Base):
    """Append-only register of consent decisions and privacy requests (Loi 25).

    Holds no personal data itself, so it survives anonymization as proof of what was done.
    """

    __tablename__ = "consent_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    kind: Mapped[str] = mapped_column(String(20))  # service | marketing | access | anonymization
    granted: Mapped[bool | None] = mapped_column(Boolean)
    policy_version: Mapped[str | None] = mapped_column(String(30))
    method: Mapped[str | None] = mapped_column(String(20))  # touch | paper | verbal | email
    recorded_by: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Order(Base):
    """A batch of cards handed in by one client (counter, store or mail)."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, default=_uuid)
    number: Mapped[str | None] = mapped_column(String(30), unique=True)  # "OPG-2026-000042"
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    channel: Mapped[str] = mapped_column(String(20))  # counter | store | mail
    status: Mapped[str] = mapped_column(String(20), default="received")
    notes: Mapped[str | None] = mapped_column(Text)
    tracking_in: Mapped[str | None] = mapped_column(String(100))
    tracking_out: Mapped[str | None] = mapped_column(String(100))
    return_address: Mapped[str | None] = mapped_column(Text)  # personal data: cleared on anonymization
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    client: Mapped[Client] = relationship(back_populates="orders")
    cards: Mapped[list["Card"]] = relationship(back_populates="order")


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
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
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
    order: Mapped["Order | None"] = relationship(back_populates="cards")
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


class User(Base):
    """Machine account. Roles: operator (grading, clients, orders) and admin (everything)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(30), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # operator | admin
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSession(Base):
    """Login session. Only a SHA-256 of the cookie token is stored."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ip: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship()


class AuditEvent(Base):
    """Security journal: logins, account changes, sensitive actions. No personal client data."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    username: Mapped[str | None] = mapped_column(String(30))
    action: Mapped[str] = mapped_column(String(40))
    target: Mapped[str | None] = mapped_column(String(100))  # e.g. "client:12", "user:3"
    details: Mapped[dict | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(64))
