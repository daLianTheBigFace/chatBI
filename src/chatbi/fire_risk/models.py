"""Pydantic contracts for corridor fire-safety assessment."""

from enum import StrEnum

from pydantic import BaseModel, Field


class RiskLevel(StrEnum):
    """Fire-hazard severity, from none to high, plus a non-corridor bucket."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    NOT_CORRIDOR = "not_corridor"


RISK_LEVEL_LABELS: dict[RiskLevel, str] = {
    RiskLevel.NONE: "无风险",
    RiskLevel.LOW: "低风险",
    RiskLevel.MEDIUM: "中风险",
    RiskLevel.HIGH: "高风险",
    RiskLevel.NOT_CORRIDOR: "非楼道",
}


class FireRiskAssessment(BaseModel):
    """Structured verdict returned by the vision model and re-validated here."""

    level: RiskLevel
    reasoning: str = Field(min_length=1)
    hazards: list[str] = Field(default_factory=list)


class FireRiskRequest(BaseModel):
    """Inbound image payload; the frontend sends a base64 data body."""

    image_base64: str = Field(min_length=1)
    mime_type: str = Field(default="image/jpeg")


class FireRiskResponse(BaseModel):
    """Outbound verdict with a display label for the frontend."""

    level: RiskLevel
    level_label: str
    reasoning: str
    hazards: list[str]
