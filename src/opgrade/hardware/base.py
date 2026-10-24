"""Hardware abstraction layer (HAL).

Business code (orchestration, grading) only depends on these interfaces, never on a concrete
driver. Every physical component gets a simulated and a real implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import numpy as np


class Side(str, Enum):
    FRONT = "front"
    BACK = "back"


class LightingMode(str, Enum):
    OFF = "off"
    DIFFUSE = "diffuse"
    # Raking lights (low angle) reveal scratches and dents; used from V1.x multi-light capture.
    RAKING_NORTH = "raking_north"
    RAKING_SOUTH = "raking_south"
    RAKING_EAST = "raking_east"
    RAKING_WEST = "raking_west"


class Camera(ABC):
    @abstractmethod
    def capture(self) -> np.ndarray:
        """Return a full-resolution BGR uint8 image."""

    def close(self) -> None:  # pragma: no cover - default no-op
        pass


class Lighting(ABC):
    @abstractmethod
    def set_mode(self, mode: LightingMode) -> None: ...

    @property
    @abstractmethod
    def mode(self) -> LightingMode: ...

    def off(self) -> None:
        self.set_mode(LightingMode.OFF)


@dataclass
class Hardware:
    camera: Camera
    lighting: Lighting
    simulated: bool

    def close(self) -> None:
        self.lighting.off()
        self.camera.close()
