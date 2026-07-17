"""Persistent company, discovery, and recruitment-portal state.

SQLite is the operational source of truth. CSV, Excel, and the legacy
``CAREER_PAGES`` dictionary remain compatibility inputs/outputs only.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


DEFAULT_DATABASE = Path("ats_discovery.db")
COMPANY_NAMESPACE = uuid.UUID("43d820d2-8c7a-4fa3-929c-67dc0771cb61")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_name(name: str) -> str:
    return " ".join(name.casefold().split())


def stable_company_id(name: str) -> str:
    return str(uuid.uuid5(COMPANY_NAMESPACE, normalize_name(name)))


def canonical_url(url: str) -> str:
    value = url.strip()
    if not value:
        return value
    parts = urlsplit(value)
    scheme = parts.scheme.casefold() or "https"
    host = (parts.hostname or "").casefold()
    port = f":{parts.port}" if parts.port else ""
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, host + port, path, parts.query, ""))


class DiscoveryStore:
    def __init__(self, path: str | Path = DEFAULT_DATABASE):
        self.path = Path(path)
        self.initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS company (
                    company_id TEXT PRIMARY KEY,
                    canonical_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pipeline_run (
                    run_id TEXT PRIMARY KEY,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    configuration_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS discovery_request (
                    request_id TEXT PRIMARY KEY,
                    run_id TEXT REFERENCES pipeline_run(run_id),
                    company_id TEXT NOT NULL REFERENCES company(company_id),
                    reason TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    next_attempt_at TEXT,
                    last_error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS discovery_request_work_idx
                    ON discovery_request(state, next_attempt_at);
                CREATE INDEX IF NOT EXISTS discovery_request_company_idx
                    ON discovery_request(company_id, created_at);

                CREATE TABLE IF NOT EXISTS portal_candidate (
                    candidate_id TEXT PRIMARY KEY,
                    request_id TEXT REFERENCES discovery_request(request_id),
                    company_id TEXT NOT NULL REFERENCES company(company_id),
                    original_url TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    ats_vendor TEXT,
                    ats_identifier TEXT,
                    state TEXT NOT NULL,
                    confidence REAL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(company_id, canonical_url, strategy)
                );

                CREATE INDEX IF NOT EXISTS portal_candidate_company_idx
                    ON portal_candidate(company_id, state);
                CREATE INDEX IF NOT EXISTS portal_candidate_url_idx
                    ON portal_candidate(canonical_url);

                CREATE TABLE IF NOT EXISTS recruitment_portal (
                    portal_id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL REFERENCES company(company_id),
                    canonical_url TEXT NOT NULL,
                    ats_vendor TEXT,
                    ats_identifier TEXT,
                    portal_type TEXT NOT NULL DEFAULT 'careers',
                    country_code TEXT,
                    language_code TEXT,
                    status TEXT NOT NULL,
                    confidence REAL,
                    source TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_to TEXT,
                    last_verified_at TEXT,
                    last_successful_scrape_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(company_id, canonical_url)
                );

                CREATE INDEX IF NOT EXISTS recruitment_portal_active_idx
                    ON recruitment_portal(company_id, status, valid_to);

                CREATE TABLE IF NOT EXISTS scrape_observation (
                    observation_id TEXT PRIMARY KEY,
                    run_id TEXT REFERENCES pipeline_run(run_id),
                    company_id TEXT NOT NULL REFERENCES company(company_id),
                    portal_id TEXT REFERENCES recruitment_portal(portal_id),
                    outcome_code TEXT NOT NULL,
                    strategy TEXT,
                    raw_job_count INTEGER,
                    retained_job_count INTEGER,
                    error_code TEXT,
                    note TEXT,
                    observed_at TEXT NOT NULL
                );
                """
            )

    def ensure_company(self, name: str) -> str:
        company_id = stable_company_id(name)
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """INSERT INTO company
                       (company_id, canonical_name, normalized_name, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(normalized_name) DO UPDATE SET
                       canonical_name = excluded.canonical_name,
                       updated_at = excluded.updated_at""",
                (company_id, name.strip(), normalize_name(name), now, now),
            )
            row = db.execute(
                "SELECT company_id FROM company WHERE normalized_name = ?",
                (normalize_name(name),),
            ).fetchone()
        return row["company_id"]

    def import_legacy_registry(self, entries: dict[str, str]) -> int:
        imported = 0
        now = utc_now()
        with self.connect() as db:
            for name, url in entries.items():
                if not name.strip() or not url.strip():
                    continue
                company_id = stable_company_id(name)
                db.execute(
                    """INSERT INTO company
                           (company_id, canonical_name, normalized_name, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(normalized_name) DO UPDATE SET
                           canonical_name = excluded.canonical_name,
                           updated_at = excluded.updated_at""",
                    (company_id, name.strip(), normalize_name(name), now, now),
                )
                row = db.execute(
                    "SELECT company_id FROM company WHERE normalized_name = ?",
                    (normalize_name(name),),
                ).fetchone()
                company_id = row["company_id"]
                normalized_url = canonical_url(url)
                portal_id = str(uuid.uuid5(uuid.UUID(company_id), normalized_url))
                before = db.total_changes
                db.execute(
                    """INSERT OR IGNORE INTO recruitment_portal
                           (portal_id, company_id, canonical_url, status, confidence,
                            source, valid_from, created_at, updated_at)
                       VALUES (?, ?, ?, 'active', 1.0, 'legacy_registry', ?, ?, ?)""",
                    (portal_id, company_id, normalized_url, now, now, now),
                )
                imported += db.total_changes - before
        return imported

    def active_portals(self, company_names: list[str] | None = None) -> dict[str, str]:
        params: list[str] = []
        where = "p.status = 'active' AND p.valid_to IS NULL"
        if company_names is not None:
            normalized = [normalize_name(name) for name in company_names]
            if not normalized:
                return {}
            where += f" AND c.normalized_name IN ({','.join('?' for _ in normalized)})"
            params.extend(normalized)
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT c.canonical_name, p.canonical_url
                    FROM company c
                    JOIN recruitment_portal p USING (company_id)
                    WHERE {where}
                    ORDER BY c.canonical_name COLLATE NOCASE,
                             p.confidence DESC,
                             p.updated_at DESC""",
                params,
            ).fetchall()
        portals: dict[str, str] = {}
        for row in rows:
            portals.setdefault(row["canonical_name"], row["canonical_url"])
        return portals

    def start_run(self, trigger: str, configuration: dict) -> str:
        run_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute(
                """INSERT INTO pipeline_run
                       (run_id, trigger, status, configuration_json, started_at)
                   VALUES (?, ?, 'running', ?, ?)""",
                (run_id, trigger, json.dumps(configuration, sort_keys=True), utc_now()),
            )
        return run_id

    def finish_run(self, run_id: str, status: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE pipeline_run SET status = ?, completed_at = ? WHERE run_id = ?",
                (status, utc_now(), run_id),
            )

    def record_scrape_observation(
        self,
        run_id: str,
        company_name: str,
        outcome_code: str,
        strategy: str | None = None,
        raw_job_count: int | None = None,
        retained_job_count: int | None = None,
        error_code: str | None = None,
        note: str | None = None,
    ) -> str:
        company_id = self.ensure_company(company_name)
        observation_id = str(uuid.uuid4())
        with self.connect() as db:
            portal = db.execute(
                """SELECT portal_id FROM recruitment_portal
                   WHERE company_id = ? AND status = 'active' AND valid_to IS NULL
                   ORDER BY confidence DESC, updated_at DESC LIMIT 1""",
                (company_id,),
            ).fetchone()
            db.execute(
                """INSERT INTO scrape_observation
                       (observation_id, run_id, company_id, portal_id, outcome_code,
                        strategy, raw_job_count, retained_job_count, error_code,
                        note, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation_id,
                    run_id,
                    company_id,
                    portal["portal_id"] if portal else None,
                    outcome_code,
                    strategy,
                    raw_job_count,
                    retained_job_count,
                    error_code,
                    note,
                    utc_now(),
                ),
            )
        return observation_id

    def create_request(
        self, run_id: str, company_name: str, reason: str, max_attempts: int
    ) -> str:
        company_id = self.ensure_company(company_name)
        request_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """INSERT INTO discovery_request
                       (request_id, run_id, company_id, reason, state, max_attempts,
                        created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (request_id, run_id, company_id, reason, max_attempts, now, now),
            )
        return request_id

    def finish_request(
        self, request_id: str, state: str, error_code: str | None = None
    ) -> None:
        with self.connect() as db:
            db.execute(
                """UPDATE discovery_request
                   SET state = ?, attempt_count = attempt_count + 1,
                       last_error_code = ?, updated_at = ?
                   WHERE request_id = ?""",
                (state, error_code, utc_now(), request_id),
            )

    def record_candidate(
        self,
        request_id: str,
        company_name: str,
        url: str,
        strategy: str,
        ats_vendor: str | None = None,
        ats_identifier: str | None = None,
        confidence: float | None = None,
        evidence: dict | None = None,
    ) -> str:
        company_id = self.ensure_company(company_name)
        normalized_url = canonical_url(url)
        candidate_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """INSERT INTO portal_candidate
                       (candidate_id, request_id, company_id, original_url,
                        canonical_url, strategy, ats_vendor, ats_identifier, state,
                        confidence, evidence_json, discovered_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'validated', ?, ?, ?, ?)
                   ON CONFLICT(company_id, canonical_url, strategy) DO UPDATE SET
                       request_id = excluded.request_id,
                       ats_vendor = excluded.ats_vendor,
                       ats_identifier = excluded.ats_identifier,
                       confidence = excluded.confidence,
                       evidence_json = excluded.evidence_json,
                       updated_at = excluded.updated_at""",
                (
                    candidate_id,
                    request_id,
                    company_id,
                    url,
                    normalized_url,
                    strategy,
                    ats_vendor,
                    ats_identifier,
                    confidence,
                    json.dumps(evidence or {}, sort_keys=True),
                    now,
                    now,
                ),
            )
            row = db.execute(
                """SELECT candidate_id FROM portal_candidate
                   WHERE company_id = ? AND canonical_url = ? AND strategy = ?""",
                (company_id, normalized_url, strategy),
            ).fetchone()
        return row["candidate_id"]

    def promote_candidate(self, candidate_id: str) -> str:
        now = utc_now()
        with self.connect() as db:
            candidate = db.execute(
                "SELECT * FROM portal_candidate WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate is None:
                raise ValueError(f"unknown candidate: {candidate_id}")
            portal_id = str(
                uuid.uuid5(uuid.UUID(candidate["company_id"]), candidate["canonical_url"])
            )
            db.execute(
                """UPDATE recruitment_portal
                   SET status = 'retired', valid_to = ?, updated_at = ?
                   WHERE company_id = ? AND status = 'active' AND valid_to IS NULL
                     AND canonical_url <> ?""",
                (now, now, candidate["company_id"], candidate["canonical_url"]),
            )
            db.execute(
                """INSERT INTO recruitment_portal
                       (portal_id, company_id, canonical_url, ats_vendor,
                        ats_identifier, status, confidence, source, valid_from,
                        last_verified_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(company_id, canonical_url) DO UPDATE SET
                       ats_vendor = excluded.ats_vendor,
                       ats_identifier = excluded.ats_identifier,
                       status = 'active',
                       confidence = excluded.confidence,
                       source = excluded.source,
                       valid_to = NULL,
                       last_verified_at = excluded.last_verified_at,
                       updated_at = excluded.updated_at""",
                (
                    portal_id,
                    candidate["company_id"],
                    candidate["canonical_url"],
                    candidate["ats_vendor"],
                    candidate["ats_identifier"],
                    candidate["confidence"],
                    candidate["strategy"],
                    now,
                    now,
                    now,
                    now,
                ),
            )
            db.execute(
                "UPDATE portal_candidate SET state = 'promoted', updated_at = ? WHERE candidate_id = ?",
                (now, candidate_id),
            )
            db.execute(
                """UPDATE discovery_request
                   SET state = 'promoted', updated_at = ? WHERE request_id = ?""",
                (now, candidate["request_id"]),
            )
        return portal_id
