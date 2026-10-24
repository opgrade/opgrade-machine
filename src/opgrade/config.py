"""Runtime settings, read from OPGRADE_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_RULES_PATH = PACKAGE_DIR / "rules" / "grading_rules.yaml"


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    simulation: bool = True
    data_dir: Path = Path("data")
    machine_id: str = "OPGX-DEV-001"
    host: str = "127.0.0.1"
    port: int = 8000
    rules_path: Path = DEFAULT_RULES_PATH
    database_url: str = field(default="")

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        if not self.database_url:
            self.database_url = f"sqlite:///{(self.data_dir / 'opgrade.db').as_posix()}"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            simulation=_env_bool("OPGRADE_SIMULATION", True),
            data_dir=Path(os.environ.get("OPGRADE_DATA_DIR", "data")),
            machine_id=os.environ.get("OPGRADE_MACHINE_ID", "OPGX-DEV-001"),
            host=os.environ.get("OPGRADE_HOST", "127.0.0.1"),
            port=int(os.environ.get("OPGRADE_PORT", "8000")),
            rules_path=Path(os.environ.get("OPGRADE_RULES_PATH", str(DEFAULT_RULES_PATH))),
            database_url=os.environ.get("OPGRADE_DATABASE_URL", ""),
        )
