"""Orders: a batch of cards from one client, followed from reception to return.

Status: received -> in_progress (first grading started) -> graded (every card graded)
        -> returned (cards handed back / shipped). cancelled at any time before return.
in_progress/graded are derived from the cards' gradings by `refresh_status`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.orm import Session

from ..data.models import Card, Client, Order
from .privacy import PrivacyError, PrivacyPolicy, has_service_consent

Channel = Literal["counter", "store", "mail"]
CHANNEL_LABELS = {"counter": "Comptoir", "store": "Boutique / revendeur", "mail": "Envoi postal"}
ACCEPTS_CARDS = ("received", "in_progress", "graded")


class OrderError(Exception):
    """Operation not allowed on this order. Message is operator-facing (French)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_order(
    db: Session,
    client: Client,
    channel: Channel,
    policy: PrivacyPolicy,
    notes: str | None = None,
    tracking_in: str | None = None,
    return_address: str | None = None,
) -> Order:
    if client.anonymized_at:
        raise PrivacyError("Ce client a été anonymisé : créez une nouvelle fiche client.")
    if not has_service_consent(client, policy):
        raise PrivacyError("Consentement requis : faites lire l'avis de confidentialité au client et enregistrez son accord.")
    if channel == "mail" and not (return_address or "").strip():
        raise OrderError("Adresse de retour obligatoire pour un envoi postal.")
    order = Order(
        client=client, channel=channel, status="received", notes=notes, tracking_in=tracking_in,
        # Only collected when needed (mail-in): minimal collection.
        return_address=return_address if channel == "mail" else None,
    )
    db.add(order)
    db.flush()
    order.number = f"OPG-{order.created_at.year}-{order.id:06d}"
    return order


def check_card_can_join(order: Order, client_id: int) -> None:
    if order.client_id != client_id:
        raise OrderError("Cette commande appartient à un autre client.")
    if order.status not in ACCEPTS_CARDS:
        raise OrderError("Commande retournée ou annulée : impossible d'y ajouter une carte.")


def card_is_graded(card: Card) -> bool:
    return any(s.state == "completed" for s in card.sessions)


def refresh_status(order: Order | None) -> None:
    if order is None or order.status in ("returned", "cancelled"):
        return
    cards = order.cards
    if cards and all(card_is_graded(c) for c in cards):
        if order.status != "graded":
            order.status, order.graded_at = "graded", _now()
    elif any(c.sessions for c in cards):
        order.status, order.graded_at = "in_progress", None
    else:
        order.status, order.graded_at = "received", None


def mark_returned(order: Order, tracking_out: str | None = None) -> None:
    refresh_status(order)
    if order.status != "graded":
        raise OrderError("Toutes les cartes de la commande doivent être gradées avant le retour.")
    order.status, order.returned_at = "returned", _now()
    if tracking_out:
        order.tracking_out = tracking_out


def cancel_order(order: Order) -> None:
    if order.status == "returned":
        raise OrderError("Commande déjà retournée.")
    order.status = "cancelled"


def progress(order: Order) -> dict:
    graded = sum(card_is_graded(c) for c in order.cards)
    return {"cards": len(order.cards), "graded": graded}
