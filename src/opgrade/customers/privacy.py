"""Personal information handling under Québec's Loi 25.

- Collect the minimum: name + one contact; return address only for mail-in orders.
- Service consent (current notice version) is required before taking an order; marketing
  consent is separate, optional and off by default.
- Every decision or request is appended to `consent_records` (no personal data in it).
- Rights: access (export), rectification (client update), deletion (anonymization).
- Retention: clients inactive for `retention_years` are anonymized; grading results stay.

Not legal advice: the notice and periods must be validated by a lawyer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.models import Client, ConsentRecord, GradingSession, Order

Lang = Literal["fr", "en"]
OPEN_ORDER_STATUSES = ("received", "in_progress", "graded")


class PrivacyError(Exception):
    """Operation refused by the privacy rules. Message is operator-facing (French)."""


class Bilingual(BaseModel):
    fr: str
    en: str


class Responsible(BaseModel):
    name: str
    contact: str


class ConsentTexts(BaseModel):
    service: Bilingual
    marketing: Bilingual


class PrivacyPolicy(BaseModel):
    version: str
    responsible: Responsible
    retention_years: int
    notice: Bilingual
    consent: ConsentTexts

    def notice_text(self, lang: Lang = "fr") -> str:
        return getattr(self.notice, lang).format(
            retention_years=self.retention_years,
            responsible_name=self.responsible.name,
            responsible_contact=self.responsible.contact,
        )

    @property
    def is_configured(self) -> bool:
        """False while the privacy officer placeholder has not been filled in."""
        return "COMPLÉTER" not in self.responsible.name + self.responsible.contact


def load_policy(path: Path) -> PrivacyPolicy:
    with open(path, encoding="utf-8") as f:
        return PrivacyPolicy.model_validate(yaml.safe_load(f))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes even for timezone=True columns: they are UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# -- consent ---------------------------------------------------------------------------------


def record_consent(
    db: Session,
    client: Client,
    kind: Literal["service", "marketing"],
    granted: bool,
    policy: PrivacyPolicy,
    method: str = "touch",
    recorded_by: str | None = None,
) -> ConsentRecord:
    if client.anonymized_at:
        raise PrivacyError("Ce client a été anonymisé.")
    if kind == "service":
        if not granted:
            raise PrivacyError(
                "Retirer le consentement au service revient à demander la suppression : "
                "utilisez « Anonymiser le client »."
            )
        client.consent_service_at = _now()
        client.consent_policy_version = policy.version
    else:
        client.consent_marketing = granted
    record = ConsentRecord(
        client_id=client.id, kind=kind, granted=granted, policy_version=policy.version,
        method=method, recorded_by=recorded_by,
    )
    db.add(record)
    return record


def has_service_consent(client: Client, policy: PrivacyPolicy) -> bool:
    """Consent given for the CURRENT notice version (a new version needs a new consent)."""
    return (
        client.anonymized_at is None
        and client.consent_service_at is not None
        and client.consent_policy_version == policy.version
    )


# -- rights ----------------------------------------------------------------------------------


def export_client(db: Session, client: Client) -> dict:
    """Access request: everything held about the client, in a portable format."""
    orders = [
        {
            "number": o.number, "channel": o.channel, "status": o.status, "notes": o.notes,
            "tracking_in": o.tracking_in, "tracking_out": o.tracking_out, "return_address": o.return_address,
            "created_at": o.created_at, "returned_at": o.returned_at,
        }
        for o in client.orders
    ]
    cards = []
    for card in client.cards:
        sessions = db.scalars(select(GradingSession).where(GradingSession.card_id == card.id)).all()
        cards.append({
            "uuid": card.uuid, "name": card.name, "set_name": card.set_name, "year": card.year,
            "card_number": card.card_number, "notes": card.notes,
            "order": card.order.number if card.order else None,
            "gradings": [
                {"uuid": s.uuid, "state": s.state, "final_grade": s.final_grade, "completed_at": s.completed_at}
                for s in sessions
            ],
        })
    consents = db.scalars(select(ConsentRecord).where(ConsentRecord.client_id == client.id)).all()
    db.add(ConsentRecord(client_id=client.id, kind="access", granted=None))
    return {
        "exported_at": _now(),
        "client": {
            "uuid": client.uuid, "first_name": client.first_name, "last_name": client.last_name,
            "email": client.email, "phone": client.phone, "notes": client.notes, "created_at": client.created_at,
            "consent_service_at": client.consent_service_at, "consent_policy_version": client.consent_policy_version,
            "consent_marketing": client.consent_marketing,
        },
        "orders": orders,
        "cards": cards,
        "consent_history": [
            {"kind": c.kind, "granted": c.granted, "policy_version": c.policy_version, "method": c.method, "at": c.at}
            for c in consents
        ],
    }


def anonymize_client(db: Session, client: Client, reason: str, recorded_by: str | None = None) -> None:
    """Deletion request or end of retention: remove personal data, keep grading results."""
    if client.anonymized_at:
        return
    if any(o.status in OPEN_ORDER_STATUSES for o in client.orders):
        raise PrivacyError("Le client a une commande en cours : retournez ou annulez-la avant d'anonymiser.")
    client.first_name = "Client"
    client.last_name = f"anonymisé n° {client.id}"
    client.email = client.phone = client.notes = None
    client.consent_marketing = False
    client.anonymized_at = _now()
    for order in client.orders:
        order.return_address = order.notes = order.tracking_in = order.tracking_out = None
    for card in client.cards:
        card.notes = None  # free text may contain personal details
    db.add(ConsentRecord(client_id=client.id, kind="anonymization", reason=reason, recorded_by=recorded_by))


# -- retention -------------------------------------------------------------------------------


def last_activity(client: Client) -> datetime:
    dates = [client.created_at, client.consent_service_at]
    dates += [d for o in client.orders for d in (o.created_at, o.returned_at)]
    dates += [c.created_at for c in client.cards]
    return max(_aware(d) for d in dates if d is not None)


def clients_due_for_anonymization(db: Session, policy: PrivacyPolicy, now: datetime | None = None) -> list[Client]:
    limit = (now or _now()) - timedelta(days=365 * policy.retention_years)
    due = []
    for client in db.scalars(select(Client).where(Client.anonymized_at.is_(None))):
        if any(o.status in OPEN_ORDER_STATUSES for o in client.orders):
            continue
        if last_activity(client) < limit:
            due.append(client)
    return due


def open_orders(client: Client) -> list[Order]:
    return [o for o in client.orders if o.status in OPEN_ORDER_STATUSES]
