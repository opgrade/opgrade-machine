"""Visual card fingerprints and the local reference index.

Each reference card image (catalog scan) is reduced to two 64-bit perceptual hashes: the whole
card and its artwork area. A captured, rectified card is hashed the same way and matched by
Hamming distance. Hashes are stored locally so recognition works without Internet.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .base import CardCandidate

HASH_INPUT = 32
HASH_BITS = 8  # 8x8 low-frequency DCT block -> 64 bits
CANONICAL_SIZE = (256, 356)  # (w, h) every card image is resized to before hashing
# Artwork window, relative to the card. Generous on purpose: layouts differ between games.
ART_BOX = (0.08, 0.10, 0.92, 0.55)
MAX_DISTANCE = 128  # two 64-bit hashes

# Distance thresholds on (full + art), from scripts/eval_visual.py on simulated captures
# (2026-10-24): right card median 0–2, 95th pct ≤ 6; closest wrong card 5th pct 28.
# MUST be recalibrated on real photos from the optical bench (expect larger distances).
CONFIDENT_DISTANCE = 16
POSSIBLE_DISTANCE = 22
# Against the full TCGdex index the closest wrong card drops to ~8 (5th pct): same artwork
# reprinted in other sets. Within this margin of the best, the printing must be checked.
REPRINT_MARGIN = 8

_POPCOUNT8 = np.array([bin(i).count("1") for i in range(256)], np.uint8)


def phash(gray: np.ndarray) -> int:
    small = cv2.resize(gray, (HASH_INPUT, HASH_INPUT), interpolation=cv2.INTER_AREA).astype(np.float32)
    block = cv2.dct(small)[:HASH_BITS, :HASH_BITS].flatten()
    median = np.median(block[1:])  # exclude DC (overall brightness)
    bits = block > median
    return int(np.packbits(bits).view(">u8")[0])


@dataclass(frozen=True)
class Fingerprint:
    full: int
    art: int


def fingerprint(card_bgr: np.ndarray) -> Fingerprint:
    """card_bgr: the whole card, upright (rectified capture or catalog scan)."""
    card = cv2.resize(card_bgr, CANONICAL_SIZE, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(card, cv2.COLOR_BGR2GRAY)
    w, h = CANONICAL_SIZE
    x0, y0, x1, y1 = ART_BOX
    art = gray[int(y0 * h) : int(y1 * h), int(x0 * w) : int(x1 * w)]
    return Fingerprint(full=phash(gray), art=phash(art))


def popcount64(values: np.ndarray) -> np.ndarray:
    """Bit count of each uint64 (works on numpy 1.x, which Raspberry Pi OS ships)."""
    return _POPCOUNT8[values.view(np.uint8)].reshape(-1, 8).sum(axis=1, dtype=np.int32)


def _to_sql(h: int) -> int:  # SQLite INTEGER is signed 64-bit
    return h - (1 << 64) if h >= 1 << 63 else h


def confidence_label(distance: int) -> str:
    if distance <= CONFIDENT_DISTANCE:
        return "high"
    if distance <= POSSIBLE_DISTANCE:
        return "medium"
    return "low"


@dataclass
class VisualMatch:
    candidate: CardCandidate
    distance: int

    @property
    def confidence(self) -> str:
        return confidence_label(self.distance)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS refs (
    catalog TEXT NOT NULL, external_id TEXT NOT NULL,
    name TEXT NOT NULL, set_name TEXT, number TEXT, year INTEGER, rarity TEXT, language TEXT,
    image_url TEXT, h_full INTEGER NOT NULL, h_art INTEGER NOT NULL, printed_number TEXT,
    PRIMARY KEY (catalog, external_id)
);
CREATE TABLE IF NOT EXISTS failures (
    catalog TEXT NOT NULL, external_id TEXT NOT NULL, image_url TEXT, error TEXT,
    PRIMARY KEY (catalog, external_id)
);
"""
# Same order as the CardCandidate fields after `source`.
_FIELDS = ("external_id", "name", "set_name", "number", "year", "rarity", "language", "image_url", "printed_number")


