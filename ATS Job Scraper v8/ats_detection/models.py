from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class DetectionEvidence:
    evidence_type: str
    value: str
    source_url: str
    confidence: float
    verified: bool = False


@dataclass
class DetectionContext:
    original_url: str
    final_url: str = ""
    redirect_chain: tuple[str, ...] = ()
    html: str = ""
    network_urls: tuple[str, ...] = ()
    company_name: str = ""
    country: str = ""

    def url_sources(self) -> list[tuple[str, str]]:
        sources = [("original_url", self.original_url)]
        sources.extend(("redirect", url) for url in self.redirect_chain)
        if self.final_url:
            sources.append(("final_url", self.final_url))
        sources.extend(("network", url) for url in self.network_urls)
        return [(kind, url) for kind, url in sources if url]


@dataclass
class DetectionResult:
    vendor: str
    confidence_score: float
    verification_status: str
    canonical_url: str
    tenant_id: str = ""
    board_id: str = ""
    detector_version: str = "1"
    evidence: list[DetectionEvidence] = field(default_factory=list)
    is_primary: bool = False
    conflicting: bool = False
    error_code: str = ""
    retryable: bool = False

    def to_dict(self) -> dict:
        return asdict(self)
