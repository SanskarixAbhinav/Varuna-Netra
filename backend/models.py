import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, ConfigDict

ATTRIBUTION_STATUSES = ["indeterminate", "insufficient_evidence", "possible", "probable", "analyst_confirmed"]
SPILL_QUALITY_FLAGS = [
    "cloud_contaminated", "sunglint", "low_wind", "natural_seep_suspect", "conflicting_source",
    "uncertain_age", "partial_coverage", "lookalike_suspect", "experimental_detector",
]
AIS_QUALITY_FLAGS = ["spoof_suspect", "position_jump", "implausible_speed", "naive_timestamp", "stale", "missing_identity", "future_timestamp"]
REASON_CODES = {
    "RC01_TRACK_OVERLAP": "Vessel track intersects spill geometry within plausible age window",
    "RC02_DRIFT_CONSISTENT": "Drift back-projection consistent with vessel position",
    "RC03_AIS_GAP": "AIS dark period overlapping spill window",
    "RC04_NATURAL_SEEP": "Observation consistent with natural seep",
    "RC05_LOOKALIKE_LOW_WIND": "Dark patch consistent with low-wind lookalike",
    "RC06_MULTIPLE_VESSELS": "Multiple vessels with comparable evidence",
    "RC07_INSUFFICIENT_DATA": "Insufficient AIS or environmental data",
    "RC08_EXTERNAL_INTEL": "Corroborated by external intelligence / inspection",
    "RC09_SENSOR_ARTIFACT": "Sensor artifact or processing error",
}


def new_id():
    return str(uuid.uuid4())


def now_utc():
    return datetime.now(timezone.utc)


class GeoJSONGeometry(BaseModel):
    type: str
    coordinates: Any


class SceneCreate(BaseModel):
    provider: str = "sentinel-1"
    provider_scene_id: str = Field(min_length=1)
    sensor_mode: Optional[str] = None
    polarization: Optional[str] = None
    acquisition_time: datetime
    footprint: GeoJSONGeometry
    storage_ref: Optional[str] = None
    metadata: Dict[str, Any] = {}


class WindInput(BaseModel):
    speed_ms: float = Field(ge=0, le=80)
    direction_deg: float = Field(ge=0, lt=360, description="meteorological: direction wind blows FROM")


class CurrentInput(BaseModel):
    speed_ms: float = Field(ge=0, le=10)
    direction_deg: float = Field(ge=0, lt=360, description="oceanographic: direction current flows TOWARD")


class SpillObservationCreate(BaseModel):
    scene_id: Optional[str] = None
    geometry: GeoJSONGeometry
    acquisition_time: datetime
    source: str = "external"
    detection_confidence: float = Field(ge=0, le=1)
    quality_flags: List[str] = []
    estimated_area_km2: Optional[float] = None
    processing_version: str = "external-polygon-1.0"
    estimated_age_hours: Optional[float] = Field(default=None, ge=0)
    wind: Optional[WindInput] = None
    current: Optional[CurrentInput] = None
    notes: Optional[str] = None


class AISPositionIn(BaseModel):
    mmsi: str = Field(min_length=1, max_length=20)
    imo: Optional[str] = None
    vessel_name: Optional[str] = None
    vessel_type: Optional[str] = None
    timestamp: datetime
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    sog_kn: Optional[float] = Field(default=None, ge=0)
    cog_deg: Optional[float] = Field(default=None, ge=0, le=360)
    heading_deg: Optional[float] = Field(default=None, ge=0, le=511)
    source: str = "terrestrial"
    quality_flags: List[str] = []


class AISBatch(BaseModel):
    positions: List[AISPositionIn] = Field(min_length=1, max_length=5000)


DEFAULT_WEIGHTS = {"spatial": 0.30, "temporal": 0.20, "continuity": 0.10, "heading": 0.10, "drift": 0.15, "reliability": 0.15}


class CorrelationParams(BaseModel):
    corridor_km: float = Field(default=25.0, gt=0, le=500)
    window_hours_before: float = Field(default=24.0, gt=0, le=240)
    window_hours_after: float = Field(default=3.0, ge=0, le=48)
    min_positions: int = Field(default=2, ge=1)
    spill_age_hours: Optional[float] = Field(default=None, ge=0)
    wind: Optional[WindInput] = None
    current: Optional[CurrentInput] = None
    use_observation_environment: bool = True
    fill_gaps: bool = True
    gap_threshold_min: float = Field(default=30, ge=5, le=720)
    weights: Dict[str, float] = DEFAULT_WEIGHTS


class CorrelateRequest(BaseModel):
    params: Optional[CorrelationParams] = None
    sync: bool = False
    fetch_environment: bool = False


class ReviewCreate(BaseModel):
    decision: Literal["confirm", "reject", "needs_more_data"]
    vessel_mmsi: Optional[str] = None
    reason_codes: List[str] = []
    notes: str = ""
    result_version: Optional[int] = None


class OverrideRequest(BaseModel):
    attribution_status: Literal["indeterminate", "insufficient_evidence", "possible", "probable", "analyst_confirmed"]
    vessel_mmsi: Optional[str] = None
    notes: str = Field(min_length=3)
    close_case: bool = True


class LoginRequest(BaseModel):
    email: str
    password: str


class UserCreate(BaseModel):
    email: str
    name: str
    role: str
    password: str = Field(min_length=8)


class UserUpdate(BaseModel):
    role: Optional[str] = None
    active: Optional[bool] = None
    name: Optional[str] = None
    notify_alerts: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=8)


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=16)
    new_password: str = Field(min_length=8)


class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str
    password: str = Field(min_length=10, max_length=128)
    organization: Optional[str] = Field(default=None, max_length=200)


class RoleRequestCreate(BaseModel):
    requested_role: Literal["analyst", "supervisor"]
    organization: Optional[str] = Field(default=None, max_length=200)
    reason: Optional[str] = Field(default=None, max_length=1000)


class CaseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = None
