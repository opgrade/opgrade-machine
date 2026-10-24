"""Read the number printed on the card (OCR) to tell apart printings with the same artwork.

Engine: RapidOCR (PaddleOCR models on onnxruntime, bundled with the package: works offline).
Tested 2026-10-24 on catalog scans at the machine resolution (10 px/mm): "4/102", "4/64",
"009/191", "3/132" read correctly, ~1–2 s per card on a PC.
"""

from __future__ import annotations

import logging
import re
import threading

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Where the number is printed, relative to the card (x0, y0, x1, y1).
OCR_REGIONS: dict[str, list[tuple[float, float, float, float]]] = {
    "tcgdex": [(0.0, 0.86, 1.0, 1.0)],  # bottom band: left (modern) or right (WotC era)
    "pokemontcg": [(0.0, 0.86, 1.0, 1.0)],
    "scryfall": [(0.0, 0.88, 1.0, 1.0)],  # collector number, bottom left / centre
    "ygoprodeck": [(0.40, 0.69, 1.0, 0.78)],  # set code under the artwork, right side
}
UPSCALE = 2.0  # small print at 10 px/mm reads much better upscaled
MIN_SCORE = 0.5

_TOKEN = re.compile(r"[A-Z0-9]+(?:[/-][A-Z0-9]+)*")


class NumberReader:
    """Lazy OCR engine. `available` is False when RapidOCR is not installed: callers skip OCR."""

    def __init__(self) -> None:
        self._engine = None
        self._failed = False
        self._lock = threading.Lock()

    def _get_engine(self):
        with self._lock:
            if self._engine is None and not self._failed:
                try:
                    from rapidocr import RapidOCR

                    self._engine = RapidOCR()
                except Exception as exc:  # missing package or model: OCR is optional
                    log.warning("OCR indisponible : %s", exc)
                    self._failed = True
            return self._engine

    @property
    def available(self) -> bool:
        return self._get_engine() is not None

    def read(self, card_bgr: np.ndarray, catalog: str) -> str:
        """All text found in the number regions of an upright card image, joined by spaces."""
        engine = self._get_engine()
        if engine is None:
            return ""
        h, w = card_bgr.shape[:2]
        texts = []
        for x0, y0, x1, y1 in OCR_REGIONS.get(catalog, OCR_REGIONS["tcgdex"]):
            region = card_bgr[int(y0 * h) : int(y1 * h), int(x0 * w) : int(x1 * w)]
            region = cv2.resize(region, None, fx=UPSCALE, fy=UPSCALE, interpolation=cv2.INTER_CUBIC)
            with self._lock:  # onnxruntime sessions are not guaranteed thread-safe here
                result = engine(region)
            for text, score in zip(result.txts or (), result.scores or ()):
                if score >= MIN_SCORE:
                    texts.append(text)
        return " ".join(texts)


def _normalize(token: str, catalog: str) -> str:
    token = token.upper().replace(" ", "")
    parts = re.split(r"([/-])", token)
    out = []
    for part in parts:
        if catalog == "ygoprodeck" and len(part) >= 3:
            # Set code tail "EN0O5": the last 3 characters are digits, O is a misread 0.
            part = part[:-3] + part[-3:].replace("O", "0")
        out.append(str(int(part)) if part.isdigit() else part)  # "009" -> "9"
    return "".join(out)


def read_tokens(text: str, catalog: str) -> set[str]:
    """Number-like tokens of the OCR text, normalised ("009/191" -> "9/191")."""
    # Join "4 / 102" and "LOB - EN005" that OCR may split.
    compact = re.sub(r"\s*([/-])\s*", r"\1", text.upper())
    tokens = {_normalize(t, catalog) for t in _TOKEN.findall(compact) if any(c.isdigit() for c in t)}
    if catalog == "scryfall":
        # Older Magic cards print "123/350": the collector number is the part before "/".
        tokens |= {t.split("/")[0] for t in tokens if "/" in t}
    return tokens


def number_matches(printed_number: str | None, tokens: set[str], catalog: str) -> bool:
    if not printed_number:
        return False
    return _normalize(printed_number, catalog) in tokens
