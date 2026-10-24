import sqlite3

from sqlalchemy import select

from opgrade.config import Settings
from opgrade.data import CardType, init_db, make_engine


def test_existing_database_gets_new_columns_and_catalogs(tmp_path):
    db_path = tmp_path / "opgrade.db"
    # Schema of v0.1.0 (before card identification).
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE card_types (id INTEGER PRIMARY KEY, uuid VARCHAR(36), code VARCHAR(50), name VARCHAR(100),"
            " category VARCHAR(50), width_mm FLOAT, height_mm FLOAT, thickness_mm FLOAT, has_border BOOLEAN,"
            " active BOOLEAN)"
        )
        conn.execute(
            "INSERT INTO card_types VALUES (1, 'u1', 'pokemon', 'Pokémon', 'tcg', 63, 88, NULL, 1, 1)"
        )

    factory = init_db(make_engine(Settings(data_dir=tmp_path)))
    with factory() as db:
        pokemon = db.scalar(select(CardType).where(CardType.code == "pokemon"))
        assert pokemon.catalog == "tcgdex"
