"""Corridor fire-safety assessment backed by the DeepSeek vision model."""

from chatbi.fire_risk.bootstrap import open_fire_risk_service
from chatbi.fire_risk.models import (
    FireRiskAssessment,
    FireRiskRequest,
    FireRiskResponse,
    RiskLevel,
)
from chatbi.fire_risk.service import FireRiskError, FireRiskService

__all__ = [
    "FireRiskAssessment",
    "FireRiskError",
    "FireRiskRequest",
    "FireRiskResponse",
    "FireRiskService",
    "RiskLevel",
    "open_fire_risk_service",
]
