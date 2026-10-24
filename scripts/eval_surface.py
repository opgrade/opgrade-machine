"""Surface defect detection on real card artwork with simulated relief and camera noise.

    python scripts/eval_surface.py --cards 40 --noise 2

Real catalog images give realistic print/artwork; scratches of random position, orientation and
length are added with a given slope. OPTIMISTIC: the simulator has no gloss, foil or holo
reflections, which are the main source of false alarms on real cards.
"""

from __future__ import annotations

import argparse
import random

import cv2
import httpx
import numpy as np

from opgrade.hardware import LightingMode, Side
from opgrade.hardware.sim import SimCardSpec, SimDefect, render_scene
from opgrade.identify.base import USER_AGENT
from opgrade.identify.sources import tcgdex_references
from opgrade.vision import find_card_quad, rectify
from opgrade.vision.surface import analyze_surface

RAKING = [LightingMode.RAKING_NORTH, LightingMode.RAKING_SOUTH, LightingMode.RAKING_EAST, LightingMode.RAKING_WEST]


def shoot(spec, noise, rng):
    def cap(mode):
        image = render_scene(spec, Side.FRONT, mode).astype(np.float32) + rng.normal(0, noise, (1200, 1000, 3))
        return np.clip(image, 0, 255).astype(np.uint8)

    quad = find_card_quad(cap(LightingMode.BACKLIGHT), 63, 88, silhouette=True)
    return (rectify(cap(LightingMode.DIFFUSE), quad, 63, 88).warped,
            {m.value: rectify(cap(m), quad, 63, 88).warped for m in RAKING})


def random_scratch(rng: random.Random, slope: float) -> SimDefect:
    length = rng.uniform(3, 25)
    angle = rng.uniform(0, np.pi)
    x, y = rng.uniform(8, 55), rng.uniform(8, 80)
    x2 = float(np.clip(x + length * np.cos(angle), 5, 58))
    y2 = float(np.clip(y + length * np.sin(angle), 5, 83))
    return SimDefect("scratch", x, y, x2, y2, width_mm=rng.uniform(0.1, 0.3), slope=slope)


def found(defect: SimDefect, result) -> bool:
    mx, my = (defect.x_mm + defect.x2_mm) / 2, (defect.y_mm + defect.y2_mm) / 2
    return any(d.x_mm - 2 <= mx <= d.x_mm + d.w_mm + 2 and d.y_mm - 2 <= my <= d.y_mm + d.h_mm + 2
               for d in result.defects)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cards", type=int, default=40)
    parser.add_argument("--noise", type=float, default=2.0, help="camera noise, grey levels (std)")
    parser.add_argument("--seed", type=int, default=4)
    args = parser.parse_args()
    rng, nrng = random.Random(args.seed), np.random.default_rng(args.seed)
    client = httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
    refs = rng.sample(list(tcgdex_references(client)), args.cards)
    images = []
    for card, url in refs:
        data = client.get(url.replace("/low.webp", "/high.webp")).content
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is not None:
            images.append(image)
    print(f"{len(images)} cartes réelles, bruit caméra σ = {args.noise} niveaux de gris")

    false_alarms = sum(len(analyze_surface(*shoot(SimCardSpec(front_image=im), args.noise, nrng)).defects)
                       for im in images)
    print(f"Fausses alertes sur cartes propres : {false_alarms} au total ({false_alarms / len(images):.2f} par carte)")
    for slope in (0.02, 0.03, 0.05, 0.08):
        hits = 0
        for im in images:
            scratch = random_scratch(rng, slope)
            hits += found(scratch, analyze_surface(*shoot(SimCardSpec(front_image=im, defects=[scratch]), args.noise, nrng)))
        print(f"Rayure pente {slope:.2f} (≈ {slope * 400:.0f} % de contraste rasant) : détectée {hits}/{len(images)}")


if __name__ == "__main__":
    main()
