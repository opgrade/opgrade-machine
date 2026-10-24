"""Card identification against public online catalogs.

Identification is a convenience, never a requirement: when a catalog is offline or unknown the
operator types the card details by hand, so the machine keeps working without Internet.
"""

from __future__ import annotations

import re
import time
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

import httpx

from .. import __version__

USER_AGENT = f"OpGradeX/{__version__}"
DEFAULT_TIMEOUT = 8.0
MAX_RESULTS = 30


@dataclass
class CardQuery:
    name: str
    number: str | None = None
    set_name: str | None = None


@dataclass
class CardCandidate:
    source: str  # catalog id, e.g. "pokemontcg"
    external_id: str
    name: str
    set_name: str | None = None
    number: str | None = None
    year: int | None = None
    rarity: str | None = None
    language: str | None = None
    image_url: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class CatalogError(Exception):
    """Catalog unreachable or returned an error. Message is operator-facing (French)."""


class CardCatalog(ABC):
    id: str
    label: str
    retry_delay = 0.4  # seconds, doubled on each retry

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            timeout=DEFAULT_TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )

    @abstractmethod
    def search(self, query: CardQuery) -> list[CardCandidate]: ...

    def _get(self, url: str, params: dict, headers: dict | None = None, retries: int = 1) -> httpx.Response:
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(self.retry_delay * 2 ** (attempt - 1))
            try:
                response = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                if attempt < retries:
                    continue
                raise CatalogError(f"{self.label} injoignable (hors ligne ?).") from exc
            if response.status_code < 500 or attempt == retries:
                return response
        raise AssertionError("unreachable")  # pragma: no cover

    def _fail(self, response: httpx.Response) -> CatalogError:
        return CatalogError(f"{self.label} a répondu une erreur ({response.status_code}).")


def ascii_words(text: str) -> list[str]:
    """'Mr. Mime' -> ['mr', 'mime']; accents removed ('Pokémon' -> 'pokemon')."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", folded)


def starts_with_words(text: str | None, prefix: str | None) -> bool:
    """True if `text` begins with the words of `prefix` (case/accents/punctuation ignored)."""
    wanted = ascii_words(prefix or "")
    return ascii_words(text or "")[: len(wanted)] == wanted


def year_from(date: str | None) -> int | None:
    match = re.match(r"(\d{4})", date or "")
    return int(match.group(1)) if match else None


def rank(candidates: list[CardCandidate], query: CardQuery) -> list[CardCandidate]:
    """Exact name first, then names starting with the query, then the rest (stable)."""
    wanted = ascii_words(query.name)

    def key(c: CardCandidate) -> int:
        words = ascii_words(c.name)
        if words == wanted:
            return 0
        if words[: len(wanted)] == wanted:
            return 1
        return 2

    return sorted(candidates, key=key)[:MAX_RESULTS]
