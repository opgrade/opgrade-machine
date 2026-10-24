"""FastAPI application: operator API, simulation API and the touch UI."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import httpx
import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import __version__
from ..config import PACKAGE_DIR, Settings
from ..customers import OrderError, PrivacyError, load_policy, refresh_status
from ..customers.orders import check_card_can_join
from ..data import Capture, Card, CardType, Client, GradingSession, Order, init_db, make_engine
from ..grading import load_rules
from ..hardware import Hardware, Side, build_hardware
from ..hardware.sim import SimCardSpec, SimulatedCamera, random_sim_defects
from ..identify import CardCatalog, CardQuery, CatalogError, build_catalog
from ..identify.base import USER_AGENT
from ..identify.ocr import NumberReader
from ..identify.recognize import CardRecognizer
from ..identify.visual import VisualIndex
from ..orchestration import AcquisitionError, GradingService, InvalidTransition, WorkflowError
from ..orchestration.service import surface_image_path
from ..auth import AuthError, audit
from ..data import User
from . import auth as auth_api
from ..calibration.store import load_active
from . import calibration as calibration_api
from . import customers, reports, schemas
from .auth import client_ip, current_user, require_admin


def create_app(
    settings: Settings | None = None,
    hardware: Hardware | None = None,
    catalogs: dict[str, CardCatalog] | None = None,
    visual_index: VisualIndex | None = None,
    number_reader: NumberReader | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    hardware = hardware or build_hardware(settings.simulation, settings.sim_optics)
    catalogs = dict(catalogs or {})
    visual_index = visual_index or VisualIndex(settings.data_dir / "card_index.db")

    def get_catalog(catalog_id: str) -> CardCatalog:
        if catalog_id not in catalogs:
            catalogs[catalog_id] = build_catalog(catalog_id)
        return catalogs[catalog_id]

    recognizer = CardRecognizer(visual_index, number_reader or NumberReader(), get_catalog)
    session_factory = init_db(make_engine(settings))
    service = GradingService(settings, hardware, load_rules(settings.rules_path))
    with session_factory() as db:
        service.set_calibration(load_active(db))  # lens correction + white balance from the start
    policy = load_policy(settings.privacy_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        hardware.close()

    def get_db() -> Iterator[Session]:
        with session_factory() as db:
            yield db

    # Every /api route requires a login unless listed in auth_api.PUBLIC_PATHS.
    app = FastAPI(
        title="OpGrade X", version=__version__, lifespan=lifespan,
        dependencies=[Depends(auth_api.make_gate(get_db))],
    )
    app.state.settings = settings
    app.state.hardware = hardware
    app.state.service = service
    app.state.session_factory = session_factory
    app.include_router(auth_api.build_router(get_db, settings))
    app.include_router(reports.build_router(get_db, settings))
    app.include_router(calibration_api.build_router(get_db, settings, service))

    def get_session(db: Session, session_id: int) -> GradingSession:
        gs = db.get(GradingSession, session_id)
        if gs is None:
            raise HTTPException(404, "Session introuvable.")
        return gs

    @app.exception_handler(WorkflowError)
    @app.exception_handler(InvalidTransition)
    @app.exception_handler(OrderError)
    @app.exception_handler(PrivacyError)
    async def _conflict(_: Request, exc: Exception):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(AuthError)
    async def _auth_error(_: Request, exc: AuthError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # -- status ---------------------------------------------------------------------------

    @app.get("/api/status")
    def status() -> dict:
        return {
            "version": __version__,
            "machine_id": settings.machine_id,
            "simulation": hardware.simulated,
            "rules_version": service.rules.version,
            "privacy_version": policy.version,
            "privacy_configured": policy.is_configured,
        }

    # -- clients / card types / cards -----------------------------------------------------

    app.include_router(customers.build_router(get_db, policy, require_admin))

    @app.get("/api/card-types", response_model=list[schemas.CardTypeOut])
    def list_card_types(db: Session = Depends(get_db)):
        return db.scalars(select(CardType).where(CardType.active).order_by(CardType.id)).all()

    @app.post("/api/card-types", response_model=schemas.CardTypeOut, status_code=201)
    def create_card_type(body: schemas.CardTypeIn, request: Request, db: Session = Depends(get_db),
                         admin: User = Depends(require_admin)):
        if db.scalar(select(CardType).where(CardType.code == body.code)):
            raise HTTPException(409, "Ce code de type de carte existe déjà.")
        ct = CardType(**body.model_dump())
        db.add(ct)
        db.flush()
        audit(db, admin.username, "card_type_created", target=f"card_type:{ct.id}",
              details=body.model_dump(), ip=client_ip(request))
        db.commit()
        return ct

    @app.get("/api/identify", response_model=schemas.IdentifyOut)
    def identify(
        card_type_id: int,
        name: str = Query(min_length=2, max_length=100),
        number: str | None = Query(default=None, max_length=30),
        set_name: str | None = Query(default=None, max_length=100),
        db: Session = Depends(get_db),
    ):
        card_type = db.get(CardType, card_type_id)
        if card_type is None:
            raise HTTPException(404, "Type de carte introuvable.")
        if not card_type.catalog:
            return {
                "catalog": None,
                "available": False,
                "message": "Pas de catalogue en ligne pour ce type de carte : saisie manuelle.",
                "candidates": [],
            }
        try:
            catalog = get_catalog(card_type.catalog)
            found = catalog.search(CardQuery(name=name, number=number or None, set_name=set_name or None))
        except CatalogError as exc:
            return {"catalog": card_type.catalog, "available": False, "message": f"{exc} Saisie manuelle.", "candidates": []}
        message = None if found else "Aucune carte trouvée. Vérifiez l'orthographe ou saisissez manuellement."
        return {"catalog": card_type.catalog, "available": True, "message": message, "candidates": [c.as_dict() for c in found]}

    @app.post("/api/identify/image", response_model=schemas.IdentifyOut)
    def identify_image(body: schemas.IdentifyImageIn, db: Session = Depends(get_db)):
        card_type = db.get(CardType, body.card_type_id)
        if card_type is None:
            raise HTTPException(404, "Type de carte introuvable.")
        catalog = card_type.catalog
        if not catalog or visual_index.count(catalog) == 0:
            hint = f" Construisez-le : python -m opgrade.identify.build_index {catalog}" if catalog else ""
            return {
                "catalog": catalog,
                "available": False,
                "message": "Pas d'index visuel pour ce type de carte : recherche par nom ou saisie manuelle." + hint,
                "candidates": [],
            }
        try:
            detection = service.acquire(card_type).detection
        except AcquisitionError as exc:
            return {"catalog": catalog, "available": True, "message": f"Capture refusée : {exc}", "candidates": []}

        result = recognizer.recognize(detection.warped, catalog)
        return {
            "catalog": catalog,
            "available": True,
            "message": result.message,
            "candidates": result.candidates,
            "ocr_text": result.ocr_text,
        }

    @app.post("/api/cards", response_model=schemas.CardOut, status_code=201)
    def create_card(body: schemas.CardIn, db: Session = Depends(get_db)):
        client = db.get(Client, body.client_id)
        if client is None:
            raise HTTPException(404, "Client introuvable.")
        if client.anonymized_at:
            raise HTTPException(409, "Ce client a été anonymisé.")
        if db.get(CardType, body.card_type_id) is None:
            raise HTTPException(404, "Type de carte introuvable.")
        order = None
        if body.order_id is not None:
            order = db.get(Order, body.order_id)
            if order is None:
                raise HTTPException(404, "Commande introuvable.")
            check_card_can_join(order, client.id)
        card = Card(**body.model_dump())
        db.add(card)
        db.flush()
        refresh_status(order)  # a graded order receiving a new card is in progress again
        db.commit()
        return card

    # -- grading sessions -----------------------------------------------------------------

    @app.get("/api/sessions", response_model=list[schemas.SessionOut])
    def list_sessions(limit: int = 50, db: Session = Depends(get_db)):
        stmt = select(GradingSession).order_by(GradingSession.id.desc()).limit(min(limit, 500))
        return db.scalars(stmt).all()

    @app.post("/api/sessions", response_model=schemas.SessionOut, status_code=201)
    def start_session(body: schemas.SessionIn, request: Request, db: Session = Depends(get_db)):
        # The operator is the logged-in account, never a free-text value from the client.
        return service.start_session(db, body.card_id, current_user(request).username)

    @app.get("/api/sessions/{session_id}", response_model=schemas.SessionOut)
    def get_session_route(session_id: int, db: Session = Depends(get_db)):
        return get_session(db, session_id)

    @app.post("/api/sessions/{session_id}/capture", response_model=schemas.CaptureResult)
    def capture(session_id: int, db: Session = Depends(get_db)):
        get_session(db, session_id)
        outcome = service.capture(db, session_id)
        gs = get_session(db, session_id)
        return {"accepted": outcome.accepted, "message": outcome.message, "session": gs}

    @app.post("/api/sessions/{session_id}/analyze", response_model=schemas.SessionOut)
    def analyze(session_id: int, db: Session = Depends(get_db)):
        get_session(db, session_id)
        return service.analyze(db, session_id)

    @app.post("/api/sessions/{session_id}/review", response_model=schemas.SessionOut)
    def review(session_id: int, body: schemas.ReviewIn, request: Request, db: Session = Depends(get_db)):
        get_session(db, session_id)
        grades = {"corners": body.corners, "edges": body.edges, "surface": body.surface}
        return service.submit_review(db, session_id, grades, body.centering_override, current_user(request).username)

    @app.post("/api/sessions/{session_id}/cancel", response_model=schemas.SessionOut)
    def cancel(session_id: int, db: Session = Depends(get_db)):
        get_session(db, session_id)
        return service.cancel(db, session_id)

    @app.get("/api/captures/{capture_id}/image")
    def capture_image(capture_id: int, kind: str = "warped", db: Session = Depends(get_db)):
        cap = db.get(Capture, capture_id)
        if cap is None:
            raise HTTPException(404, "Capture introuvable.")
        rel = cap.raw_path if kind == "raw" else cap.warped_path
        path = (settings.data_dir / rel).resolve()
        if not path.is_relative_to(settings.data_dir.resolve()) or not path.exists():
            raise HTTPException(404, "Image introuvable.")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/sessions/{session_id}/surface/{side}.png")
    def surface_image(session_id: int, side: Side, db: Session = Depends(get_db)):
        """Diffuse photo with the raking-light relief map and numbered defects."""
        gs = get_session(db, session_id)
        path = settings.data_dir / surface_image_path(gs, side)
        if not path.exists():
            raise HTTPException(404, "Pas d'analyse de surface pour cette face.")
        return FileResponse(path, media_type="image/png")

    # -- simulation -----------------------------------------------------------------------

    if hardware.simulated and isinstance(hardware.camera, SimulatedCamera):
        table = hardware.camera.table

        @app.get("/api/sim/table")
        def sim_table() -> dict:
            return {
                "card_present": table.card is not None,
                "side_up": table.side_up.value,
                "label": table.card.label if table.card else None,
            }

        @app.post("/api/sim/place-reference")
        def sim_place_reference(body: schemas.SimReferenceIn) -> dict:
            """Put a random indexed catalog card on the tray (needs Internet for its image)."""
            ref = visual_index.random_reference(body.catalog)
            if ref is None:
                raise HTTPException(409, f"Index visuel vide pour {body.catalog}.")
            try:
                response = httpx.get(ref.image_url, timeout=20, headers={"User-Agent": USER_AGENT})
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise HTTPException(502, f"Image de référence inaccessible : {exc}") from exc
            image = cv2.imdecode(np.frombuffer(response.content, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise HTTPException(502, "Image de référence illisible.")
            label = " · ".join(str(v) for v in (ref.name, ref.set_name, ref.number) if v)
            table.place(SimCardSpec(front_image=image, label=label, rotation_deg=float(np.random.uniform(-4, 4))))
            return sim_table()

        @app.post("/api/sim/place")
        def sim_place(body: schemas.SimCardIn | None = None) -> dict:
            fields = body.model_dump() if body else {}
            n_defects = fields.pop("random_defects", 0)
            spec = SimCardSpec(**fields, defects=random_sim_defects(n_defects))
            table.place(spec, Side.FRONT)
            return sim_table()

        @app.post("/api/sim/flip")
        def sim_flip() -> dict:
            table.flip()
            return sim_table()

        @app.post("/api/sim/remove")
        def sim_remove() -> dict:
            table.remove()
            return sim_table()

    # -- web UI ---------------------------------------------------------------------------

    web_dir = Path(PACKAGE_DIR) / "web"
    app.mount("/ui", StaticFiles(directory=web_dir, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/ui/")

    return app
