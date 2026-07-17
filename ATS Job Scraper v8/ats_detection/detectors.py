from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .client import VerificationResponse
from .models import DetectionContext, DetectionEvidence, DetectionResult


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        ((parts.scheme or "https").casefold(), parts.netloc.casefold(), path, parts.query, "")
    )


@dataclass(frozen=True)
class PatternDetector:
    vendor_id: str
    pattern: re.Pattern
    tenant_group: str = "tenant"
    board_group: str = "board"
    detector_version: str = "1"

    def detect(self, context: DetectionContext) -> list[DetectionResult]:
        results = []
        seen = set()
        for source_type, source_url in context.url_sources():
            results.extend(self._matches(source_url, source_type, source_url, seen))
        if context.html:
            results.extend(
                self._matches(context.html, "html_fingerprint", context.final_url, seen)
            )
        return results

    def _matches(self, value, evidence_type, source_url, seen):
        results = []
        for match in self.pattern.finditer(value):
            matched_url = match.group(0).rstrip("'\"<>),")
            normalized = canonical_url(matched_url)
            if normalized in seen:
                continue
            seen.add(normalized)
            groups = match.groupdict()
            tenant = groups.get(self.tenant_group, "")
            board = groups.get(self.board_group, "")
            confidence = 0.9 if evidence_type in {"final_url", "network"} else 0.82
            if evidence_type == "html_fingerprint":
                confidence = 0.72
            results.append(
                DetectionResult(
                    vendor=self.vendor_id,
                    confidence_score=confidence,
                    verification_status="inferred",
                    canonical_url=normalized,
                    tenant_id=tenant,
                    board_id=board,
                    detector_version=self.detector_version,
                    evidence=[
                        DetectionEvidence(
                            evidence_type=evidence_type,
                            value=matched_url,
                            source_url=source_url or "",
                            confidence=confidence,
                        )
                    ],
                )
            )
        return results

    def verify(self, result, context, client):
        return result


class SAPDetector(PatternDetector):
    def detect(self, context):
        results = super().detect(context)
        for result in results:
            query = parse_qs(urlsplit(result.canonical_url).query)
            result.tenant_id = (query.get("company") or [""])[0]
            if result.tenant_id:
                result.confidence_score = max(result.confidence_score, 0.88)
        return results


class CustomDetector:
    vendor_id = "custom"
    detector_version = "1"
    JOB_SIGNALS = (
        "application/ld+json",
        "jobposting",
        "job-search",
        "search jobs",
        "current openings",
        "vacancies",
        "apply now",
    )

    def detect(self, context):
        html = context.html.casefold()
        signals = [signal for signal in self.JOB_SIGNALS if signal in html]
        if len(signals) < 2:
            return []
        url = context.final_url or context.original_url
        score = min(0.75, 0.5 + len(signals) * 0.05)
        return [
            DetectionResult(
                vendor="custom",
                confidence_score=score,
                verification_status="inferred",
                canonical_url=canonical_url(url),
                detector_version=self.detector_version,
                evidence=[
                    DetectionEvidence(
                        evidence_type="career_page_behavior",
                        value=",".join(signals),
                        source_url=url,
                        confidence=score,
                    )
                ],
            )
        ]

    def verify(self, result, context, client):
        return result


class UnknownPlugin:
    vendor_id = "unknown"
    detector_version = "1"

    def detect(self, context):
        url = context.final_url or context.original_url
        return [
            DetectionResult(
                vendor="unknown",
                confidence_score=0.0,
                verification_status="unverified",
                canonical_url=url,
                detector_version=self.detector_version,
                evidence=[
                    DetectionEvidence(
                        evidence_type="no_known_fingerprint",
                        value="",
                        source_url=url,
                        confidence=0.0,
                    )
                ],
            )
        ]

    def verify(self, result, context, client):
        return result


def _apply_verification(result, response: VerificationResponse):
    result.error_code = response.error_code
    result.retryable = response.retryable
    if response.status_code == 200:
        result.confidence_score = 1.0
        result.verification_status = "verified"
        result.evidence.append(
            DetectionEvidence(
                evidence_type="api_verified",
                value=result.vendor,
                source_url=result.canonical_url,
                confidence=1.0,
                verified=True,
            )
        )
    elif response.retryable:
        result.verification_status = "temporarily_unavailable"
    elif response.status_code in {401, 403}:
        result.verification_status = "access_denied"
    elif response.status_code is not None:
        result.verification_status = "rejected"
    return result


