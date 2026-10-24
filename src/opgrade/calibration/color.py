"""White balance and lighting uniformity, from a capture of a white or 18 % grey reference card
lying flat on the tray under the diffuse light."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

CENTER = 0.5  # fraction of the image used for white balance (central area)
GRID = 5
MIN_UNIFORMITY = 0.85  # darkest / brightest cell
MIN_LEVEL, MAX_LEVEL = 60.0, 235.0  # mean grey level of the reference: not too dark, not clipped


@dataclass
class ColorResult:
    gains_bgr: tuple[float, float, float]  # multiply B, G, R by these to neutralise the reference
    uniformity: float
    mean_level: float
    cells: list[list[float]]

    def problems(self) -> list[str]:
        out = []
        if self.uniformity < MIN_UNIFORMITY:
            out.append(f"Éclairage peu uniforme : {self.uniformity * 100:.0f} % (≥ {MIN_UNIFORMITY * 100:.0f} % attendu). "
                       "Vérifier le diffuseur et la position des LED.")
        if self.mean_level < MIN_LEVEL:
            out.append("Référence trop sombre : augmenter l'exposition ou l'éclairage.")
        if self.mean_level > MAX_LEVEL:
            out.append("Référence surexposée : diminuer l'exposition.")
        return out


def measure_reference(image: np.ndarray) -> ColorResult:
    h, w = image.shape[:2]
    m = (1 - CENTER) / 2
    center = image[int(h * m) : int(h * (1 - m)), int(w * m) : int(w * (1 - m))].reshape(-1, 3).astype(np.float64)
    mean_bgr = center.mean(axis=0)
    g = mean_bgr[1]
    gains = (float(g / mean_bgr[0]), 1.0, float(g / mean_bgr[2]))
    gray = image.astype(np.float64).mean(axis=2)
    cells = [[float(gray[r * h // GRID : (r + 1) * h // GRID, c * w // GRID : (c + 1) * w // GRID].mean())
              for c in range(GRID)] for r in range(GRID)]
    flat = np.array(cells)
    return ColorResult(gains, float(flat.min() / flat.max()), float(gray.mean()), [[round(v, 1) for v in row] for row in cells])
