from __future__ import annotations

from .client import VerificationClient
from .detectors import CustomDetector, DEFAULT_DETECTORS, UnknownPlugin
from .models import DetectionContext, DetectionResult


class DetectorRegistry:
    def __init__(self, detectors=None, custom_detector=None, unknown_detector=None):
        self.detectors = list(detectors or DEFAULT_DETECTORS)
        self.custom_detector = custom_detector or CustomDetector()
        self.unknown_detector = unknown_detector or UnknownPlugin()

    def detect(self, context: DetectionContext) -> list[DetectionResult]:
        results = []
        for detector in self.detectors:
            results.extend(detector.detect(context))
        results = self._deduplicate(results)
        if not results:
            results = self.custom_detector.detect(context)
        if not results:
            results = self.unknown_detector.detect(context)
        return self._resolve(results)

    def verify(
        self,
        results: list[DetectionResult],
        context: DetectionContext,
        client: VerificationClient,
    ) -> list[DetectionResult]:
        plugins = {detector.vendor_id: detector for detector in self.detectors}
        plugins[self.custom_detector.vendor_id] = self.custom_detector
        plugins[self.unknown_detector.vendor_id] = self.unknown_detector
        verified = [
            plugins[result.vendor].verify(result, context, client)
            for result in results
        ]
        return self._resolve(verified)

    @staticmethod
    def _resolve(results):
        for result in results:
            result.is_primary = False
            result.conflicting = False
        results.sort(key=lambda result: -result.confidence_score)
        results[0].is_primary = True
        if len({result.vendor for result in results if result.confidence_score >= 0.7}) > 1:
            for result in results:
                result.conflicting = True
        return results

    @staticmethod
    def _deduplicate(results):
        deduplicated = {}
        for result in results:
            key = (result.vendor, result.canonical_url)
            existing = deduplicated.get(key)
            if existing is None:
                deduplicated[key] = result
            else:
                existing.confidence_score = max(
                    existing.confidence_score, result.confidence_score
                )
                existing.evidence.extend(result.evidence)
        return list(deduplicated.values())


default_registry = DetectorRegistry()
