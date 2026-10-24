"""Clients (Loi 25: consent, access, rectification, anonymization) and orders routes."""

from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import audit
from ..customers import orders as order_rules
from ..customers import privacy
from ..data import Client, Order
from . import schemas
from .auth import client_ip, current_user


def client_out(client: Client, policy: privacy.PrivacyPolicy) -> dict:
    data = schemas.ClientOut.model_validate(client).model_dump()
    data["consent_valid"] = privacy.has_service_consent(client, policy)
    return data


def order_out(order: Order, with_cards: bool = False) -> dict:
    counts = order_rules.progress(order)
    data = {
        **{f: getattr(order, f) for f in (
            "id", "uuid", "number", "client_id", "channel", "status", "notes", "tracking_in", "tracking_out",
            "return_address", "created_at", "graded_at", "returned_at",
        )},
        "client_name": f"{order.client.first_name} {order.client.last_name}",
        "cards_count": counts["cards"],
        "graded_count": counts["graded"],
    }
    if with_cards:
        data["cards"] = []
        for card in order.cards:
            latest = max(card.sessions, key=lambda s: s.id, default=None)
            data["cards"].append({
                "id": card.id, "name": card.name, "set_name": card.set_name, "card_number": card.card_number,
                "card_type": card.card_type.name,
                "session_id": latest.id if latest else None,
                "session_state": latest.state if latest else None,
                "final_grade": latest.final_grade if latest else None,
            })
    return data


