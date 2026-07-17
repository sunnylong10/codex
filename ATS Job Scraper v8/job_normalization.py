"""Canonical job schema and pure normalization for extractor observations."""

from __future__ import annotations

import html
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from discovery_store import stable_company_id


SCHEMA_VERSION = "1.0"
JOB_NAMESPACE = uuid.UUID("57972d98-bac2-45da-b972-e20aa3b6a525")
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
COUNTRY_ALIASES = {
    "australia": ("AU", "Australia"),
    "canada": ("CA", "Canada"),
    "china": ("CN", "China"),
    "germany": ("DE", "Germany"),
    "hong kong": ("HK", "Hong Kong"),
    "india": ("IN", "India"),
    "japan": ("JP", "Japan"),
    "malaysia": ("MY", "Malaysia"),
    "singapore": ("SG", "Singapore"),
    "united kingdom": ("GB", "United Kingdom"),
    "uk": ("GB", "United Kingdom"),
    "united states": ("US", "United States"),
    "usa": ("US", "United States"),
}
TITLE_JUNK = {
    "apply now",
    "careers",
    "job search",
    "jobs",
    "learn more",
    "load more",
    "search jobs",
    "view all jobs",
}


def clean_text(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", html.unescape(str(value))).strip()


def normalize_url(url: str) -> str:
    value = clean_text(url)
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        return ""
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if key.casefold() not in TRACKING_PARAMETERS
        )
    )
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), path, query, "")
    )


def sanitize_description(value: str) -> tuple[str, str]:
    source = str(value or "")[:200_000]
    if not source:
        return "", ""
    soup = BeautifulSoup(source, "html.parser")
    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    text = clean_text(soup.get_text(" ", strip=True))[:100_000]
    return text, str(soup)[:200_000]


def normalize_employment_type(value) -> tuple[str, str]:
    if isinstance(value, list):
        raw = ", ".join(clean_text(item) for item in value if clean_text(item))
    else:
        raw = clean_text(value)
    normalized = re.sub(r"[^a-z]+", " ", raw.casefold()).strip()
    mappings = (
        ("internship", ("intern", "internship")),
        ("apprenticeship", ("apprentice", "apprenticeship")),
        ("part_time", ("part time", "parttime")),
        ("full_time", ("full time", "fulltime", "permanent")),
        ("temporary", ("fixed term", "temporary", "temp")),
        ("contract", ("contract", "contractor", "freelance")),
        ("seasonal", ("seasonal",)),
        ("volunteer", ("volunteer",)),
        ("per_diem", ("per diem",)),
    )
    for canonical, terms in mappings:
        if any(term in normalized for term in terms):
            return canonical, raw
    return "unknown", raw


def normalize_workplace_type(location: str, description: str = "") -> str:
    blob = f"{location} {description[:5_000]}".casefold()
    if re.search(r"\bhybrid\b", blob):
        return "hybrid"
    if re.search(r"\b(remote|work from home|home[- ]based)\b", blob):
        return "remote"
    if clean_text(location):
        return "onsite"
    return "unknown"


@dataclass(frozen=True)
class CanonicalLocation:
    raw: str
    country_code: str | None = None
    country: str | None = None
    region: str | None = None
    city: str | None = None
    postal_code: str | None = None
    confidence: float = 0.0


def normalize_locations(value) -> list[CanonicalLocation]:
    if isinstance(value, list):
        raw_locations = [clean_text(item) for item in value]
    else:
        raw = clean_text(value)
        raw_locations = re.split(r"\s*[|;]\s*", raw) if raw else []
    normalized = []
    seen = set()
    for raw in raw_locations:
        if not raw or raw.casefold() in seen:
            continue
        seen.add(raw.casefold())
        lower = raw.casefold()
        country_code = country = None
        for alias, (code, label) in COUNTRY_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", lower):
                country_code, country = code, label
                break
        parts = [part.strip() for part in raw.split(",") if part.strip()]
        city = parts[0] if parts and parts[0].casefold() not in COUNTRY_ALIASES else None
        if country_code in {"HK", "SG"} and parts:
            city = country
        region = parts[-2] if len(parts) >= 3 else None
        confidence = 0.9 if country_code and city else 0.75 if country_code else 0.35
        normalized.append(
            CanonicalLocation(
                raw=raw,
                country_code=country_code,
                country=country,
                region=region,
                city=city,
                confidence=confidence,
            )
        )
    return normalized


