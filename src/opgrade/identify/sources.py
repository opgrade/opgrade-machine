"""Full card lists with reference images, used to build the visual index.

Shapes checked against the live services on 2026-10-24:
- TCGdex: GET /v2/{lang}/cards lists every card (~23 700) with an image base URL.
- YGOPRODeck: cardinfo.php without filter returns every card (~14 600), one image per artwork.
- Scryfall: bulk file "unique_artwork" (gzip JSON lines, ~40 MB), one entry per artwork.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import httpx

from .base import CardCandidate, year_from

Reference = tuple[CardCandidate, str]  # (card, image URL to fingerprint)


def tcgdex_references(client: httpx.Client, lang: str = "en") -> Iterator[Reference]:
    base = "https://api.tcgdex.net/v2"
    sets = {s["id"]: s["name"] for s in client.get(f"{base}/{lang}/sets").raise_for_status().json()}

    def set_year(set_id: str) -> int | None:
        try:
            return year_from(client.get(f"{base}/{lang}/sets/{set_id}").raise_for_status().json().get("releaseDate"))
        except httpx.HTTPError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        years = dict(zip(sets, pool.map(set_year, sets)))

    for card in client.get(f"{base}/{lang}/cards").raise_for_status().json():
        if not card.get("image"):
            continue
        local_id = card.get("localId", "")
        set_id = card["id"].removesuffix(f"-{local_id}")
        yield (
            CardCandidate(
                source="tcgdex",
                external_id=f"{lang}:{card['id']}",
                name=card["name"],
                set_name=sets.get(set_id),
                number=local_id,
                year=years.get(set_id),
                language=lang,
                image_url=f"{card['image']}/low.webp",
            ),
            f"{card['image']}/low.webp",
        )


def ygoprodeck_references(client: httpx.Client) -> Iterator[Reference]:
    """One entry per artwork; the printing (set code) is chosen afterwards by text search."""
    data = client.get("https://db.ygoprodeck.com/api/v7/cardinfo.php", timeout=120).raise_for_status().json()
    for card in data["data"]:
        for image in card.get("card_images") or []:
            url = image.get("image_url_small")
            if url:
                yield (
                    CardCandidate(
                        source="ygoprodeck",
                        external_id=f"art:{image['id']}",
                        name=card["name"],
                        language="en",
                        image_url=url,
                    ),
                    url,
                )


def scryfall_references(client: httpx.Client) -> Iterator[Reference]:
    meta = client.get("https://api.scryfall.com/bulk-data/unique_artwork").raise_for_status().json()
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)  # the file is .jsonl.gz
    buffer = b""
    with client.stream("GET", meta["jsonl_download_uri"], timeout=300) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            buffer += decompressor.decompress(chunk)
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                if line.strip():
                    ref = _scryfall_reference(json.loads(line))
                    if ref:
                        yield ref
    if buffer.strip():
        ref = _scryfall_reference(json.loads(buffer))
        if ref:
            yield ref


def _scryfall_reference(card: dict) -> Reference | None:
    images = card.get("image_uris") or ((card.get("card_faces") or [{}])[0].get("image_uris") or {})
    url = images.get("normal") or images.get("small")
    if not url:
        return None
    return (
        CardCandidate(
            source="scryfall",
            external_id=card["id"],
            name=card["name"],
            set_name=card.get("set_name"),
            number=card.get("collector_number"),
            year=year_from(card.get("released_at")),
            rarity=card.get("rarity"),
            language=card.get("lang"),
            image_url=images.get("small") or url,
        ),
        url,
    )


SOURCES = {
    "tcgdex": tcgdex_references,
    "ygoprodeck": ygoprodeck_references,
    "scryfall": scryfall_references,
}