class VisualIndex:
    """Reference fingerprints in their own SQLite file (large, rebuildable, not business data)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[int, np.ndarray, np.ndarray, list[tuple]]] = {}
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(refs)")}
            if "printed_number" not in columns:  # index built before OCR support
                conn.execute("ALTER TABLE refs ADD COLUMN printed_number TEXT")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    # -- writing (index builder) --------------------------------------------------------

    def known_ids(self, catalog: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT external_id FROM refs WHERE catalog = ? UNION SELECT external_id FROM failures WHERE catalog = ?",
                (catalog, catalog),
            )
            return {r[0] for r in rows}

    def add_many(self, items: list[tuple[CardCandidate, Fingerprint]]) -> None:
        with self._lock, self._connect() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO refs (catalog, {', '.join(_FIELDS)}, h_full, h_art)"
                f" VALUES ({', '.join('?' * (len(_FIELDS) + 3))})",
                [
                    (c.source, *(getattr(c, f) for f in _FIELDS), _to_sql(fp.full), _to_sql(fp.art))
                    for c, fp in items
                ],
            )

    def update_metadata(self, cards: list[CardCandidate]) -> int:
        """Refresh names/sets/numbers of already indexed cards (no image download)."""
        fields = _FIELDS[1:]
        with self._lock, self._connect() as conn:
            cur = conn.executemany(
                f"UPDATE refs SET {', '.join(f'{f} = ?' for f in fields)} WHERE catalog = ? AND external_id = ?",
                [(*(getattr(c, f) for f in fields), c.source, c.external_id) for c in cards],
            )
            return cur.rowcount

    def remove_prefixes(self, catalog: str, prefixes: list[str]) -> int:
        """Delete entries whose external_id starts with one of the prefixes."""
        removed = 0
        with self._lock, self._connect() as conn:
            for prefix in prefixes:
                escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                removed += conn.execute(
                    "DELETE FROM refs WHERE catalog = ? AND external_id LIKE ? ESCAPE '\\'", (catalog, escaped + "%")
                ).rowcount
        return removed

    def add_failure(self, catalog: str, external_id: str, image_url: str | None, error: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO failures VALUES (?, ?, ?, ?)", (catalog, external_id, image_url, error[:300])
            )

    # -- reading ------------------------------------------------------------------------

    def count(self, catalog: str) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM refs WHERE catalog = ?", (catalog,)).fetchone()[0]

    def random_reference(self, catalog: str) -> CardCandidate | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_FIELDS)} FROM refs WHERE catalog = ? AND image_url IS NOT NULL"
                " ORDER BY RANDOM() LIMIT 1",
                (catalog,),
            ).fetchone()
        return CardCandidate(catalog, *row) if row else None

    def _load(self, catalog: str) -> tuple[np.ndarray, np.ndarray, list[tuple]]:
        n = self.count(catalog)
        cached = self._cache.get(catalog)
        if cached and cached[0] == n:
            return cached[1], cached[2], cached[3]
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_FIELDS)}, h_full, h_art FROM refs WHERE catalog = ?", (catalog,)
            ).fetchall()
        full = np.array([r[-2] for r in rows], np.int64).view(np.uint64)
        art = np.array([r[-1] for r in rows], np.int64).view(np.uint64)
        meta = [r[:-2] for r in rows]
        self._cache[catalog] = (n, full, art, meta)
        return full, art, meta

    def search(self, catalog: str, fp: Fingerprint, limit: int = 10) -> list[VisualMatch]:
        full, art, meta = self._load(catalog)
        if not meta:
            return []
        q_full = np.array([_to_sql(fp.full)], np.int64).view(np.uint64)[0]
        q_art = np.array([_to_sql(fp.art)], np.int64).view(np.uint64)[0]
        distance = popcount64(full ^ q_full) + popcount64(art ^ q_art)
        best = np.argsort(distance, kind="stable")[:limit]
        return [VisualMatch(CardCandidate(catalog, *meta[i]), int(distance[i])) for i in best]
