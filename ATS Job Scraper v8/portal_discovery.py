"""Evidence-driven company career portal discovery.

Search and crawling generate candidates. Independent evidence resolves company
identity, recruitment behavior, ATS vendor, geographic scope, and promotion.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections import defaultdict, deque
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from ats_detection import DetectionContext, default_registry
from career_url_discovery import normalize_url
from discovery_store import stable_company_id


BLACKLISTED_DOMAINS = {
    "glassdoor.com",
    "indeed.com",
    "jobstreet.com",
    "linkedin.com",
    "monster.com",
    "mycareersfuture.gov.sg",
    "ziprecruiter.com",
}
BLACKLISTED_DOMAIN_MARKERS = (
    "fastjobs.",
    "foundit.",
    "glassdoor.",
    "indeed.",
    "instagram.",
    "jobstreet.",
    "jobsdb.",
    "jooble.",
    "linkedin.",
    "ziprecruiter.",
)
CAREER_TERMS = (
    "career",
    "careers",
    "current openings",
    "job search",
    "jobs",
    "join our team",
    "join us",
    "open positions",
    "vacancies",
    "work with us",
)
GENERIC_IDENTITY_TERMS = {
    "company",
    "corporation",
    "global",
    "group",
    "holdings",
    "international",
    "limited",
    "technologies",
}
VALIDATOR_VERSION = "2"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def registered_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").casefold()
    return host.removeprefix("www.")


def token_set(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if token not in GENERIC_IDENTITY_TERMS and len(token) > 1
    }


@dataclass(frozen=True)
class CompanyDiscoveryRequest:
    company_name: str
    company_id: str = ""
    aliases: tuple[str, ...] = ()
    official_domains: tuple[str, ...] = ()
    country: str = ""
    language: str = ""

    def __post_init__(self):
        if not self.company_id:
            object.__setattr__(self, "company_id", stable_company_id(self.company_name))


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str = ""
    snippet: str = ""
    rank: int = 0
    query: str = ""
    provider: str = ""


@dataclass
class PortalCandidate:
    candidate_id: str
    company_id: str
    company_name: str
    url: str
    canonical_url: str
    country: str
    language: str
    sources: set[str] = field(default_factory=set)
    source_details: list[dict] = field(default_factory=list)
    content_text: str = ""
    final_url: str = ""
    redirect_chain: list[str] = field(default_factory=list)
    portal_type: str = "unknown"
    identity_score: float = 0.0
    recruitment_score: float = 0.0
    authority_score: float = 0.0
    scope_score: float = 0.0
    ats_vendor: str = "unknown"
    ats_confidence: float = 0.0
    confidence_score: float = 0.0
    validation_status: str = "discovered"
    rejection_reason: str = ""
    promoted: bool = False

    def to_row(self) -> dict:
        row = asdict(self)
        row["sources"] = ",".join(sorted(self.sources))
        row["source_details"] = json.dumps(self.source_details, sort_keys=True)
        row["redirect_chain"] = json.dumps(self.redirect_chain)
        row.pop("content_text", None)
        return row


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int | None
    text: str
    redirect_chain: tuple[str, ...] = ()
    error_code: str = ""


class SearchProvider(Protocol):
    provider_id: str

    def search(self, query: str, limit: int = 10) -> list[SearchResult]: ...


class Fetcher(Protocol):
    def fetch(self, url: str) -> FetchResult: ...


class SerperSearchProvider:
    provider_id = "serper"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("SERPER_API_KEY", "")
        if not self.api_key:
            raise ValueError("SERPER_API_KEY is not set")
        self.session = requests.Session()

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        response = self.session.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            json={"q": query, "num": limit},
            timeout=30,
        )
        response.raise_for_status()
        return [
            SearchResult(
                url=item.get("link", ""),
                title=item.get("title", ""),
                snippet=item.get("snippet", ""),
                rank=index,
                query=query,
                provider=self.provider_id,
            )
            for index, item in enumerate(response.json().get("organic", []), 1)
            if item.get("link")
        ]


class RequestsFetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0 CareerPortalDiscovery/1.0"

    def fetch(self, url: str) -> FetchResult:
        try:
            response = self.session.get(url, timeout=(5, 12), allow_redirects=True)
        except requests.Timeout:
            return FetchResult(url, "", None, "", error_code="timeout")
        except requests.RequestException:
            return FetchResult(url, "", None, "", error_code="network_error")
        redirects = tuple(item.url for item in response.history)
        return FetchResult(
            url,
            response.url,
            response.status_code,
            response.text[:1_000_000],
            redirects,
        )


class EvidenceLedger:
    def __init__(self, path: str | Path = "portal_discovery.db"):
        self.path = Path(path)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS discovery_candidate (
                    candidate_id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    validation_status TEXT NOT NULL,
                    confidence_score REAL NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(company_id, canonical_url)
                );
                CREATE TABLE IF NOT EXISTS candidate_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    evidence_type TEXT NOT NULL,
                    value TEXT NOT NULL,
                    source TEXT NOT NULL,
                    score REAL NOT NULL,
                    observed_at TEXT NOT NULL,
                    UNIQUE(candidate_id, evidence_type, value, source)
                );
                CREATE TABLE IF NOT EXISTS negative_match (
                    company_id TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    validator_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(company_id, canonical_url, reason)
                );
                CREATE TABLE IF NOT EXISTS portal_selection (
                    selection_id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    country TEXT NOT NULL,
                    language TEXT NOT NULL,
                    portal_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    selected_at TEXT NOT NULL,
                    UNIQUE(company_id, country, language, portal_type, candidate_id)
                );
                CREATE TABLE IF NOT EXISTS search_cache (
                    cache_key TEXT PRIMARY KEY,
                    results_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return closing(connection)

    def is_rejected(self, company_id: str, url: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                """SELECT 1 FROM negative_match
                   WHERE company_id = ? AND canonical_url = ? AND validator_version = ?""",
                (company_id, normalize_url(url), VALIDATOR_VERSION),
            ).fetchone()
        return row is not None

    def save_candidate(self, candidate: PortalCandidate) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO discovery_candidate
                       (candidate_id, company_id, company_name, canonical_url,
                        payload_json, validation_status, confidence_score, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(company_id, canonical_url) DO UPDATE SET
                       payload_json = excluded.payload_json,
                       validation_status = excluded.validation_status,
                       confidence_score = excluded.confidence_score,
                       updated_at = excluded.updated_at""",
                (
                    candidate.candidate_id,
                    candidate.company_id,
                    candidate.company_name,
                    candidate.canonical_url,
                    json.dumps(candidate.to_row(), sort_keys=True),
                    candidate.validation_status,
                    candidate.confidence_score,
                    utc_now(),
                ),
            )
            db.commit()

    def add_evidence(
        self, candidate_id: str, evidence_type: str, value: str, source: str, score: float
    ) -> None:
        evidence_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "|".join((candidate_id, evidence_type, value, source)),
            )
        )
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO candidate_evidence
                       (evidence_id, candidate_id, evidence_type, value, source,
                        score, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (evidence_id, candidate_id, evidence_type, value, source, score, utc_now()),
            )
            db.commit()

    def reject(self, candidate: PortalCandidate, reason: str) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO negative_match
                       (company_id, canonical_url, reason, validator_version, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(company_id, canonical_url, reason) DO UPDATE SET
                       validator_version = excluded.validator_version,
                       created_at = excluded.created_at""",
                (
                    candidate.company_id,
                    candidate.canonical_url,
                    reason,
                    VALIDATOR_VERSION,
                    utc_now(),
                ),
            )
            db.commit()

    def promote(self, candidate: PortalCandidate, portal_type: str = "careers") -> None:
        selection_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO portal_selection
                       (selection_id, company_id, candidate_id, country, language,
                        portal_type, status, selected_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?)""",
                (
                    selection_id,
                    candidate.company_id,
                    candidate.candidate_id,
                    candidate.country,
                    candidate.language,
                    portal_type,
                    utc_now(),
                ),
            )
            db.commit()

    def cached_search(self, key: str) -> list[SearchResult] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT results_json, expires_at FROM search_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if row is None or datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            return None
        return [SearchResult(**item) for item in json.loads(row["results_json"])]

    def cache_search(self, key: str, results: list[SearchResult]) -> None:
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        with self.connect() as db:
            db.execute(
                """INSERT INTO search_cache(cache_key, results_json, expires_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       results_json = excluded.results_json,
                       expires_at = excluded.expires_at""",
                (key, json.dumps([asdict(item) for item in results]), expires),
            )
            db.commit()


