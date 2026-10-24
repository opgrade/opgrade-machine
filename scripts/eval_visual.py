"""Measure visual recognition accuracy on real catalog images seen through the simulator.

    python scripts/eval_visual.py --catalog tcgdex --index-size 2000 --queries 150

Builds a throwaway index from a random sample of the catalog, then "photographs" sampled cards
with the simulator (rotation, lighting, blur, noise, colour cast, JPEG) and searches them.
This is an OPTIMISTIC bound: queries and references come from the same scans. Real photos
(print vs scan, glare, holo) must be evaluated on the optical bench.
"""

from __future__ import annotations

import argparse
import random
import tempfile
import time
from pathlib import Path

import cv2
import httpx
import numpy as np

from opgrade.hardware import LightingMode, Side
from opgrade.hardware.sim import SimCardSpec, render_scene
from opgrade.identify import build_catalog
from opgrade.identify.base import USER_AGENT
from opgrade.identify.build_index import build
from opgrade.identify.ocr import NumberReader
from opgrade.identify.recognize import CardRecognizer
from opgrade.identify.sources import SOURCES
from opgrade.identify.visual import REPRINT_MARGIN, VisualIndex, fingerprint
from opgrade.vision import DetectionError, detect_card, find_card_quad, rectify

DIMENSIONS = {"tcgdex": (63, 88), "scryfall": (63, 88), "ygoprodeck": (59, 86)}


