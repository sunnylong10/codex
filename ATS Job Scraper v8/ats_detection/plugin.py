from __future__ import annotations

from typing import Protocol

from .client import VerificationClient
from .models import DetectionContext, DetectionResult


class ATSDetector(Protocol):
    """Detects one ATS family and returns evidence-backed possibilities."""

    vendor_id: str
    detector_version: str

    def detect(self, context: DetectionContext) -> list[DetectionResult]: ...

    def verify(
        self,
        result: DetectionResult,
        context: DetectionContext,
        client: VerificationClient,
    ) -> DetectionResult: ...
