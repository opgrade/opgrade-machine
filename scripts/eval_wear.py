"""Corner/edge wear detection on real card artwork with simulated wear and camera noise.

    python scripts/eval_wear.py --cards 40 --noise 2

False alarms on clean real cards (every border colour, full art...), then detection of random
corner whitening, edge whitening and dinged corners. OPTIMISTIC: no gloss, no card thickness,
no real paper fibres.
"""

from __future__ import annotations

import argparse
import random

import cv2
import httpx
import numpy as np

from opgrade.hardware import LightingMode, Side
from opgrade.hardware.sim import SimCardSpec, SimWear, render_scene
from opgrade.identify.base import USER_AGENT
from opgrade.identify.sources import tcgdex_references
from opgrade.vision import find_card_quad, rectify
from opgrade.vision.wear import analyze_wear


def shoot(spec, noise, rng):
    def cap(mode):
        image = render_scene(spec, Side.FRONT, mode).astype(np.float32) + rng.normal(0, noise, (1200, 1000, 3))
        return np.clip(image, 0, 255).astype(np.uint8)

    sil = cap(LightingMode.BACKLIGHT)
    quad = find_card_quad(sil, 63, 88, silhouette=True)
    return rectify(cap(LightingMode.DIFFUSE), quad, 63, 88).warped, rectify(sil, quad, 63, 88).warped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cards", type=int, default=40)
    parser.add_argument("--noise", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=5)
    args = parser.parse_args()
    rng, nrng = random.Random(args.seed), np.random.default_rng(args.seed)
    client = httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
    # Only scans WITH transparency: their outline is the true card outline. Scans without it
    # include a rim of scanner background, which would be counted as whitening (data artefact:
    # on the machine the outline comes from the backlit silhouette).
    images, skipped = [], 0
    refs = rng.sample(list(tcgdex_references(client)), args.cards * 3)
    for _, url in refs:
        if len(images) >= args.cards:
            break
        image = cv2.imdecode(np.frombuffer(client.get(url.replace("/low.webp", "/high.webp")).content, np.uint8),
                             cv2.IMREAD_UNCHANGED)
        if image is None or image.ndim != 3 or image.shape[2] != 4:
            skipped += 1
            continue
        images.append(image)
    print(f"{len(images)} cartes réelles (scans avec transparence ; {skipped} sans, écartés), bruit σ = {args.noise}")

    false_corners = false_edges = unmeasurable = 0
    for im in images:
        r = analyze_wear(*shoot(SimCardSpec(front_image=im), args.noise, nrng))
        false_corners += sum(c.whitening_mm2 >= 0.1 or c.missing_mm2 >= 0.05 for c in r.corners)
        false_edges += sum(e.whitened_length_mm >= 0.5 or e.nicks > 0 for e in r.edges)
        unmeasurable += sum(not c.measurable for c in r.corners) + sum(not e.measurable for e in r.edges)
    total = len(images) * 4
    print(f"Fausses alertes : coins {false_corners}/{total} · bordures {false_edges}/{total} · "
          f"non mesurables {unmeasurable}/{2 * total} (bordure claire)")

    corners, edges, missed_unmeasurable = ("tl", "tr", "br", "bl"), ("top", "right", "bottom", "left"), 0
    hits = {"corner_whitening": 0, "edge_whitening": 0, "corner_ding": 0}
    for im in images:
        corner, edge = rng.choice(corners), rng.choice(edges)
        length = 63 if edge in ("top", "bottom") else 88
        start = rng.uniform(8, length - 20)
        cases = {
            "corner_whitening": SimWear("corner_whitening", corner=corner, size_mm=rng.uniform(1.5, 3), depth_mm=0.8, density=0.6),
            "edge_whitening": SimWear("edge_whitening", edge=edge, start_mm=start, end_mm=start + rng.uniform(3, 12),
                                      depth_mm=0.6, density=0.6),
            "corner_ding": SimWear("corner_ding", corner=corner, size_mm=rng.uniform(0.8, 2.0)),
        }
        for kind, wear in cases.items():
            r = analyze_wear(*shoot(SimCardSpec(front_image=im, wear=[wear]), args.noise, nrng))
            if kind == "edge_whitening":
                e = next(e for e in r.edges if e.name == edge)
                hits[kind] += e.whitened_length_mm >= 0.5
                missed_unmeasurable += not e.measurable
            else:
                c = next(c for c in r.corners if c.name == corner)
                hits[kind] += c.missing_mm2 >= 0.05 if kind == "corner_ding" else c.whitening_mm2 >= 0.1
    for kind, n in hits.items():
        print(f"Détecté {kind:16} : {n}/{len(images)}")
    print(f"(bordures non mesurables parmi les cas d'usure de bordure : {missed_unmeasurable})")


if __name__ == "__main__":
    main()
