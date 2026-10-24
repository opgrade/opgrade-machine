from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CatalogId = Literal["tcgdex", "pokemontcg", "scryfall", "ygoprodeck"]  # keep in sync with identify.CATALOGS


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


ConsentMethod = Literal["touch", "paper", "verbal", "email"]


class ClientFields(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    notes: str | None = None


class ClientIn(ClientFields):
    """New client. Loi 25: one contact is enough, and service consent is mandatory."""

    consent_service: bool
    consent_policy_version: str  # version of the notice shown to the client
    consent_marketing: bool = False
    consent_method: ConsentMethod = "touch"

    @model_validator(mode="after")
    def _check(self) -> "ClientIn":
        if not (self.email or self.phone):
            raise ValueError("Un moyen de contact est requis : courriel ou téléphone.")
        if not self.consent_service:
            raise ValueError("Le consentement du client est obligatoire pour ouvrir un dossier.")
        return self


class ClientUpdate(BaseModel):
    """Rectification request: only the fields sent are changed."""

    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    notes: str | None = None


class ClientOut(ORM, ClientFields):
    id: int
    uuid: str
    created_at: datetime
    consent_service_at: datetime | None
    consent_policy_version: str | None
    consent_marketing: bool | None
    anonymized_at: datetime | None
    consent_valid: bool = False  # consent given for the current notice version


class ConsentIn(BaseModel):
    kind: Literal["service", "marketing"]
    granted: bool
    policy_version: str
    method: ConsentMethod = "touch"


class AnonymizeIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    confirm: Literal[True]


class PrivacyNoticeOut(BaseModel):
    version: str
    lang: str
    notice: str
    consent_service: str
    consent_marketing: str
    retention_years: int
    responsible_configured: bool


OrderChannel = Literal["counter", "store", "mail"]
OrderStatus = Literal["received", "in_progress", "graded", "returned", "cancelled"]


class OrderIn(BaseModel):
    client_id: int
    channel: OrderChannel
    notes: str | None = None
    tracking_in: str | None = Field(default=None, max_length=100)
    return_address: str | None = Field(default=None, max_length=500)


class OrderOut(ORM):
    id: int
    uuid: str
    number: str
    client_id: int
    client_name: str
    channel: OrderChannel
    status: OrderStatus
    notes: str | None
    tracking_in: str | None
    tracking_out: str | None
    return_address: str | None
    created_at: datetime
    graded_at: datetime | None
    returned_at: datetime | None
    cards_count: int
    graded_count: int


class OrderCardOut(BaseModel):
    id: int
    name: str
    set_name: str | None
    card_number: str | None
    card_type: str
    session_id: int | None  # latest grading session
    session_state: str | None
    final_grade: int | None


class OrderDetailOut(OrderOut):
    cards: list[OrderCardOut]


class ReturnIn(BaseModel):
    tracking_out: str | None = Field(default=None, max_length=100)


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
    order_id: int | None = None
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
    certificate_number: str | None
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


# -- accounts --------------------------------------------------------------------------------

UserRole = Literal["operator", "admin"]


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=200)


class SetupIn(BaseModel):
    username: str = Field(min_length=3, max_length=30)
    password: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=100)


class UserCreateIn(SetupIn):
    role: UserRole = "operator"


class UserUpdateIn(BaseModel):
    role: UserRole | None = None
    active: bool | None = None
    display_name: str | None = Field(default=None, max_length=100)


class PasswordResetIn(BaseModel):
    new_password: str = Field(min_length=1, max_length=200)


class PasswordChangeIn(PasswordResetIn):
    current_password: str = Field(min_length=1, max_length=200)


class UserOut(ORM):
    id: int
    username: str
    display_name: str | None
    role: UserRole
    active: bool
    created_at: datetime
    last_login_at: datetime | None


class AuditOut(ORM):
    id: int
    at: datetime
    username: str | None
    action: str
    target: str | None
    details: dict | None
    ip: str | None