class WorkdayPlugin(PatternDetector):
    def __init__(self):
        super().__init__(
            "workday",
            re.compile(
                r"https://(?P<tenant>[^./\s]+)\.(?:wd\d+)\.myworkdayjobs\.com/"
                r"(?:[a-z]{2}-[A-Z]{2}/)?(?P<board>[^/?#\s'\"<>]+)",
                re.I,
            ),
        )

    def verify(self, result, context, client):
        parts = urlsplit(result.canonical_url)
        endpoint = (
            f"{parts.scheme}://{parts.netloc}/wday/cxs/"
            f"{result.tenant_id}/{result.board_id}/jobs"
        )
        response = client.request(
            "POST",
            endpoint,
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
        )
        return _apply_verification(result, response)


class GreenhousePlugin(PatternDetector):
    def __init__(self):
        super().__init__(
            "greenhouse",
            re.compile(
                r"https://(?:boards|job-boards)\.greenhouse\.io/"
                r"(?P<tenant>[A-Za-z0-9_-]+)",
                re.I,
            ),
        )

    def verify(self, result, context, client):
        response = client.request(
            "GET", f"https://boards-api.greenhouse.io/v1/boards/{result.tenant_id}"
        )
        return _apply_verification(result, response)


class LeverPlugin(PatternDetector):
    def __init__(self):
        super().__init__(
            "lever",
            re.compile(r"https://jobs\.lever\.co/(?P<tenant>[A-Za-z0-9_-]+)", re.I),
        )

    def verify(self, result, context, client):
        response = client.request(
            "GET", f"https://api.lever.co/v0/postings/{result.tenant_id}?mode=json"
        )
        return _apply_verification(result, response)


class SmartRecruitersPlugin(PatternDetector):
    def __init__(self):
        super().__init__(
            "smartrecruiters",
            re.compile(
                r"https://careers\.smartrecruiters\.com/"
                r"(?P<tenant>[A-Za-z0-9_-]+)",
                re.I,
            ),
        )

    def verify(self, result, context, client):
        response = client.request(
            "GET",
            f"https://api.smartrecruiters.com/v1/companies/"
            f"{result.tenant_id}/postings",
            params={"limit": 1},
        )
        return _apply_verification(result, response)


class OraclePlugin(PatternDetector):
    def __init__(self):
        super().__init__(
            "oracle",
            re.compile(
                r"https://(?P<tenant>[^/\s'\"<>]+\.oraclecloud\.com)/hcmUI/"
                r"CandidateExperience/[a-zA-Z-]+/sites/"
                r"(?P<board>[^/?#\s'\"<>]+)",
                re.I,
            ),
        )

    def verify(self, result, context, client):
        response = client.request(
            "GET",
            f"https://{result.tenant_id}/hcmRestApi/resources/latest/"
            "recruitingCEJobRequisitions",
            params={
                "onlyData": "true",
                "finder": f"findReqs;siteNumber={result.board_id},limit=1,offset=0",
            },
        )
        return _apply_verification(result, response)


class SuccessFactorsPlugin(SAPDetector):
    def __init__(self):
        super().__init__(
            "sap_successfactors",
            re.compile(
                r"https://(?:career|jobs)[a-z0-9]*\."
                r"(?:successfactors|sapsf)\.[a-z.]{2,8}/"
                r"[^\s'\"<>]*company=[^\s'\"<>&]+[^\s'\"<>]*",
                re.I,
            ),
        )

    def verify(self, result, context, client):
        response = client.request("GET", result.canonical_url)
        return _apply_verification(result, response)


WORKDAY = WorkdayPlugin()
GREENHOUSE = GreenhousePlugin()
LEVER = LeverPlugin()
SMARTRECRUITERS = SmartRecruitersPlugin()
ORACLE = OraclePlugin()
SAP = SuccessFactorsPlugin()


DEFAULT_DETECTORS = [WORKDAY, SAP, GREENHOUSE, LEVER, ORACLE, SMARTRECRUITERS]