def normalize_date(value, observed_at: datetime) -> tuple[str | None, str, str]:
    raw = clean_text(value)
    if not raw:
        return None, "unknown", raw
    if isinstance(value, (int, float)) or raw.isdigit() and len(raw) >= 10:
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            parsed = datetime.fromtimestamp(timestamp, timezone.utc)
            return parsed.isoformat(), "second", raw
        except (OverflowError, OSError, ValueError):
            return None, "unknown", raw
    lower = raw.casefold()
    if lower in {"today", "posted today"}:
        parsed_date = observed_at.date()
        return datetime.combine(parsed_date, datetime.min.time(), timezone.utc).isoformat(), "day", raw
    match = re.search(r"(?:posted\s+)?(\d+)\+?\s+days?\s+ago", lower)
    if match:
        parsed_date = observed_at.date() - timedelta(days=int(match.group(1)))
        return datetime.combine(parsed_date, datetime.min.time(), timezone.utc).isoformat(), "inferred_day", raw
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        parsed_date = datetime.strptime(raw, "%Y-%m-%d").date()
        parsed = datetime.combine(parsed_date, datetime.min.time(), timezone.utc)
        return parsed.isoformat(), "day", raw
    candidate = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(), "second", raw
    except ValueError:
        pass
    for pattern in ("%Y-%m-%d", "%d %B %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            parsed_date = datetime.strptime(raw, pattern).date()
            parsed = datetime.combine(parsed_date, datetime.min.time(), timezone.utc)
            return parsed.isoformat(), "day", raw
        except ValueError:
            continue
    return None, "unknown", raw


def source_vendor(value: str) -> str:
    lower = clean_text(value).casefold()
    for vendor in ("workday", "greenhouse", "lever", "oracle", "smartrecruiters"):
        if vendor in lower:
            return vendor
    return "custom" if "browser" in lower else "unknown"


def external_id_from_url(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    if not path:
        return ""
    value = path.rsplit("/", 1)[-1]
    return value if len(value) <= 200 else ""


@dataclass(frozen=True)
class JobSource:
    ats_vendor: str
    tenant_id: str | None
    board_id: str | None
    external_job_id: str | None
    requisition_id: str | None
    extraction_method: str
    parser_version: str
    observed_at: str


@dataclass(frozen=True)
class JobObservation:
    company_name: str
    title: str
    location_raw: str | list[str] = ""
    department_raw: str = ""
    employment_type_raw: str | list[str] = ""
    description_raw: str = ""
    posted_date_raw: str | int | float = ""
    updated_date_raw: str | int | float = ""
    source_url: str = ""
    apply_url: str = ""
    source_name: str = ""
    external_job_id: str = ""
    requisition_id: str = ""
    tenant_id: str = ""
    board_id: str = ""
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class NormalizationIssue:
    code: str
    severity: str
    message: str


@dataclass
class CanonicalJob:
    schema_version: str
    job_id: str
    company_id: str
    company_name: str
    title: str
    description_text: str
    description_html: str
    locations: list[CanonicalLocation]
    primary_country_code: str | None
    primary_city: str | None
    workplace_type: str
    department: str | None
    team: str | None
    employment_type: str
    employment_type_raw: str
    seniority: str | None
    posted_at: str | None
    updated_at: str | None
    expires_at: str | None
    date_precision: str
    source_url: str
    apply_url: str | None
    canonical_url: str
    source: JobSource
    language_code: str | None
    status: str
    normalization_confidence: float
    raw_payload_ref: str | None
    issues: list[NormalizationIssue]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_excel_row(self) -> dict:
        primary = self.locations[0] if self.locations else None
        return {
            "Job ID": self.job_id,
            "Company ID": self.company_id,
            "Company": self.company_name,
            "Job Title": self.title,
            "Country": primary.country if primary else "",
            "Country Code": self.primary_country_code or "",
            "City": self.primary_city or "",
            "Location": primary.raw if primary else "",
            "Department": self.department or "",
            "Employment Type": self.employment_type,
            "Employment Type (Raw)": self.employment_type_raw,
            "Job Type": self.employment_type_raw,
            "Workplace Type": self.workplace_type,
            "Description": self.description_text,
            "Posted Date": self.posted_at or "",
            "Updated Date": self.updated_at or "",
            "Source URL": self.source_url,
            "Job URL": self.canonical_url,
            "Apply URL": self.apply_url or "",
            "ATS Vendor": self.source.ats_vendor,
            "External Job ID": self.source.external_job_id or "",
            "Source": self.source.extraction_method,
            "Normalization Confidence": self.normalization_confidence,
            "Normalization Issues": ",".join(issue.code for issue in self.issues),
            "Scraped At": self.source.observed_at,
        }


def normalize_job(observation: JobObservation) -> CanonicalJob:
    title = clean_text(observation.title)
    company_name = clean_text(observation.company_name)
    company_id = stable_company_id(company_name)
    source_url = normalize_url(observation.source_url)
    apply_url = normalize_url(observation.apply_url) or None
    canonical_url = apply_url or source_url
    description_text, description_html = sanitize_description(observation.description_raw)
    locations = normalize_locations(observation.location_raw)
    employment_type, employment_raw = normalize_employment_type(
        observation.employment_type_raw
    )
    posted_at, date_precision, posted_raw = normalize_date(
        observation.posted_date_raw, observation.observed_at
    )
    updated_at, _, _ = normalize_date(
        observation.updated_date_raw, observation.observed_at
    )
    vendor = source_vendor(observation.source_name)
    external_id = clean_text(observation.external_job_id) or external_id_from_url(
        canonical_url
    )
    issues = []
    if not title:
        issues.append(NormalizationIssue("missing_title", "error", "Job title is missing"))
    elif title.casefold() in TITLE_JUNK or len(title) < 3:
        issues.append(NormalizationIssue("invalid_title", "error", "Job title looks like navigation text"))
    if not canonical_url:
        issues.append(NormalizationIssue("invalid_url", "error", "No valid HTTP job URL"))
    if not locations:
        issues.append(NormalizationIssue("missing_location", "warning", "No structured location"))
    if observation.posted_date_raw and not posted_at:
        issues.append(
            NormalizationIssue(
                "unparsed_posted_date", "warning", f"Could not parse {posted_raw!r}"
            )
        )
    requisition_id = clean_text(observation.requisition_id)
    if requisition_id:
        strong_key = f"{company_id}|requisition|{requisition_id.casefold()}"
    elif vendor != "unknown" and external_id:
        source_scope = clean_text(observation.tenant_id) or company_id
        strong_key = f"{vendor}|{source_scope}|{external_id}"
    else:
        strong_key = (
            f"{company_id}|{canonical_url}|{title.casefold()}|"
            f"{'|'.join(location.raw.casefold() for location in locations)}"
        )
    job_id = str(uuid.uuid5(JOB_NAMESPACE, strong_key))
    confidence_components = [
        1.0 if external_id else 0.0,
        1.0 if canonical_url else 0.0,
        1.0 if title and not any(issue.code == "invalid_title" for issue in issues) else 0.0,
        1.0 if company_name else 0.0,
        max((location.confidence for location in locations), default=0.0),
        1.0 if posted_at else 0.0,
        1.0 if description_text else 0.0,
        1.0 if employment_type != "unknown" or observation.department_raw else 0.0,
        1.0 if vendor not in {"unknown", "custom"} else 0.5 if vendor == "custom" else 0.0,
    ]
    weights = (0.20, 0.15, 0.15, 0.15, 0.10, 0.10, 0.05, 0.05, 0.05)
    confidence = round(sum(score * weight for score, weight in zip(confidence_components, weights)), 4)
    source = JobSource(
        ats_vendor=vendor,
        tenant_id=clean_text(observation.tenant_id) or None,
        board_id=clean_text(observation.board_id) or None,
        external_job_id=external_id or None,
        requisition_id=requisition_id or None,
        extraction_method=clean_text(observation.source_name) or "unknown",
        parser_version=f"{vendor}-legacy-v1",
        observed_at=observation.observed_at.astimezone(timezone.utc).isoformat(),
    )
    return CanonicalJob(
        schema_version=SCHEMA_VERSION,
        job_id=job_id,
        company_id=company_id,
        company_name=company_name,
        title=title,
        description_text=description_text,
        description_html=description_html,
        locations=locations,
        primary_country_code=locations[0].country_code if locations else None,
        primary_city=locations[0].city if locations else None,
        workplace_type=normalize_workplace_type(
            locations[0].raw if locations else "", description_text
        ),
        department=clean_text(observation.department_raw) or None,
        team=None,
        employment_type=employment_type,
        employment_type_raw=employment_raw,
        seniority=None,
        posted_at=posted_at,
        updated_at=updated_at,
        expires_at=None,
        date_precision=date_precision,
        source_url=source_url,
        apply_url=apply_url,
        canonical_url=canonical_url,
        source=source,
        language_code=None,
        status="active" if not any(issue.severity == "error" for issue in issues) else "invalid",
        normalization_confidence=confidence,
        raw_payload_ref=None,
        issues=issues,
    )


def observation_from_legacy(record: dict, observed_at: datetime | None = None) -> JobObservation:
    return JobObservation(
        company_name=record.get("company", ""),
        title=record.get("title", ""),
        location_raw=record.get("locations", record.get("location", "")),
        department_raw=record.get("department", ""),
        employment_type_raw=record.get("employment_type", record.get("job_type", "")),
        description_raw=record.get("description_html", record.get("description", "")),
        posted_date_raw=record.get("posted_at", record.get("posted_date", "")),
        updated_date_raw=record.get("updated_at", ""),
        source_url=record.get("source_url", record.get("url", "")),
        apply_url=record.get("apply_url", ""),
        source_name=record.get("source", ""),
        external_job_id=record.get("external_job_id", ""),
        requisition_id=record.get("requisition_id", ""),
        tenant_id=record.get("tenant_id", ""),
        board_id=record.get("board_id", ""),
        observed_at=observed_at or datetime.now(timezone.utc),
    )


def normalize_legacy_jobs(records: list[dict], observed_at: datetime | None = None) -> list[CanonicalJob]:
    instant = observed_at or datetime.now(timezone.utc)
    return [normalize_job(observation_from_legacy(record, instant)) for record in records]
