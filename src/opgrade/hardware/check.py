"""First-boot hardware check on the machine.

    OPGRADE_SIMULATION=0 python -m opgrade.hardware.check [--out data/hardware_check]

Lists the camera, takes one photo per light (watch that the right LEDs switch on), reports
resolution, brightness, clipping and sharpness, and saves the photos for inspection.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from ..config import Settings
from . import LightingMode, build_hardware

MODES = [
    LightingMode.OFF, LightingMode.DIFFUSE, LightingMode.BACKLIGHT, LightingMode.RAKING_NORTH,
    LightingMode.RAKING_SOUTH, LightingMode.RAKING_EAST, LightingMode.RAKING_WEST,
]


def frame_stats(image: np.ndarray) -> dict:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    centre = gray[h // 3 : 2 * h // 3, w // 3 : 2 * w // 3]
    return {
        "size": f"{w}x{h}",
        "mean": round(float(gray.mean()), 1),
        "clipped_%": round(float((gray >= 250).mean() * 100), 2),
        "sharpness_centre": round(float(cv2.Laplacian(centre, cv2.CV_64F).var()), 1),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m opgrade.hardware.check")
    parser.add_argument("--out", default=None, help="dossier des photos (défaut : <data>/hardware_check)")
    parser.add_argument("--pause", type=float, default=1.0, help="secondes par éclairage (vérification visuelle)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    out = Path(args.out or settings.data_dir / "hardware_check")
    out.mkdir(parents=True, exist_ok=True)
    print(f"Mode : {'SIMULATION' if settings.simulation else 'matériel réel'}")
    try:
        hardware = build_hardware(settings.simulation)
    except Exception as exc:  # missing picamera2/gpiozero, camera not detected, GPIO busy...
        print(f"ÉCHEC à l'initialisation du matériel : {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Vérifier : rpicam-hello --list-cameras ; câble caméra ; venv --system-site-packages.", file=sys.stderr)
        return 1
    info = getattr(hardware.camera, "info", None)
    if info:
        print("Caméra :", info())
    print(f"Rétroéclairage déclaré : {'oui' if hardware.has_backlight else 'non'}")
    ok = True
    try:
        for mode in MODES:
            hardware.lighting.set_mode(mode)
            time.sleep(args.pause)
            image = hardware.camera.capture()
            stats = frame_stats(image)
            path = out / f"{mode.value}.jpg"
            cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 92])
            print(f"  {mode.value:13} {stats}  -> {path}")
            if mode == LightingMode.DIFFUSE and (stats["mean"] < 40 or stats["clipped_%"] > 2):
                ok = False
                print("    ! exposition à ajuster (OPGRADE_CAMERA_EXPOSURE_US / OPGRADE_CAMERA_GAIN)")
    finally:
        hardware.close()
    print("Mise au point : tourner la bague jusqu'au maximum de « sharpness_centre » (écran Calibration).")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
