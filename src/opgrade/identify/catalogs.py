"""Catalog adapters. Response shapes checked against the live APIs on 2026-10-24."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from .base import (
    MAX_RESULTS,
    CardCandidate,
    CardCatalog,
    CardQuery,
    CatalogError,
    ascii_words,
    rank,
    starts_with_words,
    year_from,
)


class PokemonTcgCatalog(CardCatalog):
    """https://pokemontcg.io — English cards. Optional key in OPGRADE_POKEMONTCG_API_KEY."""

    id = "pokemontcg"
    label = "Pokémon TCG API"
    url = "https://api.pokemontcg.io/v2/cards"

    def search(self, query: CardQuery) -> list[CardCandidate]:
        words = ascii_words(query.name)
        if not words:
            return []
        # The API rejects quoted names and punctuation (HTTP 500): join words with wildcards.
        q = [f"name:{'*'.join(words)}*"]
        if query.number:
            number = query.number.split("/")[0].strip()  # "58/102" -> "58"
            if number.isalnum():
                q.append(f"number:{number}")
        if query.set_name and ascii_words(query.set_name):
            q.append(f"set.name:{'*'.join(ascii_words(query.set_name))}*")

        headers = {}
        if key := os.environ.get("OPGRADE_POKEMONTCG_API_KEY"):
            headers["X-Api-Key"] = key
        response = self._get(
            self.url,
            {"q": " ".join(q), "pageSize": MAX_RESULTS, "select": "id,name,number,rarity,set,images"},
            headers,
            retries=3,  # this API intermittently returns 5xx for valid queries
        )
        if response.status_code != 200:
            raise self._fail(response)

        candidates = []
        for card in response.json().get("data", []):
            card_set = card.get("set") or {}
            printed_total = card_set.get("printedTotal")
            number = card.get("number")
            candidates.append(
                CardCandidate(
                    source=self.id,
                    external_id=card["id"],
                    name=card["name"],
                    set_name=card_set.get("name"),
                    number=f"{number}/{printed_total}" if number and printed_total else number,
                    year=year_from(card_set.get("releaseDate")),
                    rarity=card.get("rarity"),
                    language="en",
                    image_url=(card.get("images") or {}).get("small"),
                    printed_number=pokemon_printed_number(number or "", printed_total),
                )
            )
        return rank(candidates, query)


class TcgdexCatalog(CardCatalog):
    """https://tcgdex.dev — Pokémon, multilingual, open source. Default for Pokémon.

    Chosen over the Pokémon TCG API, which failed ~50 % of valid requests (HTTP 500/502) when
    tested on 2026-10-24. Card lists carry no set name/date, so sets are fetched and cached.
    """

    id = "tcgdex"
    label = "TCGdex"
    base_url = "https://api.tcgdex.net/v2"
    languages = ("en", "fr")  # a French name ("Dracaufeu") is found on the second pass

    def __init__(self, client=None) -> None:
        super().__init__(client)
        self._sets: dict[str, dict[str, dict]] = {}
        self._set_years: dict[tuple[str, str], int | None] = {}

    def _json(self, path: str, params: dict | None = None):
        response = self._get(f"{self.base_url}/{path}", params or {})
        if response.status_code != 200:
            raise self._fail(response)
        return response.json()

    def _set(self, lang: str, set_id: str) -> dict | None:
        """Brief set info {"name", "cardCount": {"official"}}; {} if unknown, None if digital."""
        if lang not in self._sets:
            digital = tcgdex_digital_sets(self._client, self.base_url, lang)
            self._sets[lang] = {s["id"]: (None if s["id"] in digital else s) for s in self._json(f"{lang}/sets")}
        return self._sets[lang].get(set_id, {})

    def _set_year(self, lang: str, set_id: str) -> int | None:
        key = (lang, set_id)
        if key not in self._set_years:
            try:
                self._set_years[key] = year_from(self._json(f"{lang}/sets/{set_id}").get("releaseDate"))
            except CatalogError:
                return None  # year is optional: do not cache a transient failure
        return self._set_years[key]

    def search(self, query: CardQuery) -> list[CardCandidate]:
        name = " ".join(query.name.split())
        if len(name) < 2:
            return []
        number = (query.number or "").split("/")[0].strip()
        for lang in self.languages:
            params = {"name": name}
            if number:
                params["localId"] = number
            candidates = []
            set_of: dict[str, str] = {}  # external_id -> set id
            for card in self._json(f"{lang}/cards", params):
                local_id = card.get("localId", "")
                if number and not _same_number(local_id, number):  # API filter is a substring match
                    continue
                set_id = card["id"].removesuffix(f"-{local_id}")
                card_set = self._set(lang, set_id)
                if card_set is None:  # Pokémon TCG Pocket card: not a physical card
                    continue
                set_name = card_set.get("name")
                if not starts_with_words(set_name, query.set_name):
                    continue
                image = card.get("image")
                external_id = f"{lang}:{card['id']}"
                set_of[external_id] = set_id
                candidates.append(
                    CardCandidate(
                        source=self.id,
                        external_id=external_id,
                        name=card["name"],
                        set_name=set_name,
                        number=local_id,
                        language=lang,
                        image_url=f"{image}/low.webp" if image else None,
                        printed_number=pokemon_printed_number(local_id, (card_set.get("cardCount") or {}).get("official")),
                    )
                )
            if candidates:
                ranked = rank(candidates, query)
                set_ids = list({set_of[c.external_id] for c in ranked})
                with ThreadPoolExecutor(max_workers=8) as pool:
                    years = dict(zip(set_ids, pool.map(lambda s: self._set_year(lang, s), set_ids)))
                for c in ranked:
                    c.year = years[set_of[c.external_id]]
                return ranked
        return []