class PortalDiscoveryService:
    def __init__(
        self,
        ledger: EvidenceLedger,
        fetcher: Fetcher,
        search_provider: SearchProvider | None = None,
        max_official_pages: int = 5,
    ):
        self.ledger = ledger
        self.fetcher = fetcher
        self.search_provider = search_provider
        self.max_official_pages = max_official_pages

    def discover(self, request: CompanyDiscoveryRequest) -> list[PortalCandidate]:
        candidates: dict[str, PortalCandidate] = {}
        for result in self._crawl_official_domains(request):
            self._merge_candidate(candidates, request, result.url, "official_crawl", result)
        for result in self._search(request):
            self._merge_candidate(candidates, request, result.url, "search", result)
        validated = []
        for candidate in candidates.values():
            if self.ledger.is_rejected(request.company_id, candidate.canonical_url):
                continue
            self._validate(request, candidate)
            self.ledger.save_candidate(candidate)
            if candidate.validation_status == "rejected":
                self.ledger.reject(candidate, candidate.rejection_reason)
            validated.append(candidate)
        promotable = [item for item in validated if self._should_promote(item)]
        if promotable:
            selected = max(promotable, key=self._selection_score)
            selected.promoted = True
            selected.validation_status = "promoted"
            self.ledger.promote(selected, selected.portal_type)
            self.ledger.save_candidate(selected)
        return sorted(validated, key=lambda item: -item.confidence_score)

    def _crawl_official_domains(
        self, request: CompanyDiscoveryRequest
    ) -> list[SearchResult]:
        discovered = []
        for domain in request.official_domains:
            root = domain if domain.startswith("http") else f"https://{domain}"
            trusted_host = registered_domain(root)
            queue = deque([root, urljoin(root, "/sitemap.xml")])
            visited = set()
            while queue and len(visited) < self.max_official_pages:
                url = queue.popleft()
                normalized = normalize_url(url)
                if normalized in visited:
                    continue
                visited.add(normalized)
                response = self.fetcher.fetch(url)
                if response.status_code != 200 or not response.text:
                    continue
                if urlsplit(response.final_url or url).path.casefold().endswith(".xml"):
                    soup = BeautifulSoup(response.text, "xml")
                    for location in soup.find_all("loc"):
                        href = location.get_text(strip=True)
                        if any(term in href.casefold() for term in CAREER_TERMS):
                            discovered.append(
                                SearchResult(
                                    href,
                                    snippet="official sitemap",
                                    provider="official_crawl",
                                )
                            )
                    continue
                soup = BeautifulSoup(response.text, "html.parser")
                for link in soup.find_all("a", href=True):
                    href = urljoin(response.final_url or url, link["href"])
                    label = link.get_text(" ", strip=True)
                    blob = f"{href} {label}".casefold()
                    if any(term in blob for term in CAREER_TERMS):
                        discovered.append(
                            SearchResult(
                                href,
                                title=label,
                                snippet="official-site link",
                                provider="official_crawl",
                            )
                        )
                        if registered_domain(href) == trusted_host:
                            queue.append(href)
                text = soup.get_text(" ", strip=True)
                if any(term in text.casefold() for term in CAREER_TERMS):
                    discovered.append(
                        SearchResult(
                            response.final_url or url,
                            title=soup.title.get_text(" ", strip=True) if soup.title else "",
                            snippet="official-site career content",
                            provider="official_crawl",
                        )
                    )
        return discovered

    def _search(self, request: CompanyDiscoveryRequest) -> list[SearchResult]:
        if self.search_provider is None:
            return []
        names = (request.company_name, *request.aliases)
        queries = []
        for name in names:
            queries.append(" ".join(part for part in (f'"{name}"', request.country, "careers") if part))
        for domain in request.official_domains:
            queries.append(f"site:{registered_domain(domain)} careers jobs")
        results = []
        for query in dict.fromkeys(queries):
            cache_key = hashlib.sha256(
                f"{self.search_provider.provider_id}|{query}".encode()
            ).hexdigest()
            cached = self.ledger.cached_search(cache_key)
            found = cached if cached is not None else self.search_provider.search(query)
            if cached is None:
                self.ledger.cache_search(cache_key, found)
            results.extend(found)
        return results

    def _merge_candidate(self, candidates, request, url, source, detail):
        if not url or self._blacklisted(url):
            return
        canonical = normalize_url(url)
        candidate = candidates.get(canonical)
        if candidate is None:
            candidate = PortalCandidate(
                candidate_id=str(uuid.uuid5(uuid.UUID(request.company_id), canonical)),
                company_id=request.company_id,
                company_name=request.company_name,
                url=url,
                canonical_url=canonical,
                country=request.country,
                language=request.language,
            )
            candidates[canonical] = candidate
        candidate.sources.add(source)
        candidate.source_details.append(asdict(detail))

    def _validate(self, request, candidate):
        response = self.fetcher.fetch(candidate.url)
        candidate.final_url = response.final_url
        candidate.redirect_chain = list(response.redirect_chain)
        if response.status_code != 200 or not response.text:
            candidate.validation_status = "retryable_failure" if response.error_code else "rejected"
            candidate.rejection_reason = response.error_code or f"http_{response.status_code}"
            return
        soup = BeautifulSoup(response.text, "html.parser")
        candidate.content_text = soup.get_text(" ", strip=True)[:100_000]
        identity_blob = " ".join(
            [candidate.content_text[:20_000]]
            + [str(item.get("title", "")) + " " + str(item.get("snippet", ""))
               for item in candidate.source_details]
        )
        expected = token_set(" ".join((request.company_name, *request.aliases)))
        observed = token_set(identity_blob)
        overlap = len(expected & observed) / max(1, len(expected))
        candidate.identity_score = min(1.0, overlap)
        career_hits = sum(term in candidate.content_text.casefold() for term in CAREER_TERMS)
        candidate.recruitment_score = min(1.0, career_hits / 3)
        final_domain = registered_domain(response.final_url or candidate.url)
        official_domains = {registered_domain(item) for item in request.official_domains}
        official_source = "official_crawl" in candidate.sources
        exact_official = final_domain in official_domains
        career_subdomain = any(
            final_domain.startswith((f"careers.{domain}", f"career.{domain}", f"jobs.{domain}"))
            for domain in official_domains
        )
        related_subdomain = any(
            final_domain.endswith(f".{domain}") for domain in official_domains
        )
        if exact_official:
            candidate.authority_score = 1.0
        elif career_subdomain:
            candidate.authority_score = 0.98
        elif official_source:
            candidate.authority_score = 0.9
        elif related_subdomain:
            candidate.authority_score = 0.85
        else:
            candidate.authority_score = 0.35
        scope_blob = f"{candidate.canonical_url} {candidate.content_text[:20_000]}".casefold()
        candidate.scope_score = 1.0 if not request.country or request.country.casefold() in scope_blob else 0.5
        detections = default_registry.detect(
            DetectionContext(
                original_url=candidate.url,
                final_url=response.final_url,
                redirect_chain=response.redirect_chain,
                html=response.text,
                company_name=request.company_name,
                country=request.country,
            )
        )
        candidate.ats_vendor = detections[0].vendor
        candidate.ats_confidence = detections[0].confidence_score
        candidate.portal_type = self._portal_type(candidate)
        candidate.confidence_score = round(
            candidate.identity_score * 0.35
            + candidate.recruitment_score * 0.25
            + candidate.authority_score * 0.25
            + candidate.scope_score * 0.10
            + candidate.ats_confidence * 0.05,
            4,
        )
        self._record_evidence(candidate)
        if candidate.identity_score == 0:
            candidate.validation_status = "rejected"
            candidate.rejection_reason = "identity_conflict"
        elif candidate.recruitment_score < 0.34:
            candidate.validation_status = "rejected"
            candidate.rejection_reason = "no_recruitment_evidence"
        elif candidate.authority_score < 0.9 and len(candidate.sources) < 2:
            candidate.validation_status = "needs_review"
        elif candidate.confidence_score >= 0.7:
            candidate.validation_status = "validated"
        else:
            candidate.validation_status = "needs_review"

    def _record_evidence(self, candidate):
        values = (
            ("identity", str(candidate.identity_score), "validator", candidate.identity_score),
            ("recruitment", str(candidate.recruitment_score), "validator", candidate.recruitment_score),
            ("authority", str(candidate.authority_score), "validator", candidate.authority_score),
            ("scope", str(candidate.scope_score), "validator", candidate.scope_score),
            ("ats", candidate.ats_vendor, "ats_registry", candidate.ats_confidence),
        )
        for evidence_type, value, source, score in values:
            self.ledger.add_evidence(
                candidate.candidate_id, evidence_type, value, source, score
            )

    @staticmethod
    def _should_promote(candidate):
        independent_sources = len(candidate.sources)
        authoritative = candidate.authority_score >= 0.9
        return (
            candidate.validation_status == "validated"
            and candidate.identity_score >= 0.7
            and candidate.recruitment_score >= 2 / 3
            and candidate.confidence_score >= 0.8
            and (authoritative or independent_sources >= 2)
            and candidate.portal_type in {"careers", "ats_portal"}
        )

    @staticmethod
    def _portal_type(candidate):
        parts = urlsplit(candidate.final_url or candidate.canonical_url)
        host = (parts.hostname or "").casefold()
        path = parts.path.casefold()
        if path.endswith(".xml") or "sitemap" in path:
            return "sitemap"
        if "/faq" in path or "frequently-asked" in path:
            return "informational"
        if re.search(r"/jobs?/(?:results?/)?\d", path):
            return "job_detail"
        if candidate.ats_vendor not in {"unknown", "custom"}:
            return "ats_portal"
        if host.startswith(("career.", "careers.", "jobs.")):
            return "careers"
        if any(
            term in path
            for term in (
                "career",
                "employment",
                "jobs",
                "join-us",
                "opportunit",
                "vacan",
                "work-in",
                "work-with",
            )
        ):
            return "careers"
        return "corporate"

    @staticmethod
    def _selection_score(candidate):
        score = candidate.confidence_score
        path = urlsplit(candidate.canonical_url).path.casefold()
        if candidate.portal_type == "ats_portal":
            score += 0.12
        if candidate.portal_type == "careers":
            score += 0.10
        if path.rstrip("/").endswith(("/careers", "/jobs", "/career")):
            score += 0.08
        if any(marker in path for marker in ("/about/careers", "/careers/applications")):
            score += 0.06
        if len(candidate.sources) >= 2:
            score += 0.05
        score -= min(0.1, path.count("/") * 0.005)
        return score

    @staticmethod
    def _blacklisted(url):
        host = registered_domain(url)
        return (
            any(host == domain or host.endswith(f".{domain}") for domain in BLACKLISTED_DOMAINS)
            or any(marker in host for marker in BLACKLISTED_DOMAIN_MARKERS)
        )


