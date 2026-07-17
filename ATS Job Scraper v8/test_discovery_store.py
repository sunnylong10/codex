import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from discovery_store import DiscoveryStore, canonical_url, stable_company_id


class DiscoveryStoreTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "test.db"
        self.store = DiscoveryStore(self.database)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_legacy_import_is_idempotent_and_ids_are_stable(self):
        entries = {"Example Corp": "https://jobs.example.com/openings/"}

        self.assertEqual(self.store.import_legacy_registry(entries), 1)
        self.assertEqual(self.store.import_legacy_registry(entries), 0)
        self.assertEqual(
            self.store.active_portals(),
            {"Example Corp": "https://jobs.example.com/openings"},
        )
        self.assertEqual(
            stable_company_id(" Example   Corp "), stable_company_id("example corp")
        )

    def test_promoting_candidate_retires_previous_portal(self):
        self.store.import_legacy_registry(
            {"Example Corp": "https://old.example.com/careers"}
        )
        run_id = self.store.start_run("test", {"source": "unit"})
        request_id = self.store.create_request(
            run_id, "Example Corp", "url_site_error", 2
        )
        candidate_id = self.store.record_candidate(
            request_id,
            "Example Corp",
            "HTTPS://NEW.EXAMPLE.COM/jobs/#fragment",
            "ats_probe",
            ats_vendor="greenhouse",
            confidence=0.95,
        )

        self.store.promote_candidate(candidate_id)
        self.store.finish_run(run_id, "completed")

        self.assertEqual(
            self.store.active_portals(), {"Example Corp": "https://new.example.com/jobs"}
        )
        with closing(sqlite3.connect(self.database)) as db:
            statuses = db.execute(
                "SELECT status FROM recruitment_portal ORDER BY status"
            ).fetchall()
            request_state = db.execute(
                "SELECT state FROM discovery_request WHERE request_id = ?",
                (request_id,),
            ).fetchone()[0]
        self.assertEqual(statuses, [("active",), ("retired",)])
        self.assertEqual(request_state, "promoted")

    def test_canonical_url_removes_fragment_and_trailing_slash(self):
        self.assertEqual(
            canonical_url("HTTPS://Jobs.Example.COM/openings/#top"),
            "https://jobs.example.com/openings",
        )

    def test_scrape_observation_links_run_company_and_portal(self):
        self.store.import_legacy_registry(
            {"Example Corp": "https://jobs.example.com/openings"}
        )
        run_id = self.store.start_run("test", {})

        self.store.record_scrape_observation(
            run_id,
            "Example Corp",
            "SUCCESS_WITH_JOBS",
            strategy="greenhouse",
            raw_job_count=12,
            retained_job_count=2,
        )

        with closing(sqlite3.connect(self.database)) as db:
            row = db.execute(
                """SELECT outcome_code, raw_job_count, retained_job_count, portal_id
                   FROM scrape_observation"""
            ).fetchone()
        self.assertEqual(row[:3], ("SUCCESS_WITH_JOBS", 12, 2))
        self.assertIsNotNone(row[3])


if __name__ == "__main__":
    unittest.main()
