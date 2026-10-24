"""Account administration from the machine's terminal.

    python -m opgrade.auth create-admin --username alice     # first admin, or recovery
    python -m opgrade.auth reset-password --username alice   # forgotten password
    python -m opgrade.auth list

Passwords are typed interactively (never on the command line, which ends up in shell history).
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from ..config import Settings
from ..data import User, init_db, make_engine
from .service import AuthError, audit, create_user, set_password


def _ask_password() -> str:
    first = getpass.getpass("Mot de passe : ")
    if first != getpass.getpass("Confirmation : "):
        raise AuthError("Les deux mots de passe ne correspondent pas.")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m opgrade.auth", description="Comptes OpGrade X")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-admin", help="créer un compte administrateur")
    create.add_argument("--username", required=True)
    create.add_argument("--display-name")
    reset = sub.add_parser("reset-password", help="réinitialiser un mot de passe")
    reset.add_argument("--username", required=True)
    sub.add_parser("list", help="lister les comptes")
    args = parser.parse_args(argv)

    factory = init_db(make_engine(Settings.from_env()))
    try:
        with factory() as db:
            if args.command == "list":
                for u in db.scalars(select(User).order_by(User.username)):
                    print(f"{u.username:20} {u.role:9} {'actif' if u.active else 'désactivé'}")
                return 0
            if args.command == "create-admin":
                user = create_user(db, args.username, _ask_password(), "admin", args.display_name)
                audit(db, "terminal", "user_created", target=f"user:{user.id}", details={"role": "admin"})
                print(f"Administrateur « {user.username} » créé.")
            else:
                user = db.scalar(select(User).where(User.username == args.username.strip().lower()))
                if user is None:
                    raise AuthError("Compte introuvable.")
                set_password(db, user, _ask_password())
                audit(db, "terminal", "password_reset", target=f"user:{user.id}")
                print(f"Mot de passe de « {user.username} » réinitialisé ; ses sessions sont fermées.")
            db.commit()
    except AuthError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