def build_router(get_db: Callable, policy: privacy.PrivacyPolicy, require_admin: Callable) -> APIRouter:
    router = APIRouter()
    admin = [Depends(require_admin)]

    def get_client(db: Session, client_id: int) -> Client:
        client = db.get(Client, client_id)
        if client is None:
            raise HTTPException(404, "Client introuvable.")
        return client

    def get_order(db: Session, order_id: int) -> Order:
        order = db.get(Order, order_id)
        if order is None:
            raise HTTPException(404, "Commande introuvable.")
        return order

    # -- privacy notice -------------------------------------------------------------------

    @router.get("/api/privacy/notice", response_model=schemas.PrivacyNoticeOut)
    def privacy_notice(lang: privacy.Lang = "fr"):
        return {
            "version": policy.version,
            "lang": lang,
            "notice": policy.notice_text(lang),
            "consent_service": getattr(policy.consent.service, lang),
            "consent_marketing": getattr(policy.consent.marketing, lang),
            "retention_years": policy.retention_years,
            "responsible_configured": policy.is_configured,
        }

    # -- clients --------------------------------------------------------------------------

    @router.get("/api/clients", response_model=list[schemas.ClientOut])
    def list_clients(q: str | None = None, include_anonymized: bool = False, db: Session = Depends(get_db)):
        stmt = select(Client).order_by(Client.last_name, Client.first_name)
        if not include_anonymized:
            stmt = stmt.where(Client.anonymized_at.is_(None))
        if q:
            like = f"%{q}%"
            stmt = stmt.where(Client.first_name.ilike(like) | Client.last_name.ilike(like) | Client.email.ilike(like))
        return [client_out(c, policy) for c in db.scalars(stmt)]

    @router.post("/api/clients", response_model=schemas.ClientOut, status_code=201)
    def create_client(body: schemas.ClientIn, request: Request, db: Session = Depends(get_db)):
        if body.consent_policy_version != policy.version:
            raise HTTPException(409, "L'avis de confidentialité a changé : rechargez la page et faites relire l'avis.")
        client = Client(**body.model_dump(include=set(schemas.ClientFields.model_fields)))
        db.add(client)
        db.flush()
        by = current_user(request).username
        privacy.record_consent(db, client, "service", True, policy, body.consent_method, by)
        privacy.record_consent(db, client, "marketing", body.consent_marketing, policy, body.consent_method, by)
        db.commit()
        return client_out(client, policy)

    @router.get("/api/clients/{client_id}", response_model=schemas.ClientOut)
    def read_client(client_id: int, db: Session = Depends(get_db)):
        return client_out(get_client(db, client_id), policy)

    @router.patch("/api/clients/{client_id}", response_model=schemas.ClientOut)
    def update_client(client_id: int, body: schemas.ClientUpdate, db: Session = Depends(get_db)):
        client = get_client(db, client_id)
        if client.anonymized_at:
            raise HTTPException(409, "Ce client a été anonymisé.")
        for field, value in body.model_dump(exclude_unset=True).items():
            setattr(client, field, value)
        if not (client.email or client.phone):
            raise HTTPException(422, "Un moyen de contact est requis : courriel ou téléphone.")
        db.commit()
        return client_out(client, policy)

    @router.post("/api/clients/{client_id}/consent", response_model=schemas.ClientOut)
    def give_consent(client_id: int, body: schemas.ConsentIn, request: Request, db: Session = Depends(get_db)):
        client = get_client(db, client_id)
        if body.policy_version != policy.version:
            raise HTTPException(409, "L'avis de confidentialité a changé : rechargez la page et faites relire l'avis.")
        privacy.record_consent(db, client, body.kind, body.granted, policy, body.method, current_user(request).username)
        db.commit()
        return client_out(client, policy)

    @router.get("/api/clients/{client_id}/export", dependencies=admin)
    def export_client(client_id: int, request: Request, db: Session = Depends(get_db)):
        client = get_client(db, client_id)
        data = privacy.export_client(db, client)
        audit(db, current_user(request).username, "client_export", target=f"client:{client.id}", ip=client_ip(request))
        db.commit()  # the access request itself is logged
        return Response(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="client-{client.uuid}.json"'},
        )

    @router.post("/api/clients/{client_id}/anonymize", response_model=schemas.ClientOut, dependencies=admin)
    def anonymize(client_id: int, body: schemas.AnonymizeIn, request: Request, db: Session = Depends(get_db)):
        client = get_client(db, client_id)
        by = current_user(request).username
        privacy.anonymize_client(db, client, body.reason, by)
        audit(db, by, "client_anonymized", target=f"client:{client.id}", ip=client_ip(request))
        db.commit()
        return client_out(client, policy)

    @router.get("/api/privacy/retention", response_model=list[schemas.ClientOut], dependencies=admin)
    def retention_due(db: Session = Depends(get_db)):
        """Clients past the retention period (no open order): to be anonymized."""
        return [client_out(c, policy) for c in privacy.clients_due_for_anonymization(db, policy)]

    @router.post("/api/privacy/retention/apply", dependencies=admin)
    def retention_apply(request: Request, db: Session = Depends(get_db)) -> dict:
        by = current_user(request).username
        due = privacy.clients_due_for_anonymization(db, policy)
        for client in due:
            privacy.anonymize_client(db, client, f"Fin de la période de conservation ({policy.retention_years} ans)", by)
        audit(db, by, "retention_applied", details={"anonymized": len(due)}, ip=client_ip(request))
        db.commit()
        return {"anonymized": len(due)}

    # -- orders ---------------------------------------------------------------------------

    @router.get("/api/orders", response_model=list[schemas.OrderOut])
    def list_orders(status: str = "open", client_id: int | None = None, limit: int = 100, db: Session = Depends(get_db)):
        stmt = select(Order).order_by(Order.id.desc()).limit(min(limit, 500))
        if status == "open":
            stmt = stmt.where(Order.status.in_(privacy.OPEN_ORDER_STATUSES))
        elif status != "all":
            stmt = stmt.where(Order.status == status)
        if client_id is not None:
            stmt = stmt.where(Order.client_id == client_id)
        return [order_out(o) for o in db.scalars(stmt)]

    @router.post("/api/orders", response_model=schemas.OrderDetailOut, status_code=201)
    def create_order(body: schemas.OrderIn, db: Session = Depends(get_db)):
        client = get_client(db, body.client_id)
        order = order_rules.create_order(
            db, client, body.channel, policy, body.notes, body.tracking_in, body.return_address
        )
        db.commit()
        return order_out(order, with_cards=True)

    @router.get("/api/orders/{order_id}", response_model=schemas.OrderDetailOut)
    def read_order(order_id: int, db: Session = Depends(get_db)):
        return order_out(get_order(db, order_id), with_cards=True)

    @router.post("/api/orders/{order_id}/return", response_model=schemas.OrderDetailOut)
    def return_order(order_id: int, body: schemas.ReturnIn, db: Session = Depends(get_db)):
        order = get_order(db, order_id)
        order_rules.mark_returned(order, body.tracking_out)
        db.commit()
        return order_out(order, with_cards=True)

    @router.post("/api/orders/{order_id}/cancel", response_model=schemas.OrderDetailOut)
    def cancel_order(order_id: int, db: Session = Depends(get_db)):
        order = get_order(db, order_id)
        order_rules.cancel_order(order)
        db.commit()
        return order_out(order, with_cards=True)

    return router
