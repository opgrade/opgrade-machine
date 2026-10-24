from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CatalogId = Literal["tcgdex", "pokemontcg", "scryfall", "ygoprodeck"]  # keep in sync with identify.CATALOGS


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ClientIn(BaseModel):
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    email: str | None = None
    phone: str | None = None
    notes: str | None = None


class ClientOut(ORM, ClientIn):
    id: int
    uuid: str
    created_at: datetime


class CardTypeIn(BaseModel):
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    category: str = "other"
    width_mm: float = Field(gt=20, lt=200)
    height_mm: float = Field(gt=20, lt=200)
    thickness_mm: float | None = None
    has_border: bool = True
    catalog: CatalogId | None = None


class CardTypeOut(ORM, CardTypeIn):
    id: int
    active: bool


class CardIn(BaseModel):
    client_id: int
    card_type_id: int
    name: str = Field(min_length=1)
    set_name: str | None = None
    year: int | None = None
    card_number: str | None = None
    language: str | None = None
    rarity: str | None = None
    external_source: CatalogId | None = None
    external_id: str | None = Field(default=None, max_length=100)
    reference_image_url: str | None = Field(default=None, max_length=500, pattern=r"^https://")
    notes: str | None = None


class CardOut(ORM, CardIn):
    id: int
    uuid: str
    created_at: datetime


class SessionIn(BaseModel):
    card_id: int
    operator: str | None = None


class CaptureOut(ORM):
    id: int
    side: str
    lighting: str
    width_px: int
    height_px: int
    sharpness: float


class MeasurementOut(ORM):
    side: str | None
    name: str
    value: float
    unit: str


class SubgradeOut(ORM):
    criterion: str
    value: float
    source: str
    confidence: float | None
    details: dict | None


class SessionOut(ORM):
    id: int
    uuid: str
    card_id: int
    machine_id: str
    state: str
    simulated: bool
    operator: str | None
    rules_version: str
    model_version: str | None
    final_grade: int | None
    confidence: float | None
    needs_review: bool
    explanation: list | None
    last_message: str | None
    created_at: datetime
    completed_at: datetime | None
    captures: list[CaptureOut]
    measurements: list[MeasurementOut]
    subgrades: list[SubgradeOut]


class CaptureResult(BaseModel):
    accepted: bool
    message: str
    session: SessionOut


Grade = Field(ge=1, le=10, multiple_of=0.5)


class ReviewIn(BaseModel):
    corners: float = Grade
    edges: float = Grade
    surface: float = Grade
    centering_override: float | None = Field(default=None, ge=1, le=10, multiple_of=0.5)
    operator: str | None = None


class CandidateOut(BaseModel):
    source: str
    external_id: str
    name: str
    set_name: str | None
    number: str | None
    year: int | None
    rarity: str | None
    language: str | None
    image_url: str | None
    # Image recognition only: fingerprint distance (0 = identical, 128 = max) and its reading.
    distance: int | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    printed_number: str | None = None
    # True when the number read on the card (OCR) matches this printing.
    number_match: bool | None = None


class IdentifyImageIn(BaseModel):
    card_type_id: int


class SimReferenceIn(BaseModel):
    catalog: CatalogId = "tcgdex"


class IdentifyOut(BaseModel):
    catalog: str | None
    available: bool
    message: str | None
    candidates: list[CandidateOut]
    ocr_text: str | None = None  # raw text read near the printed number (for checking)


class SimCardIn(BaseModel):
    front_left_share: float = Field(50, ge=5, le=95)
    front_top_share: float = Field(50, ge=5, le=95)
    back_left_share: float = Field(50, ge=5, le=95)
    back_top_share: float = Field(50, ge=5, le=95)
    width_mm: float = 63.0
    height_mm: float = 88.0
    rotation_deg: float = Field(1.5, ge=-20, le=20)
    blur: float = Field(0, ge=0, le=10)
