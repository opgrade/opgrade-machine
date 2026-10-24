"""Real Raspberry Pi 5 hardware.

NOT YET VALIDATED ON HARDWARE. Imports are lazy so the package works on a dev PC.
- Camera: picamera2 (install with `sudo apt install python3-picamera2`, venv with
  --system-site-packages).
- Lighting: one PWM channel per light group through logic-level MOSFETs (gpiozero).
"""

from __future__ import annotations

import numpy as np

from .base import Camera, Hardware, Lighting, LightingMode

# BCM pin per lighting group. To be confirmed by the wiring plan.
DEFAULT_LIGHT_PINS: dict[LightingMode, int] = {
    LightingMode.DIFFUSE: 18,
    LightingMode.RAKING_NORTH: 23,
    LightingMode.RAKING_SOUTH: 24,
    LightingMode.RAKING_EAST: 25,
    LightingMode.RAKING_WEST: 12,
}


class PiCamera(Camera):
    def __init__(self, exposure_us: int | None = None, analogue_gain: float | None = None) -> None:
        from picamera2 import Picamera2  # type: ignore[import-not-found]

        self._cam = Picamera2()
        config = self._cam.create_still_configuration(main={"format": "BGR888"})
        self._cam.configure(config)
        controls: dict[str, object] = {"AwbEnable": False}
        if exposure_us is not None:
            controls["ExposureTime"] = exposure_us
            controls["AeEnable"] = False
        if analogue_gain is not None:
            controls["AnalogueGain"] = analogue_gain
        self._cam.set_controls(controls)
        self._cam.start()

    def capture(self) -> np.ndarray:
        # picamera2 "BGR888" is laid out as RGB in memory order -> returns BGR array for OpenCV.
        return self._cam.capture_array("main")

    def close(self) -> None:
        self._cam.stop()
        self._cam.close()


class GpioLighting(Lighting):
    def __init__(self, pins: dict[LightingMode, int] | None = None, brightness: float = 1.0) -> None:
        from gpiozero import PWMLED  # type: ignore[import-not-found]

        self._leds = {mode: PWMLED(pin) for mode, pin in (pins or DEFAULT_LIGHT_PINS).items()}
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


def build_pi_hardware() -> Hardware:
    return Hardware(camera=PiCamera(), lighting=GpioLighting(), simulated=False)