def load_requests(path: str) -> list[CompanyDiscoveryRequest]:
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    requests_ = []
    for row in rows:
        name = (row.get("brand") or row.get("company") or row.get("entity") or "").strip()
        if not name:
            continue
        requests_.append(
            CompanyDiscoveryRequest(
                company_name=name,
                aliases=tuple(filter(None, re.split(r"[|;]", row.get("aliases", "")))),
                official_domains=tuple(
                    filter(None, re.split(r"[|;]", row.get("official_domains", "")))
                ),
                country=row.get("country", "").strip(),
                language=row.get("language", "").strip(),
            )
        )
    return requests_


def main():
    parser = argparse.ArgumentParser(description="Evidence-driven career portal discovery")
    parser.add_argument("--input", required=True)
    parser.add_argument("--database", default="portal_discovery.db")
    parser.add_argument("--output", default="portal_discovery_results.csv")
    parser.add_argument("--no-search", action="store_true")
    args = parser.parse_args()
    provider = None if args.no_search else SerperSearchProvider()
    service = PortalDiscoveryService(
        EvidenceLedger(args.database), RequestsFetcher(), provider
    )
    results = []
    for request in load_requests(args.input):
        results.extend(candidate.to_row() for candidate in service.discover(request))
    columns = list(PortalCandidate("", "", "", "", "", "", "").to_row())
    with open(args.output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    print(f"Saved {len(results)} candidates to {args.output}")


if __name__ == "__main__":
    main()
