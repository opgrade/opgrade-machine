"""Recognise a card from its rectified image: visual index first, printed number (OCR) to settle
which printing it is when several share the same artwork."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .base import CardCandidate, CardCatalog, CardQuery, CatalogError, ascii_words
from .ocr import NumberReader, number_matches, read_tokens
from .visual import REPRINT_MARGIN, VisualIndex, VisualMatch, fingerprint

SEARCH_LIMIT = 15


@dataclass
class Recognition:
    candidates: list[dict]
    message: str
    ocr_text: str | None = None
    tokens: list[str] = field(default_factory=list)


def _as_dict(match: VisualMatch | None, card: CardCandidate | None = None, number_match: bool | None = None) -> dict:
    card = card or match.candidate
    return {
        **card.as_dict(),
        "distance": match.distance if match else None,
        "confidence": match.confidence if match else None,
        "number_match": number_match,
    }


class CardRecognizer:
    def __init__(
        self,
        index: VisualIndex,
        reader: NumberReader | None,
        get_catalog: Callable[[str], CardCatalog],
    ) -> None:
        self.index = index
        self.reader = reader
        self.get_catalog = get_catalog

    def recognize(self, card_bgr: np.ndarray, catalog: str) -> Recognition:
        matches = self.index.search(catalog, fingerprint(card_bgr), limit=SEARCH_LIMIT)
        if not matches or matches[0].confidence == "low":
            return Recognition(
                [_as_dict(m) for m in matches[:10]],
                "Carte non reconnue avec certitude : vérifiez les propositions ou recherchez par nom.",
            )
        best = matches[0]
        contenders = [m for m in matches if m.distance <= best.distance + REPRINT_MARGIN]
        needs_number = catalog == "ygoprodeck" or len(contenders) > 1
        if not needs_number or self.reader is None or not self.reader.available:
            return self._visual_only(matches, contenders)

        text = self.reader.read(card_bgr, catalog)
        tokens = read_tokens(text, catalog)
        if catalog == "ygoprodeck":
            return self._ygo_printing(matches, text, tokens)

        confirmed = [m for m in contenders if number_matches(m.candidate.printed_number, tokens, catalog)]
        if len(confirmed) == 1:
            winner = confirmed[0]
            others = [m for m in matches if m is not winner]
            return Recognition(
                [_as_dict(winner, number_match=True)] + [_as_dict(m, number_match=False) for m in others[:9]],
                f"Édition confirmée par le numéro imprimé « {winner.candidate.printed_number} ».",
                text,
                sorted(tokens),
            )
        result = self._visual_only(matches, contenders)
        result.ocr_text, result.tokens = text, sorted(tokens)
        if not confirmed:
            result.message += " (Numéro imprimé non lu ou inconnu.)" if len(contenders) > 1 else ""
        return result

    def _visual_only(self, matches: list[VisualMatch], contenders: list[VisualMatch]) -> Recognition:
        best = matches[0]
        if best.confidence == "medium":
            message = "Reconnaissance incertaine : vérifiez la carte proposée."
        elif len(contenders) > 1:
            message = (
                "Plusieurs éditions ont la même illustration : vérifiez la série et le numéro "
                "imprimés sur la carte avant de choisir."
            )
        else:
            message = "Carte reconnue — confirmez en touchant la bonne carte."
        return Recognition([_as_dict(m) for m in matches[:10]], message)

    def _ygo_printing(self, matches: list[VisualMatch], text: str, tokens: set[str]) -> Recognition:
        """Yu-Gi-Oh! index entries are artworks: find the printing from the set code read."""
        best = matches[0]
        try:
            printings = [
                p for p in self.get_catalog("ygoprodeck").search(CardQuery(best.candidate.name))
                if ascii_words(p.name) == ascii_words(best.candidate.name)
            ]
        except CatalogError:
            printings = []  # offline: keep the artwork, the operator picks the printing later
        confirmed = [p for p in printings if number_matches(p.printed_number, tokens, "ygoprodeck")]
        if confirmed:
            winner = confirmed[0]
            winner.image_url = best.candidate.image_url  # the artwork actually seen
            rest = [_as_dict(m) for m in matches[1:9]]
            return Recognition(
                [_as_dict(best, winner, number_match=True)] + rest,
                f"Carte reconnue, édition confirmée par le code imprimé « {winner.printed_number} ».",
                text,
                sorted(tokens),
            )
        result = self._visual_only(matches, [best])
        result.message = (
            "Illustration reconnue ; code d'édition non lu. Touchez la carte pour choisir l'édition."
        )
        result.ocr_text, result.tokens = text, sorted(tokens)
        return result