# TCGdex also lists Pokémon TCG Pocket (mobile game): digital cards that cannot be graded.
TCGDEX_DIGITAL_SERIES = ("tcgp",)


def tcgdex_digital_sets(client, base_url: str, lang: str) -> set[str]:
    sets: set[str] = set()
    for serie in TCGDEX_DIGITAL_SERIES:
        response = client.get(f"{base_url}/{lang}/series/{serie}")
        if response.status_code == 200:
            sets |= {s["id"] for s in response.json().get("sets", [])}
    return sets


def pokemon_printed_number(local_id: str, official_count: int | None) -> str | None:
    """'4' + 102 -> '4/102' as printed at the bottom of the card. Special numbers ('TG05',
    'SWSH050') are printed without a regular set total: keep them alone."""
    if not local_id:
        return None
    if local_id.isdigit() and official_count:
        return f"{local_id}/{official_count}"
    return local_id


def _same_number(a: str, b: str) -> bool:
    """'004' == '4'; non-numeric ids ('SWSH050', 'TG01') compared case-insensitively."""
    if a.isdigit() and b.isdigit():
        return int(a) == int(b)
    return a.lower() == b.lower()


class ScryfallCatalog(CardCatalog):
    """https://scryfall.com — Magic: The Gathering, every printing."""

    id = "scryfall"
    label = "Scryfall"
    url = "https://api.scryfall.com/cards/search"

    def search(self, query: CardQuery) -> list[CardCandidate]:
        words = ascii_words(query.name)
        if not words:
            return []
        q = " ".join(words)
        if query.number and ascii_words(query.number):
            q += f" cn:{ascii_words(query.number.split('/')[0])[0]}"
        response = self._get(self.url, {"q": q, "unique": "prints", "order": "released"})
        if response.status_code == 404:  # Scryfall: no match
            return []
        if response.status_code != 200:
            raise self._fail(response)

        candidates = []
        for card in response.json().get("data", [])[: MAX_RESULTS * 2]:
            images = card.get("image_uris") or ((card.get("card_faces") or [{}])[0].get("image_uris") or {})
            if not starts_with_words(card.get("set_name"), query.set_name):
                continue
            candidates.append(
                CardCandidate(
                    source=self.id,
                    external_id=card["id"],
                    name=card["name"],
                    set_name=card.get("set_name"),
                    number=card.get("collector_number"),
                    year=year_from(card.get("released_at")),
                    rarity=card.get("rarity"),
                    language=card.get("lang"),
                    image_url=images.get("small"),
                    printed_number=card.get("collector_number"),
                )
            )
        return rank(candidates, query)


class YgoProDeckCatalog(CardCatalog):
    """https://ygoprodeck.com — Yu-Gi-Oh! One candidate per printing (set code)."""

    id = "ygoprodeck"
    label = "YGOPRODeck"
    url = "https://db.ygoprodeck.com/api/v7/cardinfo.php"

    def search(self, query: CardQuery) -> list[CardCandidate]:
        name = " ".join(ascii_words(query.name))
        if not name:
            return []
        response = self._get(self.url, {"fname": name})
        if response.status_code == 400 and "No card matching" in response.text:
            return []
        if response.status_code != 200:
            raise self._fail(response)

        wanted_number = (query.number or "").strip().upper()
        candidates = []
        for card in response.json().get("data", []):
            image = ((card.get("card_images") or [{}])[0]).get("image_url_small")
            for printing in card.get("card_sets") or [{}]:
                code = printing.get("set_code")
                if wanted_number and (code or "").upper() != wanted_number:
                    continue
                if not starts_with_words(printing.get("set_name"), query.set_name):
                    continue
                candidates.append(
                    CardCandidate(
                        source=self.id,
                        external_id=f"{card['id']}:{code}" if code else str(card["id"]),
                        name=card["name"],
                        set_name=printing.get("set_name"),
                        number=code,
                        year=None,  # the API has no per-printing release date
                        rarity=printing.get("set_rarity"),
                        language="en",
                        image_url=image,
                        printed_number=code,
                    )
                )
        return rank(candidates, query)


CATALOGS: dict[str, type[CardCatalog]] = {
    c.id: c for c in (TcgdexCatalog, PokemonTcgCatalog, ScryfallCatalog, YgoProDeckCatalog)
}


def build_catalog(catalog_id: str, **kwargs) -> CardCatalog:
    try:
        return CATALOGS[catalog_id](**kwargs)
    except KeyError:
        raise CatalogError(f"Catalogue inconnu : {catalog_id}.") from None
