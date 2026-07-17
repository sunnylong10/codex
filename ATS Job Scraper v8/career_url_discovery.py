"""Shared career URL candidate normalization, scoring, ranking, and caching."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "source",
    "src",
    "srsltid",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}
GENERIC_NAME_TOKENS = {
    "company",
    "corporation",
    "global",
    "group",
    "holdings",
    "international",
    "limited",
    "technology",
    "technologies",
}
API_VERIFIED_ATS = {"greenhouse", "lever", "smartrecruiters", "workday", "oracle"}
COUNTRY_URL_MARKERS = {
    "australia": (".au", "/au/", "australia"),
    "canada": (".ca", "/ca/", "canada"),
    "china": (".cn", "/cn/", "china"),
    "germany": (".de", "/de/", "germany"),
    "hong kong": (".hk", "/hk/", "hong-kong"),
    "india": (".in", "/in/", "india"),
    "japan": (".jp", "/jp/", "japan"),
    "singapore": (".sg", "/sg/", "singapore"),
    "united kingdom": (".uk", "/uk/", "united-kingdom"),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_url(url: str) -> str:
    value = url.strip()
    if not value:
        return value
    parts = urlsplit(value)
    scheme = (parts.scheme or "https").casefold()
    host = (parts.hostname or "").casefold()
    port = f":{parts.port}" if parts.port else ""
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.casefold() not in TRACKING_PARAMETERS
        )
    )
    return urlunsplit((scheme, host + port, path, query, ""))


def identity_match(brand: str, verified_name: str) -> tuple[float, str]:
    if not verified_name:
        return 0.35, "identity_unverified"
    brand_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", brand.casefold())
        if token not in GENERIC_NAME_TOKENS
    }
    verified_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", verified_name.casefold())
        if token not in GENERIC_NAME_TOKENS
    }
    if not brand_tokens or not verified_tokens:
        return 0.35, "identity_weak_tokens"
    overlap = len(brand_tokens & verified_tokens) / len(brand_tokens | verified_tokens)
    if overlap == 0:
        return 0.0, "identity_conflict"
    if overlap >= 0.5:
        return 1.0, "identity_strong"
    return 0.65, "identity_partial"


@dataclass
class CareerUrlCandidate:
    brand: str
    url: str
    canonical_url: str
    ats: str
    identifier: str
    verified_name: str
    jobs: int
    sg_jobs: int
    country: str
    language: str
    source: str
    ats_confidence: float
    ats_verification_status: str
    ats_evidence: str
    ats_alternatives: str
    confidence_score: float
    confidence_level: str
    validation_status: str
    reasons: str
    is_primary: int = 0
    needs_review: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def score_candidate(
    hit: dict, brand: str, country: str = "", language: str = ""
) -> CareerUrlCandidate:
    ats = str(hit.get("ats") or "unknown")
    ats_family = ats.split()[0].casefold()
    verified_name = str(hit.get("verified_name") or "")
    identity_score, identity_reason = identity_match(brand, verified_name)
    jobs = int(hit.get("jobs", -1))
    sg_jobs = int(hit.get("sg_jobs", -1))
    source = str(hit.get("source") or "ats_probe")
    detected_confidence = float(hit.get("ats_confidence", 0.0) or 0.0)
    api_score = 1.0 if ats_family in API_VERIFIED_ATS and jobs >= 0 else 0.55
    api_score = max(api_score, detected_confidence)
    career_score = 1.0 if jobs > 0 else 0.8 if jobs == 0 else 0.55
    authority_score = {
        "official_homepage": 1.0,
        "ats_probe": 0.8,
        "homepage_sniff": 0.85,
        "search": 0.55,
        "ai": 0.4,
    }.get(source, 0.5)
    url_blob = normalize_url(str(hit.get("url") or "")).casefold()
    scope_score = 0.5
    country_markers = COUNTRY_URL_MARKERS.get(
        country.casefold(), (country.casefold(),) if country else ()
    )
    if country and any(marker in url_blob for marker in country_markers):
        scope_score = 1.0
    elif country:
        scope_score = 0.65
    score = (
        identity_score * 0.30
        + authority_score * 0.25
        + career_score * 0.20
        + api_score * 0.15
        + 0.05
        + scope_score * 0.05
    )
    reasons = [identity_reason, f"source_{source}"]
    if jobs > 0:
        reasons.append("active_jobs")
    elif jobs == 0:
        reasons.append("valid_empty")
    else:
        reasons.append("jobs_unverified")
    if identity_reason == "identity_conflict":
        status = "rejected"
        review = 1
    elif score >= 0.85:
        status = "validated"
        review = 0
    elif score >= 0.65:
        status = "needs_review"
        review = 1
    else:
        status = "unverified"
        review = 1
    level = "high" if score >= 0.85 else "medium" if score >= 0.65 else "low"
    return CareerUrlCandidate(
        brand=brand,
        url=str(hit.get("url") or ""),
        canonical_url=normalize_url(str(hit.get("url") or "")),
        ats=ats,
        identifier=str(hit.get("identifier") or ""),
        verified_name=verified_name,
        jobs=jobs,
        sg_jobs=sg_jobs,
        country=country,
        language=language,
        source=source,
        ats_confidence=round(detected_confidence, 4),
        ats_verification_status=str(hit.get("ats_verification_status") or ""),
        ats_evidence=str(hit.get("ats_evidence") or ""),
        ats_alternatives=str(hit.get("ats_alternatives") or ""),
        confidence_score=round(score, 4),
        confidence_level=level,
        validation_status=status,
        reasons=",".join(reasons),
        needs_review=review,
    )


def rank_candidates(
    hits: list[dict], brand: str, country: str = "", language: str = ""
) -> list[CareerUrlCandidate]:
    deduplicated: dict[str, CareerUrlCandidate] = {}
    for hit in hits:
        candidate = score_candidate(hit, brand, country, language)
        if not candidate.canonical_url:
            continue
        existing = deduplicated.get(candidate.canonical_url)
        if existing is None or candidate.confidence_score > existing.confidence_score:
            deduplicated[candidate.canonical_url] = candidate
    ranked = sorted(
        deduplicated.values(),
        key=lambda candidate: (
            candidate.validation_status != "validated",
            -candidate.confidence_score,
            -candidate.jobs,
            candidate.canonical_url,
        ),
    )
    if ranked:
        ranked[0].is_primary = 1
    return ranked


class DiscoveryCache:
    """Small SQLite cache with separate success and transient-failure TTLs."""

    def __init__(self, path: str | Path = "career_url_cache.db"):
        self.path = Path(path)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS discovery_cache (
                       cache_key TEXT PRIMARY KEY,
                       payload_json TEXT NOT NULL,
                       outcome TEXT NOT NULL,
                       expires_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL
                )"""
            )
            db.commit()

    @staticmethod
    def key(brand: str, country: str, language: str, strategies: list[str]) -> str:
        raw = json.dumps(
            [brand.casefold().strip(), country.casefold(), language.casefold(), strategies],
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key: str) -> list[dict] | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT payload_json, expires_at FROM discovery_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if row is None or datetime.fromisoformat(row[1]) <= utc_now():
            return None
        return json.loads(row[0])

    def put(
        self,
        key: str,
        candidates: list[dict],
        outcome: str = "success",
        success_ttl_days: int = 14,
    ) -> None:
        ttl = timedelta(days=success_ttl_days)
        if outcome == "transient_error":
            ttl = timedelta(hours=1)
        elif outcome == "not_found":
            ttl = timedelta(days=3)
        now = utc_now()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                """INSERT INTO discovery_cache
                       (cache_key, payload_json, outcome, expires_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       payload_json = excluded.payload_json,
                       outcome = excluded.outcome,
                       expires_at = excluded.expires_at,
                       updated_at = excluded.updated_at""",
                (
                    key,
                    json.dumps(candidates),
                    outcome,
                    (now + ttl).isoformat(),
                    now.isoformat(),
                ),
            )
            db.commit()
