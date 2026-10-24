"""Grading report (PDF) and the optional public certificate verification page."""

from __future__ import annotations

from collections.abc import Callable
from html import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..data import GradingSession
from ..reports import assign_certificate, build_report, normalize_certificate
from ..reports.pdf import CRITERIA, GRADE_NAMES


def build_router(get_db: Callable, settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/api/sessions/{session_id}/report.pdf")
    def report(session_id: int, db: Session = Depends(get_db)):
        gs = db.get(GradingSession, session_id)
        if gs is None:
            raise HTTPException(404, "Session introuvable.")
        if gs.state != "completed":
            raise HTTPException(409, "Le rapport est disponible une fois la gradation terminée.")
        if not gs.certificate_number:  # graded before certificates existed
            assign_certificate(db, gs)
            db.commit()
        pdf = build_report(gs, settings.data_dir, settings.verify_url)
        return Response(
            pdf, media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="opgrade-{gs.certificate_number}.pdf"'},
        )

    if settings.public_verification:

        @router.get("/verify/{certificate}", response_class=HTMLResponse, include_in_schema=False)
        def verify(certificate: str, db: Session = Depends(get_db)):
            """Public: card and grade only, never any client information."""
            number = normalize_certificate(certificate)
            gs = db.scalar(select(GradingSession).where(GradingSession.certificate_number == number))
            if gs is None or gs.state != "completed":
                return HTMLResponse(_page("Certificat introuvable",
                                          "<p>Aucun certificat OpGrade ne porte ce numéro.</p>"), status_code=404)
            card = gs.card
            subs = {s.criterion: s.value for s in gs.subgrades}
            rows = "".join(f"<tr><td>{escape(label)}</td><td>{subs.get(key, '-'):g}</td></tr>"
                           for key, label in CRITERIA if key in subs)
            details = " · ".join(escape(str(v)) for v in (card.set_name, card.card_number, card.year) if v)
            warning = '<p class="sim">ANALYSE SIMULÉE — CERTIFICAT NON VALIDE</p>' if gs.simulated else ""
            body = f"""{warning}
              <p class="cert">{escape(gs.certificate_number)}</p>
              <h1>{escape(card.name)}</h1><p>{details}</p>
              <p class="grade">{gs.final_grade} <span>{GRADE_NAMES.get(gs.final_grade, "")}</span></p>
              <table>{rows}</table>
              <p class="muted">Gradée le {gs.completed_at:%Y-%m-%d} · règles {escape(gs.rules_version)}</p>"""
            return HTMLResponse(_page(f"Certificat {gs.certificate_number}", body))

    return router


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)} — OpGrade</title>
<style>body{{font-family:system-ui,sans-serif;max-width:520px;margin:40px auto;padding:0 16px;color:#16181d}}
.cert{{font-weight:700;letter-spacing:.08em}}.grade{{font-size:4rem;font-weight:800;margin:8px 0}}
.grade span{{font-size:1.2rem;color:#5d6472}}td{{padding:6px 12px 6px 0}}.muted{{color:#5d6472;font-size:.85rem}}
.sim{{background:#e5484d;color:#fff;padding:8px;border-radius:6px;font-weight:700}}</style></head>
<body><p><b>OpGrade</b> — vérification de certificat</p>{body}</body></html>"""
