"""Certificate numbers and what the QR code on a report points to."""

from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.models import GradingSession

# No 0/O, 1/I/L: read aloud or typed from the slab label without ambiguity.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def new_certificate_number() -> str:
    """Random, not sequential: certificates cannot be enumerated. 31^8 ≈ 8.5e11 values."""
    code = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"OPG-{code[:4]}-{code[4:]}"


def assign_certificate(db: Session, session: GradingSession) -> str:
    if session.certificate_number:
        return session.certificate_number
    while True:
        number = new_certificate_number()
        if db.scalar(select(GradingSession.id).where(GradingSession.certificate_number == number)) is None:
            session.certificate_number = number
            return number


def normalize_certificate(text: str) -> str:
    """'opg7kq2m9xd' or 'OPG-7KQ2-M9XD' -> 'OPG-7KQ2-M9XD' (as typed by a buyer)."""
    raw = "".join(ch for ch in text.upper() if ch.isalnum()).removeprefix("OPG")
    return f"OPG-{raw[:4]}-{raw[4:8]}" if len(raw) == 8 else text.upper()


def qr_payload(certificate_number: str, verify_url: str | None) -> str:
    if verify_url:
        return f"{verify_url.rstrip('/')}/verify/{certificate_number}"
    return f"OPGRADE CERT {certificate_number}"
