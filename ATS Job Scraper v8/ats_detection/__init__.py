"""Plugin-based ATS detection independent of job extraction."""

from .client import RequestsVerificationClient, VerificationClient, VerificationResponse
from .detectors import (
    GreenhousePlugin,
    LeverPlugin,
    OraclePlugin,
    SmartRecruitersPlugin,
    SuccessFactorsPlugin,
    UnknownPlugin,
    WorkdayPlugin,
)
from .models import DetectionContext, DetectionEvidence, DetectionResult
from .registry import DetectorRegistry, default_registry

__all__ = [
    "DetectionContext",
    "DetectionEvidence",
    "DetectionResult",
    "DetectorRegistry",
    "GreenhousePlugin",
    "LeverPlugin",
    "OraclePlugin",
    "RequestsVerificationClient",
    "SmartRecruitersPlugin",
    "SuccessFactorsPlugin",
    "UnknownPlugin",
    "VerificationClient",
    "VerificationResponse",
    "WorkdayPlugin",
    "default_registry",
]
