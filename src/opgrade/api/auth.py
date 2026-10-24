"""Login, accounts and access control.

Every /api route requires a logged-in user (global dependency `auth_gate`) except PUBLIC_PATHS.
Admin-only routes add `Depends(require_admin)`.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth
from ..config import Settings
from ..data import AuditEvent, User
from . import schemas

COOKIE = "opgrade_session"
PUBLIC_PATHS = {"/api/status", "/api/auth/state", "/api/auth/login", "/api/auth/setup"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def current_user(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Connexion requise.")
    return user


def require_admin(request: Request) -> User:
    user = current_user(request)
    if user.role != "admin":
        raise HTTPException(403, "Réservé à l'administrateur.")
    return user


def user_out(user: User) -> dict:
    return schemas.UserOut.model_validate(user).model_dump()


def make_gate(get_db: Callable) -> Callable:
    def auth_gate(request: Request, db: Session = Depends(get_db)) -> None:
        path = request.url.path
        if not path.startswith("/api/"):
            return
        # Cross-site request forgery: refuse state changes coming from another origin.
        origin = request.headers.get("origin")
        if request.method not in SAFE_METHODS and origin and urlparse(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "Origine de la requête refusée.")
        request.state.user = auth.resolve_session(db, request.cookies.get(COOKIE))
        if request.state.user is not None:
            db.expunge(request.state.user)  # usable after this request's DB session closes
        if path not in PUBLIC_PATHS and request.state.user is None:
            raise HTTPException(401, "Connexion requise.")

    return auth_gate


def build_router(get_db: Callable, settings: Settings) -> APIRouter:
    router = APIRouter()

    def set_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            COOKIE, token, httponly=True, samesite="strict", secure=settings.cookie_secure,
            max_age=int(auth.service.MAX_SESSION.total_seconds()), path="/",
        )

    def login_response(db: Session, user: User, request: Request, response: Response) -> dict:
        token = auth.open_session(db, user, client_ip(request))
        auth.audit(db, user.username, "login", ip=client_ip(request))
        db.commit()
        set_cookie(response, token)
        return user_out(user)

    def get_user(db: Session, user_id: int) -> User:
        user = db.get(User, user_id)
        if user is None:
            raise HTTPException(404, "Compte introuvable.")
        return user

    # -- session --------------------------------------------------------------------------

    @router.get("/api/auth/state")
    def state(request: Request, db: Session = Depends(get_db)) -> dict:
        user = getattr(request.state, "user", None)
        return {"setup_required": not auth.users_exist(db), "user": user_out(user) if user else None}

    @router.post("/api/auth/setup", response_model=schemas.UserOut)
    def setup(body: schemas.SetupIn, request: Request, response: Response, db: Session = Depends(get_db)):
        """First administrator. Only while no account exists, and only from the machine itself."""
        if auth.users_exist(db):
            raise HTTPException(409, "La configuration initiale est déjà faite.")
        if not auth.is_local(client_ip(request)):
            raise HTTPException(403, "Le premier administrateur se crée sur la machine elle-même "
                                     "(écran tactile) ou avec : python -m opgrade.auth create-admin")
        user = auth.create_user(db, body.username, body.password, "admin", body.display_name)
        auth.audit(db, user.username, "setup", target=f"user:{user.id}", ip=client_ip(request))
        return login_response(db, user, request, response)

    @router.post("/api/auth/login", response_model=schemas.UserOut)
    def login(body: schemas.LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
        try:
            user = auth.authenticate(db, body.username, body.password)
        except auth.AuthError as exc:
            auth.audit(db, body.username.strip().lower()[:30], "login_failed", ip=client_ip(request))
            db.commit()  # keep the failed-attempt counter
            raise HTTPException(401, str(exc)) from exc
        return login_response(db, user, request, response)

    @router.post("/api/auth/logout")
    def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
        auth.close_session(db, request.cookies.get(COOKIE))
        auth.audit(db, current_user(request).username, "logout", ip=client_ip(request))
        db.commit()
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @router.get("/api/auth/me", response_model=schemas.UserOut)
    def me(request: Request):
        return user_out(current_user(request))

    @router.post("/api/auth/password")
    def change_own_password(body: schemas.PasswordChangeIn, request: Request, response: Response,
                            db: Session = Depends(get_db)) -> dict:
        user = get_user(db, current_user(request).id)
        if not auth.verify_password(body.current_password, user.password_hash):
            raise HTTPException(401, "Mot de passe actuel incorrect.")
        auth.set_password(db, user, body.new_password)  # also closes all sessions
        auth.audit(db, user.username, "password_changed", target=f"user:{user.id}", ip=client_ip(request))
        token = auth.open_session(db, user, client_ip(request))
        db.commit()
        set_cookie(response, token)
        return {"ok": True}

    # -- administration -------------------------------------------------------------------

    admin = [Depends(require_admin)]

    @router.get("/api/users", response_model=list[schemas.UserOut], dependencies=admin)
    def list_users(db: Session = Depends(get_db)):
        return [user_out(u) for u in db.scalars(select(User).order_by(User.username))]

    @router.post("/api/users", response_model=schemas.UserOut, status_code=201, dependencies=admin)
    def create_user(body: schemas.UserCreateIn, request: Request, db: Session = Depends(get_db)):
        user = auth.create_user(db, body.username, body.password, body.role, body.display_name)
        auth.audit(db, current_user(request).username, "user_created", target=f"user:{user.id}",
                   details={"username": user.username, "role": user.role}, ip=client_ip(request))
        db.commit()
        return user_out(user)

    @router.patch("/api/users/{user_id}", response_model=schemas.UserOut, dependencies=admin)
    def update_user(user_id: int, body: schemas.UserUpdateIn, request: Request, db: Session = Depends(get_db)):
        user = get_user(db, user_id)
        changes = body.model_dump(exclude_unset=True)
        auth.update_user(db, user, **changes)
        auth.audit(db, current_user(request).username, "user_updated", target=f"user:{user.id}",
                   details=changes, ip=client_ip(request))
        db.commit()
        return user_out(user)

    @router.post("/api/users/{user_id}/password", dependencies=admin)
    def reset_password(user_id: int, body: schemas.PasswordResetIn, request: Request,
                       db: Session = Depends(get_db)) -> dict:
        user = get_user(db, user_id)
        auth.set_password(db, user, body.new_password)
        auth.audit(db, current_user(request).username, "password_reset", target=f"user:{user.id}",
                   ip=client_ip(request))
        db.commit()
        return {"ok": True}

    @router.get("/api/audit", response_model=list[schemas.AuditOut], dependencies=admin)
    def audit_log(limit: int = 200, db: Session = Depends(get_db)):
        stmt = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(min(limit, 1000))
        return db.scalars(stmt).all()

    return router
