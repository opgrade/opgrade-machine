"""Real Raspberry Pi 5 hardware.

NOT YET VALIDATED ON HARDWARE: run `python -m opgrade.hardware.check` on the machine first
(docs/RASPBERRY_PI.md). Imports are lazy so the package works on a dev PC.
- Camera: picamera2 (`sudo apt install python3-picamera2`, venv with --system-site-packages).
- Lighting: one PWM channel per light group through logic-level MOSFETs (gpiozero + lgpio).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import numpy as np

from .base import Camera, Hardware, Lighting, LightingMode

log = logging.getLogger(__name__)

# BCM pin per lighting group. To be confirmed by the wiring plan.
DEFAULT_LIGHT_PINS: dict[LightingMode, int] = {
    LightingMode.DIFFUSE: 18,
    LightingMode.RAKING_NORTH: 23,
    LightingMode.RAKING_SOUTH: 24,
    LightingMode.RAKING_EAST: 25,
    LightingMode.RAKING_WEST: 12,
    LightingMode.BACKLIGHT: 16,
}


@dataclass
class CameraSettings:
    """Fixed exposure and gain: two captures of the same card must be comparable."""

    width: int = 4056  # IMX477 full resolution
    height: int = 3040
    exposure_us: int = 20000
    analogue_gain: float = 1.0
    # Frames thrown away after a lighting or control change, so the kept frame was exposed
    # entirely under the new conditions (the sensor streams continuously).
    settle_frames: int = 2

    @classmethod
    def from_env(cls) -> "CameraSettings":
        return cls(
            exposure_us=int(os.environ.get("OPGRADE_CAMERA_EXPOSURE_US", cls.exposure_us)),
            analogue_gain=float(os.environ.get("OPGRADE_CAMERA_GAIN", cls.analogue_gain)),
        )


class PiCamera(Camera):
    def __init__(self, settings: CameraSettings | None = None) -> None:
        from picamera2 import Picamera2  # type: ignore[import-not-found]

        self.settings = settings or CameraSettings.from_env()
        self._cam = Picamera2()
        # picamera2 naming is inverted w.r.t. OpenCV: "RGB888" is stored B, G, R in memory,
        # i.e. exactly the BGR array OpenCV expects. ("BGR888" would swap red and blue.)
        config = self._cam.create_still_configuration(
            main={"size": (self.settings.width, self.settings.height), "format": "RGB888"},
            buffer_count=2,
        )
        self._cam.configure(config)
        self._cam.set_controls({
            "AeEnable": False,
            "ExposureTime": self.settings.exposure_us,
            "AnalogueGain": self.settings.analogue_gain,
            "AwbEnable": False,  # white balance comes from the calibration (set_colour_gains)
        })
        self._cam.start()
        self._discard(self.settings.settle_frames + 2)

    def _discard(self, frames: int) -> None:
        for _ in range(frames):
            self._cam.capture_request().release()

    def capture(self) -> np.ndarray:
        self._discard(self.settings.settle_frames)
        return self._cam.capture_array("main")

    @property
    def colour_gains(self) -> tuple[float, float, float]:
        red, blue = self._cam.capture_metadata().get("ColourGains", (1.0, 1.0))
        return (float(blue), 1.0, float(red))

    def set_colour_gains(self, gains_bgr: tuple[float, float, float]) -> None:
        blue, green, red = gains_bgr
        # The sensor pipeline only takes red and blue gains, relative to green.
        self._cam.set_controls({"AwbEnable": False, "ColourGains": (red / green, blue / green)})
        self._discard(self.settings.settle_frames + 1)

    def info(self) -> dict:
        from picamera2 import Picamera2  # type: ignore[import-not-found]

        return {"cameras": Picamera2.global_camera_info(), "settings": self.settings.__dict__}

    def close(self) -> None:
        self._cam.stop()
        self._cam.close()


class GpioLighting(Lighting):
    def __init__(self, pins: dict[LightingMode, int] | None = None, brightness: float = 1.0) -> None:
        from gpiozero import PWMLED  # type: ignore[import-not-found]

        self.pins = pins or DEFAULT_LIGHT_PINS
        self._leds = {mode: PWMLED(pin) for mode, pin in self.pins.items()}
        self._brightness = brightness
        self._mode = LightingMode.OFF
        self.set_mode(LightingMode.OFF)

    def set_mode(self, mode: LightingMode) -> None:
        for led_mode, led in self._leds.items():
            led.value = self._brightness if led_mode == mode else 0.0
        self._mode = mode

    @property
    def mode(self) -> LightingMode:
        return self._mode


def build_pi_hardware(has_backlight: bool | None = None) -> Hardware:
    if has_backlight is None:
        # Set once the backlit tray (LED panel under opal acrylic) is built and wired.
        has_backlight = os.environ.get("OPGRADE_BACKLIGHT", "0").lower() in {"1", "true", "yes", "on"}
    return Hardware(camera=PiCamera(), lighting=GpioLighting(), simulated=False, has_backlight=has_backlight)
