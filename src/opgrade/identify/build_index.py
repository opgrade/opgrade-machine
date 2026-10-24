"""Build or update the local visual index.

    python -m opgrade.identify.build_index tcgdex [--lang en] [--limit N] [--workers 6]

Downloads each reference image once, keeps only its fingerprint (images are not stored).
Resumable: cards already indexed (or that failed) are skipped. Run it again to pick up new sets.
Approximate first run: TCGdex ~22 000 images (~450 MB), YGOPRODeck ~15 000, Scryfall ~50 000.
"""

from __future__ import annotations

import argparse
import itertools
import sys
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

import cv2
import httpx
import numpy as np

from ..config import Settings
from .base import USER_AGENT
from .sources import SOURCES, Reference, tcgdex_excluded_prefixes
from .visual import Fingerprint, VisualIndex, fingerprint

BATCH = 200


def download_fingerprint(client: httpx.Client, url: str) -> Fingerprint:
    response = client.get(url)
    response.raise_for_status()
    image = cv2.imdecode(np.frombuffer(response.content, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("undecodable image")
    return fingerprint(image)


def build(
    index: VisualIndex,
    references: Iterable[Reference],
    client: httpx.Client,
    workers: int = 6,
    limit: int | None = None,
    log=print,
) -> tuple[int, int]:
    """Returns (added, failed)."""
    catalog_known: dict[str, set[str]] = {}

    def is_new(ref: Reference) -> bool:
        catalog = ref[0].source
        if catalog not in catalog_known:
            catalog_known[catalog] = index.known_ids(catalog)
        return ref[0].external_id not in catalog_known[catalog]

    todo = itertools.islice(filter(is_new, references), limit)
    added = failed = 0
    started = time.monotonic()

    def work(ref: Reference):
        card, url = ref
        try:
            return card, download_fingerprint(client, url), None
        except Exception as exc:  # network, HTTP or decoding error: record and move on
            return card, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while batch := list(itertools.islice(todo, BATCH)):
            results = list(pool.map(work, batch))
            ok = [(card, fp) for card, fp, err in results if fp is not None]
            index.add_many(ok)
            for card, _, err in results:
                if err:
                    index.add_failure(card.source, card.external_id, card.image_url, err)
            added += len(ok)
            failed += len(results) - len(ok)
            log(f"  {added} indexées, {failed} échecs ({time.monotonic() - started:.0f} s)")
    return added, failed


def _batched(items: Iterable, size: int = 1000):
    it = iter(items)
    while batch := list(itertools.islice(it, size)):
        yield batch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Construit l'index visuel des cartes de référence.")
    parser.add_argument("catalog", choices=sorted(SOURCES))
    parser.add_argument("--lang", default="en", help="langue TCGdex (en, fr…)")
    parser.add_argument("--limit", type=int, default=None, help="nombre max de nouvelles cartes (essai)")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--refresh-metadata", action="store_true",
        help="met à jour noms/séries/numéros des cartes déjà indexées, sans retélécharger les images",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    index = VisualIndex(settings.data_dir / "card_index.db")
    client = httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    source = SOURCES[args.catalog]
    references = source(client, args.lang) if args.catalog == "tcgdex" else source(client)

    print(f"Index : {index.path} — catalogue {args.catalog} ({index.count(args.catalog)} déjà indexées)")
    if args.catalog == "tcgdex":
        removed = index.remove_prefixes("tcgdex", tcgdex_excluded_prefixes(client, args.lang))
        if removed:
            print(f"Cartes numériques (Pokémon TCG Pocket) retirées : {removed}")
    if args.refresh_metadata:
        updated = sum(index.update_metadata([card for card, _ in batch]) for batch in _batched(references))
        print(f"Métadonnées mises à jour : {updated} cartes.")
        return 0
    added, failed = build(index, references, client, workers=args.workers, limit=args.limit)
    print(f"Terminé : {added} ajoutées, {failed} échecs, {index.count(args.catalog)} au total.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
