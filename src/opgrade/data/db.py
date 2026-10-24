"""Database engine, session factory and initial data."""

from __future__ import annotations

from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from .models import Base, CardType

# Initial analysis profiles. Admins can add more through the API without code changes.
DEFAULT_CARD_TYPES = [
    {"code": "pokemon", "name": "Pokémon", "category": "tcg", "width_mm": 63.0, "height_mm": 88.0, "catalog": "tcgdex"},
    {"code": "magic", "name": "Magic: The Gathering", "category": "tcg", "width_mm": 63.0, "height_mm": 88.0, "catalog": "scryfall"},
    {"code": "yugioh", "name": "Yu-Gi-Oh!", "category": "tcg", "width_mm": 59.0, "height_mm": 86.0, "catalog": "ygoprodeck"},
    {"code": "sport_hockey", "name": "Sport — Hockey", "category": "sport", "width_mm": 63.5, "height_mm": 88.9},
    {"code": "sport_baseball", "name": "Sport — Baseball", "category": "sport", "width_mm": 63.5, "height_mm": 88.9},
    {"code": "sport_basketball", "name": "Sport — Basketball", "category": "sport", "width_mm": 63.5, "height_mm": 88.9},
    {"code": "sport_football", "name": "Sport — Football", "category": "sport", "width_mm": 63.5, "height_mm": 88.9},
    {"code": "other_standard", "name": "Autre (format standard)", "category": "other", "width_mm": 63.0, "height_mm": 88.0},
]


def make_engine(settings: Settings) -> Engine:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")  # survives power loss better
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def _add_missing_columns(engine: Engine) -> None:
    """Minimal forward migration: add new nullable columns to existing tables.

    Enough while the schema only grows; switch to Alembic before the first real deployment.
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in existing and column.nullable:
                    col_type = column.type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))


def init_db(engine: Engine) -> sessionmaker[Session]:
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        existing = {ct.code: ct for ct in db.scalars(select(CardType))}
        for ct in DEFAULT_CARD_TYPES:
            if ct["code"] not in existing:
                db.add(CardType(**ct))
            elif existing[ct["code"]].catalog is None and ct.get("catalog"):
                existing[ct["code"]].catalog = ct["catalog"]
        db.commit()
    return factory