def simulated_capture(image: np.ndarray, rng: random.Random, dims: tuple[float, float]) -> tuple[np.ndarray, np.ndarray]:
    """Returns (backlit silhouette, front-lit photo) of the same card position."""
    spec = SimCardSpec(
        width_mm=dims[0], height_mm=dims[1], front_image=image,
        rotation_deg=rng.uniform(-6, 6), blur=rng.uniform(0, 1.2), brightness=rng.uniform(0.75, 1.2),
    )
    silhouette = render_scene(spec, Side.FRONT, LightingMode.BACKLIGHT)
    scene = render_scene(spec, Side.FRONT, LightingMode.DIFFUSE).astype(np.float32)
    scene *= np.array([rng.uniform(0.9, 1.1) for _ in range(3)], np.float32)  # white balance error
    scene += np.random.default_rng(rng.randrange(1 << 30)).normal(0, 4, scene.shape)
    scene = np.clip(scene, 0, 255).astype(np.uint8)
    ok, jpeg = cv2.imencode(".jpg", scene, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return silhouette, cv2.imdecode(jpeg, cv2.IMREAD_COLOR)


def ambiguous_queries(index, sample, client, catalog, wanted, rng, max_tries=4000):
    """Cards whose reference fingerprint has another index entry within REPRINT_MARGIN."""
    found = []
    for card, url in rng.sample(sample, min(max_tries, len(sample))):
        image = cv2.imdecode(np.frombuffer(client.get(url).content, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            continue
        matches = index.search(catalog, fingerprint(image), limit=3)
        close = [m for m in matches if m.distance <= matches[0].distance + REPRINT_MARGIN]
        if len(close) > 1:
            found.append((card, url))
            if len(found) >= wanted:
                break
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", default="tcgdex", choices=sorted(SOURCES))
    parser.add_argument("--index-size", type=int, default=2000)
    parser.add_argument("--queries", type=int, default=150)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--no-backlight", action="store_true", help="détecter sur fond noir, sans silhouette")
    parser.add_argument("--ocr", action="store_true", help="évaluer aussi la lecture du numéro (recognizer complet)")
    parser.add_argument("--ambiguous-only", action="store_true", help="seulement des cartes réimprimées (même illustration)")
    args = parser.parse_args()
    rng = random.Random(args.seed)

    client = httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    refs = list(SOURCES[args.catalog](client))
    sample = rng.sample(refs, min(args.index_size, len(refs)))
    index = VisualIndex(Path(tempfile.mkdtemp()) / "eval_index.db")
    print(f"Indexation de {len(sample)} cartes sur {len(refs)}…")
    build(index, sample, client, workers=8, log=lambda *_: None)

    recognizer = None
    if args.ocr:
        catalogs = {}
        recognizer = CardRecognizer(index, NumberReader(), lambda c: catalogs.setdefault(c, build_catalog(c)))
    top1 = top5 = same_name = detect_fail = 0
    ocr_top1 = ocr_ran = ocr_confirmed = 0
    ocr_seconds = []
    wrongly_confirmed = []  # the dangerous case: OCR "confirms" the wrong printing
    good, wrong_best = [], []
    if args.ambiguous_only:
        queries = ambiguous_queries(index, sample, client, args.catalog, args.queries, rng)
        print(f"{len(queries)} cartes dont l'illustration existe dans plusieurs éditions.")
    else:
        queries = rng.sample(sample, min(args.queries, len(sample)))
    for card, url in queries:
        if args.ocr and args.catalog == "tcgdex":
            url = url.replace("/low.webp", "/high.webp")  # legible print, closer to a real photo
        image = cv2.imdecode(np.frombuffer(client.get(url).content, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            detect_fail += 1
            continue
        dims = DIMENSIONS[args.catalog]
        silhouette, photo = simulated_capture(image, rng, dims)
        try:
            if args.no_backlight:
                warped = detect_card(photo, *dims).warped
            else:
                warped = rectify(photo, find_card_quad(silhouette, *dims, silhouette=True), *dims).warped
        except DetectionError:
            detect_fail += 1
            continue
        matches = index.search(args.catalog, fingerprint(warped), limit=5)
        ids = [m.candidate.external_id for m in matches]
        top1 += ids[0] == card.external_id
        top5 += card.external_id in ids
        same_name += matches[0].candidate.name == card.name
        good += [m.distance for m in matches if m.candidate.external_id == card.external_id]
        wrong = [m.distance for m in matches if m.candidate.external_id != card.external_id]
        if wrong:
            wrong_best.append(min(wrong))
        if recognizer:
            t = time.monotonic()
            result = recognizer.recognize(warped, args.catalog)
            elapsed = time.monotonic() - t
            if result.ocr_text is not None:
                ocr_ran += 1
                ocr_seconds.append(elapsed)
            best = result.candidates[0]
            ocr_confirmed += bool(best.get("number_match"))
            hit = best["external_id"] == card.external_id
            if args.catalog == "ygoprodeck":  # the printing is not in the index: judge the name
                hit = best["name"] == card.name
            ocr_top1 += hit
            if best.get("number_match") and not hit:
                wrongly_confirmed.append((card.name, card.printed_number, best["printed_number"], result.ocr_text))

    n = len(queries) - detect_fail
    print(f"Requêtes : {n} (détection échouée : {detect_fail})")
    print(f"Top-1 exact : {top1 / n:.1%} · top-5 : {top5 / n:.1%} · bon nom en 1er : {same_name / n:.1%}")
    if good:
        print(f"Distance bonne carte : médiane {np.median(good):.0f}, 95e centile {np.percentile(good, 95):.0f}")
    if wrong_best:
        print(f"Distance meilleure mauvaise carte : 5e centile {np.percentile(wrong_best, 5):.0f}, médiane {np.median(wrong_best):.0f}")
    if recognizer:
        print(f"Avec OCR : top-1 exact {ocr_top1 / n:.1%} · OCR lancé sur {ocr_ran}/{n} cartes ambiguës · "
              f"numéro confirmé {ocr_confirmed}/{n}"
              + (f" · {np.mean(ocr_seconds):.1f} s en moyenne quand l'OCR tourne" if ocr_seconds else ""))
        print(f"Confirmées à tort : {len(wrongly_confirmed)}")
        for item in wrongly_confirmed:
            print("   ", item)


if __name__ == "__main__":
    main()
