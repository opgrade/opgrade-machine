"""Accounts, login sessions and the security journal."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..data.models import AuditEvent, AuthSession, User
from .passwords import hash_password, verify_password

Role = Literal["operator", "admin"]
ROLES = ("operator", "admin")
MIN_PASSWORD_LENGTH = {"operator": 6, "admin": 10}
USERNAME = re.compile(r"^[a-z0-9._-]{3,30}$")

MAX_FAILED_ATTEMPTS = 5
LOCK_DURATION = timedelta(minutes=5)
IDLE_TIMEOUT = timedelta(minutes=30)  # unattended touch screen: log out quickly
MAX_SESSION = timedelta(hours=12)
TOUCH_INTERVAL = timedelta(seconds=60)  # do not write last_seen on every request

BAD_CREDENTIALS = "Identifiant ou mot de passe incorrect."


class AuthError(Exception):
    """Operator-facing (French) authentication or account error."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes for timezone=True columns: they are UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_local(host: str | None) -> bool:
    """True for requests from the machine itself (the kiosk browser)."""
    try:
        return host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


# -- accounts --------------------------------------------------------------------------------


def users_exist(db: Session) -> bool:
    return db.scalar(select(func.count(User.id))) > 0


def check_password_policy(password: str, role: str) -> None:
    minimum = MIN_PASSWORD_LENGTH[role]
    if len(password) < minimum:
        raise AuthError(f"Mot de passe trop court : {minimum} caractères minimum pour ce rôle.")


def create_user(db: Session, username: str, password: str, role: Role, display_name: str | None = None) -> User:
    username = username.strip().lower()
    if not USERNAME.match(username):
        raise AuthError("Identifiant invalide : 3 à 30 caractères (lettres minuscules, chiffres, . _ -).")
    if role not in ROLES:
        raise AuthError("Rôle inconnu.")
    if db.scalar(select(User).where(User.username == username)):
        raise AuthError("Cet identifiant existe déjà.")
    check_password_policy(password, role)
    user = User(username=username, password_hash=hash_password(password), role=role, display_name=display_name)
    db.add(user)
    db.flush()
    return user


def _active_admins(db: Session) -> int:
    return db.scalar(select(func.count(User.id)).where(User.role == "admin", User.active.is_(True)))


def update_user(db: Session, user: User, role: Role | None = None, active: bool | None = None,
                display_name: str | None = None) -> None:
    losing_admin = user.role == "admin" and user.active and (role == "operator" or active is False)
    if losing_admin and _active_admins(db) <= 1:
        raise AuthError("Impossible : il doit rester au moins un administrateur actif.")
    if role is not None:
        if role not in ROLES:
            raise AuthError("Rôle inconnu.")
        user.role = role
    if active is not None:
        user.active = active
        if not active:
            revoke_sessions(db, user)
    if display_name is not None:
        user.display_name = display_name


def set_password(db: Session, user: User, password: str) -> None:
    check_password_policy(password, user.role)
    user.password_hash = hash_password(password)
    user.failed_attempts, user.locked_until = 0, None
    revoke_sessions(db, user)


_DUMMY: list[str] = []


def _dummy_hash() -> str:
    if not _DUMMY:
        _DUMMY.append(hash_password(secrets.token_hex(8)))
    return _DUMMY[0]


def authenticate(db: Session, username: str, password: str) -> User:
    """Raises AuthError; the caller must commit even on failure (failed attempt counter)."""
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None or not user.active:
        verify_password(password, _dummy_hash())  # same cost as a real check: no username probing
        raise AuthError(BAD_CREDENTIALS)
    locked_until = _aware(user.locked_until)
    if locked_until and locked_until > _now():
        raise AuthError("Compte verrouillé après plusieurs essais : réessayez dans quelques minutes.")
    if not verify_password(password, user.password_hash):
        user.failed_attempts += 1
        if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
            user.locked_until, user.failed_attempts = _now() + LOCK_DURATION, 0
        raise AuthError(BAD_CREDENTIALS)
    user.failed_attempts, user.locked_until = 0, None
    user.last_login_at = _now()
    return user


# -- sessions --------------------------------------------------------------------------------


def open_session(db: Session, user: User, ip: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(token_hash=_token_hash(token), user_id=user.id, ip=ip))
    return token


def resolve_session(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == _token_hash(token)))
    if session is None:
        return None
    now = _now()
    if (now - _aware(session.last_seen_at) > IDLE_TIMEOUT or now - _aware(session.created_at) > MAX_SESSION
            or not session.user.active):
        db.delete(session)
        db.commit()
        return None
    if now - _aware(session.last_seen_at) > TOUCH_INTERVAL:
        session.last_seen_at = now
        db.commit()
    return session.user


def close_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == _token_hash(token)))


def revoke_sessions(db: Session, user: User) -> None:
    db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))


# -- journal ---------------------------------------------------------------------------------


def audit(db: Session, username: str | None, action: str, target: str | None = None,
          details: dict | None = None, ip: str | None = None) -> None:
    db.add(AuditEvent(username=username, action=action, target=target, details=details, ip=ip))
