"""Structured failure classification and bounded self-healing control plane."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

from discovery_store import stable_company_id


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FailureCode(StrEnum):
    URL_NOT_FOUND = "URL_NOT_FOUND"
    URL_REDIRECTED = "URL_REDIRECTED"
    URL_WRONG_PAGE = "URL_WRONG_PAGE"
    DOMAIN_EXPIRED = "DOMAIN_EXPIRED"
    ACCESS_DENIED = "ACCESS_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    COMPANY_MISMATCH = "COMPANY_MISMATCH"
    BRAND_SCOPE_MISMATCH = "BRAND_SCOPE_MISMATCH"
    COUNTRY_SCOPE_MISMATCH = "COUNTRY_SCOPE_MISMATCH"
    AMBIGUOUS_IDENTITY = "AMBIGUOUS_IDENTITY"
    ATS_CHANGED = "ATS_CHANGED"
    ATS_TENANT_STALE = "ATS_TENANT_STALE"
    ATS_BOARD_STALE = "ATS_BOARD_STALE"
    ATS_CONFLICT = "ATS_CONFLICT"
    NO_ATS_DETECTED = "NO_ATS_DETECTED"
    CUSTOM_ATS = "CUSTOM_ATS"
    JS_RENDER_FAILURE = "JS_RENDER_FAILURE"
    BROWSER_CRASH = "BROWSER_CRASH"
    PARSER_UNSUPPORTED = "PARSER_UNSUPPORTED"
    CONTENT_CHANGED = "CONTENT_CHANGED"
    MALFORMED_PAYLOAD = "MALFORMED_PAYLOAD"
    PARTIAL_EXTRACTION = "PARTIAL_EXTRACTION"
    NO_OPEN_JOBS = "NO_OPEN_JOBS"
    NO_COUNTRY_MATCHES = "NO_COUNTRY_MATCHES"
    JOBS_EXTRACTED = "JOBS_EXTRACTED"
    PORTAL_HEALTHY = "PORTAL_HEALTHY"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


class CorrectionAction(StrEnum):
    RETRY_SAME_ENDPOINT = "RETRY_SAME_ENDPOINT"
    FOLLOW_REDIRECT = "FOLLOW_REDIRECT"
    REFRESH_ATS_DETECTION = "REFRESH_ATS_DETECTION"
    REFRESH_TENANT = "REFRESH_TENANT"
    REFRESH_BOARD = "REFRESH_BOARD"
    REDISCOVER_PORTAL = "REDISCOVER_PORTAL"
    TRY_API_EXTRACTION = "TRY_API_EXTRACTION"
    TRY_BROWSER_EXTRACTION = "TRY_BROWSER_EXTRACTION"
    TRY_CUSTOM_EXTRACTION = "TRY_CUSTOM_EXTRACTION"
    RENDER_WITH_BROWSER = "RENDER_WITH_BROWSER"
    LOWER_CONCURRENCY = "LOWER_CONCURRENCY"
    USE_AGGREGATOR_FALLBACK = "USE_AGGREGATOR_FALLBACK"
    REQUEST_MANUAL_REVIEW = "REQUEST_MANUAL_REVIEW"
    MARK_HEALTHY_EMPTY = "MARK_HEALTHY_EMPTY"
    QUARANTINE_CANDIDATE = "QUARANTINE_CANDIDATE"


class RepairState(StrEnum):
    OBSERVED = "observed"
    CLASSIFIED = "classified"
    CORRECTION_PLANNED = "correction_planned"
    CORRECTION_RUNNING = "correction_running"
    VERIFYING = "verifying"
    RETRY_WAIT = "retry_wait"
    MANUAL_REVIEW = "manual_review"
    RESOLVED = "resolved"
    MONITORING = "monitoring"
    REJECTED = "rejected"
    EXHAUSTED = "exhausted"
    CANCELLED = "cancelled"


TERMINAL_STATES = {
    RepairState.RESOLVED,
    RepairState.MONITORING,
    RepairState.REJECTED,
    RepairState.EXHAUSTED,
    RepairState.CANCELLED,
}
ALLOWED_TRANSITIONS = {
    RepairState.OBSERVED: {RepairState.CLASSIFIED},
    RepairState.CLASSIFIED: {
        RepairState.CORRECTION_PLANNED,
        RepairState.MONITORING,
        RepairState.MANUAL_REVIEW,
        RepairState.REJECTED,
    },
    RepairState.CORRECTION_PLANNED: {
        RepairState.CORRECTION_RUNNING,
        RepairState.CANCELLED,
    },
    RepairState.CORRECTION_RUNNING: {
        RepairState.VERIFYING,
        RepairState.RETRY_WAIT,
        RepairState.MANUAL_REVIEW,
        RepairState.EXHAUSTED,
    },
    RepairState.VERIFYING: {
        RepairState.RESOLVED,
        RepairState.MONITORING,
        RepairState.REJECTED,
        RepairState.RETRY_WAIT,
        RepairState.MANUAL_REVIEW,
        RepairState.EXHAUSTED,
    },
    RepairState.RETRY_WAIT: {
        RepairState.CORRECTION_PLANNED,
        RepairState.EXHAUSTED,
        RepairState.CANCELLED,
    },
    RepairState.MANUAL_REVIEW: {
        RepairState.CORRECTION_PLANNED,
        RepairState.RESOLVED,
        RepairState.REJECTED,
        RepairState.CANCELLED,
    },
}


@dataclass(frozen=True)
class FailureObservation:
    failure_id: str
    run_id: str
    company_id: str
    company_name: str
    portal_id: str | None
    failure_code: FailureCode
    severity: str
    retryable: bool
    source_stage: str
    observed_at: str
    http_status: int | None = None
    error_code: str = ""
    message: str = ""
    evidence: dict | None = None
    detector_version: str = ""
    parser_version: str = ""


@dataclass(frozen=True)
class RetryBudget:
    max_attempts: int
    delays: tuple[timedelta, ...]

    def delay_after(self, attempt_number: int) -> timedelta | None:
        if attempt_number >= self.max_attempts or not self.delays:
            return None
        index = min(max(0, attempt_number - 1), len(self.delays) - 1)
        return self.delays[index]


@dataclass(frozen=True)
class CorrectionPlan:
    action: CorrectionAction
    max_attempts: int
    retry_delays_seconds: tuple[int, ...]
    requires_verification: bool = True


@dataclass(frozen=True)
class VerificationOutcome:
    fixed: bool
    healthy_empty: bool = False
    retryable: bool = False
    evidence: dict | None = None
    message: str = ""


TRANSIENT_BUDGET = RetryBudget(
    3, (timedelta(minutes=1), timedelta(minutes=10), timedelta(hours=1))
)
REDISCOVERY_BUDGET = RetryBudget(2, (timedelta(minutes=5), timedelta(hours=4)))
BROWSER_BUDGET = RetryBudget(2, (timedelta(seconds=0), timedelta(minutes=5)))
PARSER_BUDGET = RetryBudget(1, ())
NO_RETRY_BUDGET = RetryBudget(0, ())


POLICY: dict[FailureCode, tuple[CorrectionAction, RetryBudget]] = {
    FailureCode.URL_NOT_FOUND: (CorrectionAction.REDISCOVER_PORTAL, REDISCOVERY_BUDGET),
    FailureCode.URL_WRONG_PAGE: (CorrectionAction.REDISCOVER_PORTAL, REDISCOVERY_BUDGET),
    FailureCode.DOMAIN_EXPIRED: (CorrectionAction.REDISCOVER_PORTAL, REDISCOVERY_BUDGET),
    FailureCode.URL_REDIRECTED: (CorrectionAction.FOLLOW_REDIRECT, REDISCOVERY_BUDGET),
    FailureCode.TIMEOUT: (CorrectionAction.RETRY_SAME_ENDPOINT, TRANSIENT_BUDGET),
    FailureCode.NETWORK_ERROR: (CorrectionAction.RETRY_SAME_ENDPOINT, TRANSIENT_BUDGET),
    FailureCode.RATE_LIMITED: (CorrectionAction.LOWER_CONCURRENCY, TRANSIENT_BUDGET),
    FailureCode.ACCESS_DENIED: (CorrectionAction.TRY_BROWSER_EXTRACTION, BROWSER_BUDGET),
    FailureCode.COMPANY_MISMATCH: (CorrectionAction.QUARANTINE_CANDIDATE, NO_RETRY_BUDGET),
    FailureCode.AMBIGUOUS_IDENTITY: (CorrectionAction.REQUEST_MANUAL_REVIEW, NO_RETRY_BUDGET),
    FailureCode.ATS_CHANGED: (CorrectionAction.REFRESH_ATS_DETECTION, REDISCOVERY_BUDGET),
    FailureCode.ATS_TENANT_STALE: (CorrectionAction.REFRESH_TENANT, REDISCOVERY_BUDGET),
    FailureCode.ATS_BOARD_STALE: (CorrectionAction.REFRESH_BOARD, REDISCOVERY_BUDGET),
    FailureCode.ATS_CONFLICT: (CorrectionAction.REQUEST_MANUAL_REVIEW, NO_RETRY_BUDGET),
    FailureCode.NO_ATS_DETECTED: (CorrectionAction.TRY_CUSTOM_EXTRACTION, PARSER_BUDGET),
    FailureCode.CUSTOM_ATS: (CorrectionAction.TRY_CUSTOM_EXTRACTION, PARSER_BUDGET),
    FailureCode.JS_RENDER_FAILURE: (CorrectionAction.RENDER_WITH_BROWSER, BROWSER_BUDGET),
    FailureCode.BROWSER_CRASH: (CorrectionAction.RENDER_WITH_BROWSER, BROWSER_BUDGET),
    FailureCode.PARSER_UNSUPPORTED: (
        CorrectionAction.USE_AGGREGATOR_FALLBACK,
        PARSER_BUDGET,
    ),
    FailureCode.CONTENT_CHANGED: (CorrectionAction.REFRESH_ATS_DETECTION, PARSER_BUDGET),
    FailureCode.MALFORMED_PAYLOAD: (CorrectionAction.REQUEST_MANUAL_REVIEW, PARSER_BUDGET),
    FailureCode.PARTIAL_EXTRACTION: (CorrectionAction.RETRY_SAME_ENDPOINT, TRANSIENT_BUDGET),
    FailureCode.NO_OPEN_JOBS: (CorrectionAction.MARK_HEALTHY_EMPTY, NO_RETRY_BUDGET),
    FailureCode.NO_COUNTRY_MATCHES: (
        CorrectionAction.MARK_HEALTHY_EMPTY,
        NO_RETRY_BUDGET,
    ),
}


def plan_for_failure(failure_code: FailureCode) -> CorrectionPlan:
    action, budget = POLICY.get(
        failure_code,
        (CorrectionAction.REQUEST_MANUAL_REVIEW, NO_RETRY_BUDGET),
    )
    return CorrectionPlan(
        action=action,
        max_attempts=budget.max_attempts,
        retry_delays_seconds=tuple(int(delay.total_seconds()) for delay in budget.delays),
        requires_verification=action
        not in {CorrectionAction.MARK_HEALTHY_EMPTY, CorrectionAction.QUARANTINE_CANDIDATE},
    )


def classify_diagnostic(row: dict, run_id: str) -> FailureObservation:
    company_name = str(row.get("Company") or row.get("company") or "").strip()
    verdict = str(row.get("Verdict") or row.get("verdict") or "").strip()
    error_code = str(row.get("Error Code") or row.get("error_code") or "").strip()
    note = str(row.get("Note") or row.get("note") or "").strip()
    url = str(row.get("URL Tried") or row.get("url") or "").strip()
    blob = f"{error_code} {note}".casefold()
    http_status = None
    match = __import__("re").search(r"\b(4\d\d|5\d\d)\b", blob)
    if match:
        http_status = int(match.group(1))
    if verdict == "OK":
        failure_code = FailureCode.JOBS_EXTRACTED
    elif verdict.startswith("NO SG MATCHES") or verdict.startswith("NO COUNTRY MATCHES"):
        failure_code = FailureCode.NO_COUNTRY_MATCHES
    elif verdict == "SITE EMPTY OR EXTRACTION FAILED":
        failure_code = FailureCode.PARSER_UNSUPPORTED
    elif "mismatch" in blob:
        failure_code = FailureCode.COMPANY_MISMATCH
    elif "timeout" in blob:
        failure_code = FailureCode.TIMEOUT
    elif "429" in blob or "rate" in blob and "limit" in blob:
        failure_code = FailureCode.RATE_LIMITED
    elif "403" in blob or "access denied" in blob:
        failure_code = FailureCode.ACCESS_DENIED
    elif error_code in {"PW_CRASH", "TASK_CRASH"} or "browser" in blob and "closed" in blob:
        failure_code = FailureCode.BROWSER_CRASH
    elif error_code.startswith("WD_404"):
        failure_code = FailureCode.ATS_BOARD_STALE
    elif "404" in blob or verdict == "URL/SITE ERROR":
        failure_code = FailureCode.URL_NOT_FOUND
    else:
        failure_code = FailureCode.UNKNOWN_FAILURE
    healthy = failure_code in {
        FailureCode.JOBS_EXTRACTED,
        FailureCode.NO_COUNTRY_MATCHES,
        FailureCode.NO_OPEN_JOBS,
        FailureCode.PORTAL_HEALTHY,
    }
    retryable = failure_code not in {
        FailureCode.COMPANY_MISMATCH,
        FailureCode.JOBS_EXTRACTED,
        FailureCode.NO_COUNTRY_MATCHES,
        FailureCode.NO_OPEN_JOBS,
    }
    failure_key = "|".join((run_id, company_name, failure_code, url, error_code))
    return FailureObservation(
        failure_id=str(uuid.uuid5(uuid.NAMESPACE_URL, failure_key)),
        run_id=run_id,
        company_id=stable_company_id(company_name),
        company_name=company_name,
        portal_id=None,
        failure_code=failure_code,
        severity="healthy" if healthy else "recoverable" if retryable else "review",
        retryable=retryable,
        source_stage="job_extraction",
        observed_at=utc_now().isoformat(),
        http_status=http_status,
        error_code=error_code,
        message=note,
        evidence={"verdict": verdict, "url": url},
    )


class InvalidTransition(ValueError):
    pass


class SelfHealingStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS failure_observation (
                    failure_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    portal_id TEXT,
                    failure_code TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    retryable INTEGER NOT NULL,
                    source_stage TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    http_status INTEGER,
                    error_code TEXT NOT NULL,
                    message TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    detector_version TEXT NOT NULL,
                    parser_version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repair_request (
                    repair_id TEXT PRIMARY KEY,
                    failure_id TEXT NOT NULL REFERENCES failure_observation(failure_id),
                    company_id TEXT NOT NULL,
                    portal_id TEXT,
                    failure_code TEXT NOT NULL,
                    action TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    retry_delays_json TEXT NOT NULL,
                    next_attempt_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS repair_request_work_idx
                    ON repair_request(state, next_attempt_at);
                CREATE TABLE IF NOT EXISTS correction_attempt (
                    attempt_id TEXT PRIMARY KEY,
                    repair_id TEXT NOT NULL REFERENCES repair_request(repair_id),
                    attempt_number INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error_code TEXT NOT NULL,
                    UNIQUE(repair_id, attempt_number)
                );
                CREATE TABLE IF NOT EXISTS repair_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    repair_id TEXT NOT NULL REFERENCES repair_request(repair_id),
                    evidence_type TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS manual_override (
                    override_id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    portal_id TEXT,
                    action TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT
                );
                """
            )

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return closing(connection)

    def ingest(self, observation: FailureObservation) -> str | None:
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO failure_observation
                       (failure_id, run_id, company_id, company_name, portal_id,
                        failure_code, severity, retryable, source_stage, observed_at,
                        http_status, error_code, message, evidence_json,
                        detector_version, parser_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation.failure_id,
                    observation.run_id,
                    observation.company_id,
                    observation.company_name,
                    observation.portal_id,
                    observation.failure_code,
                    observation.severity,
                    int(observation.retryable),
                    observation.source_stage,
                    observation.observed_at,
                    observation.http_status,
                    observation.error_code,
                    observation.message,
                    json.dumps(observation.evidence or {}, sort_keys=True),
                    observation.detector_version,
                    observation.parser_version,
                ),
            )
            existing = db.execute(
                "SELECT repair_id FROM repair_request WHERE failure_id = ?",
                (observation.failure_id,),
            ).fetchone()
            if existing:
                db.commit()
                return existing["repair_id"]
            plan = plan_for_failure(observation.failure_code)
            if plan.action == CorrectionAction.MARK_HEALTHY_EMPTY:
                state = RepairState.MONITORING
            elif plan.action == CorrectionAction.QUARANTINE_CANDIDATE:
                state = RepairState.REJECTED
            elif plan.action == CorrectionAction.REQUEST_MANUAL_REVIEW:
                state = RepairState.MANUAL_REVIEW
            elif observation.failure_code == FailureCode.JOBS_EXTRACTED:
                db.commit()
                return None
            else:
                state = RepairState.CORRECTION_PLANNED
            repair_id = str(uuid.uuid5(uuid.UUID(observation.failure_id), plan.action))
            now = utc_now().isoformat()
            db.execute(
                """INSERT INTO repair_request
                       (repair_id, failure_id, company_id, portal_id, failure_code,
                        action, state, attempt_count, max_attempts,
                        retry_delays_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
                (
                    repair_id,
                    observation.failure_id,
                    observation.company_id,
                    observation.portal_id,
                    observation.failure_code,
                    plan.action,
                    state,
                    plan.max_attempts,
                    json.dumps(plan.retry_delays_seconds),
                    now,
                    now,
                ),
            )
            db.commit()
        return repair_id

    def state(self, repair_id: str) -> RepairState:
        with self.connect() as db:
            row = db.execute(
                "SELECT state FROM repair_request WHERE repair_id = ?", (repair_id,)
            ).fetchone()
        if row is None:
            raise KeyError(repair_id)
        return RepairState(row["state"])

    def transition(self, repair_id: str, target: RepairState) -> None:
        current = self.state(repair_id)
        if current in TERMINAL_STATES or target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise InvalidTransition(f"cannot transition {current} -> {target}")
        with self.connect() as db:
            db.execute(
                "UPDATE repair_request SET state = ?, updated_at = ? WHERE repair_id = ?",
                (target, utc_now().isoformat(), repair_id),
            )
            db.commit()

    def start_attempt(self, repair_id: str, inputs: dict | None = None) -> str:
        if self.state(repair_id) != RepairState.CORRECTION_PLANNED:
            raise InvalidTransition("attempt can start only when correction is planned")
        with self.connect() as db:
            request = db.execute(
                "SELECT * FROM repair_request WHERE repair_id = ?", (repair_id,)
            ).fetchone()
            attempt_number = request["attempt_count"] + 1
            if attempt_number > request["max_attempts"]:
                raise InvalidTransition("retry budget exhausted")
            attempt_id = str(uuid.uuid4())
            now = utc_now().isoformat()
            db.execute(
                """INSERT INTO correction_attempt
                       (attempt_id, repair_id, attempt_number, action, status,
                        input_json, output_json, started_at, error_code)
                   VALUES (?, ?, ?, ?, 'running', ?, '{}', ?, '')""",
                (
                    attempt_id,
                    repair_id,
                    attempt_number,
                    request["action"],
                    json.dumps(inputs or {}, sort_keys=True),
                    now,
                ),
            )
            db.execute(
                """UPDATE repair_request
                   SET state = ?, attempt_count = ?, updated_at = ?
                   WHERE repair_id = ?""",
                (RepairState.CORRECTION_RUNNING, attempt_number, now, repair_id),
            )
            db.commit()
        return attempt_id

    def complete_attempt(
        self,
        attempt_id: str,
        output: dict | None = None,
        error_code: str = "",
    ) -> None:
        with self.connect() as db:
            attempt = db.execute(
                "SELECT * FROM correction_attempt WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise KeyError(attempt_id)
            status = "failed" if error_code else "completed"
            db.execute(
                """UPDATE correction_attempt
                   SET status = ?, output_json = ?, completed_at = ?, error_code = ?
                   WHERE attempt_id = ?""",
                (
                    status,
                    json.dumps(output or {}, sort_keys=True),
                    utc_now().isoformat(),
                    error_code,
                    attempt_id,
                ),
            )
            db.commit()
        if error_code:
            self._schedule_retry_or_exhaust(attempt["repair_id"])
        else:
            self.transition(attempt["repair_id"], RepairState.VERIFYING)

    def verify(self, repair_id: str, outcome: VerificationOutcome) -> RepairState:
        if self.state(repair_id) != RepairState.VERIFYING:
            raise InvalidTransition("repair must be verifying")
        if outcome.fixed:
            target = RepairState.MONITORING if outcome.healthy_empty else RepairState.RESOLVED
            self.transition(repair_id, target)
        elif outcome.retryable:
            self._schedule_retry_or_exhaust(repair_id)
            target = self.state(repair_id)
        else:
            target = RepairState.REJECTED
            self.transition(repair_id, target)
        self.add_evidence(repair_id, "verification", asdict(outcome))
        return target

    def _schedule_retry_or_exhaust(self, repair_id: str) -> None:
        with self.connect() as db:
            request = db.execute(
                "SELECT * FROM repair_request WHERE repair_id = ?", (repair_id,)
            ).fetchone()
            if request["attempt_count"] >= request["max_attempts"]:
                target = RepairState.EXHAUSTED
                next_attempt = None
            else:
                delays = json.loads(request["retry_delays_json"])
                index = min(max(0, request["attempt_count"] - 1), len(delays) - 1)
                delay = delays[index] if delays else 0
                target = RepairState.RETRY_WAIT
                next_attempt = (utc_now() + timedelta(seconds=delay)).isoformat()
            db.execute(
                """UPDATE repair_request
                   SET state = ?, next_attempt_at = ?, updated_at = ?
                   WHERE repair_id = ?""",
                (target, next_attempt, utc_now().isoformat(), repair_id),
            )
            db.commit()

    def add_evidence(self, repair_id: str, evidence_type: str, value: dict) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO repair_evidence
                       (evidence_id, repair_id, evidence_type, value_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    repair_id,
                    evidence_type,
                    json.dumps(value, sort_keys=True),
                    utc_now().isoformat(),
                ),
            )
            db.commit()
