import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from self_healing import (
    CorrectionAction,
    FailureCode,
    InvalidTransition,
    RepairState,
    SelfHealingStore,
    VerificationOutcome,
    classify_diagnostic,
    plan_for_failure,
)


class SelfHealingTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "healing.db"
        self.store = SelfHealingStore(self.database)

    def tearDown(self):
        self.tempdir.cleanup()

    def observation(self, verdict, error_code="", note=""):
        return classify_diagnostic(
            {
                "Company": "Example Corp",
                "Verdict": verdict,
                "Error Code": error_code,
                "URL Tried": "https://example.com/careers",
                "Note": note,
            },
            "run-1",
        )

    def test_current_diagnostics_map_to_stable_failure_codes(self):
        cases = {
            ("URL/SITE ERROR", "WD_404", ""): FailureCode.ATS_BOARD_STALE,
            ("URL/SITE ERROR", "", "request timeout"): FailureCode.TIMEOUT,
            ("URL/SITE ERROR", "HTTP_403", "access denied"): FailureCode.ACCESS_DENIED,
            ("SITE EMPTY OR EXTRACTION FAILED", "ZERO_CARDS", ""): FailureCode.PARSER_UNSUPPORTED,
            ("NO SG MATCHES (jobs exist elsewhere)", "OK", ""): FailureCode.NO_COUNTRY_MATCHES,
            ("OK", "OK", ""): FailureCode.JOBS_EXTRACTED,
        }

        actual = {
            key: self.observation(*key).failure_code
            for key in cases
        }

        self.assertEqual(actual, cases)

    def test_policy_uses_failure_specific_corrections(self):
        actual = {
            code: plan_for_failure(code).action
            for code in (
                FailureCode.TIMEOUT,
                FailureCode.ATS_BOARD_STALE,
                FailureCode.COMPANY_MISMATCH,
                FailureCode.PARSER_UNSUPPORTED,
                FailureCode.NO_OPEN_JOBS,
            )
        }

        self.assertEqual(
            actual,
            {
                FailureCode.TIMEOUT: CorrectionAction.RETRY_SAME_ENDPOINT,
                FailureCode.ATS_BOARD_STALE: CorrectionAction.REFRESH_BOARD,
                FailureCode.COMPANY_MISMATCH: CorrectionAction.QUARANTINE_CANDIDATE,
                FailureCode.PARSER_UNSUPPORTED: CorrectionAction.USE_AGGREGATOR_FALLBACK,
                FailureCode.NO_OPEN_JOBS: CorrectionAction.MARK_HEALTHY_EMPTY,
            },
        )

    def test_successful_correction_must_verify_before_resolution(self):
        repair_id = self.store.ingest(self.observation("URL/SITE ERROR", "HTTP_404"))
        attempt_id = self.store.start_attempt(repair_id, {"old_url": "bad"})

        self.store.complete_attempt(attempt_id, {"new_url": "good"})
        state_before_verification = self.store.state(repair_id)
        state_after_verification = self.store.verify(
            repair_id,
            VerificationOutcome(True, evidence={"http_status": 200}),
        )

        self.assertEqual(state_before_verification, RepairState.VERIFYING)
        self.assertEqual(state_after_verification, RepairState.RESOLVED)
        self.assertEqual(self.store.state(repair_id), RepairState.RESOLVED)

    def test_failed_transient_attempt_schedules_retry_then_exhausts(self):
        repair_id = self.store.ingest(
            self.observation("URL/SITE ERROR", "TIMEOUT", "request timeout")
        )
        states = []
        for attempt_number in range(1, 4):
            if self.store.state(repair_id) == RepairState.RETRY_WAIT:
                self.store.transition(repair_id, RepairState.CORRECTION_PLANNED)
            attempt_id = self.store.start_attempt(repair_id)
            self.store.complete_attempt(attempt_id, error_code="timeout")
            states.append(self.store.state(repair_id))

        self.assertEqual(
            states,
            [RepairState.RETRY_WAIT, RepairState.RETRY_WAIT, RepairState.EXHAUSTED],
        )

    def test_no_jobs_is_monitoring_not_rediscovery(self):
        repair_id = self.store.ingest(self.observation("NO SG MATCHES (jobs exist elsewhere)"))

        self.assertEqual(self.store.state(repair_id), RepairState.MONITORING)

    def test_company_mismatch_is_quarantined_without_retry(self):
        repair_id = self.store.ingest(
            self.observation("URL/SITE ERROR", "IDENTITY", "company mismatch")
        )

        self.assertEqual(self.store.state(repair_id), RepairState.REJECTED)
        with self.assertRaises(InvalidTransition):
            self.store.start_attempt(repair_id)

    def test_terminal_state_cannot_transition(self):
        repair_id = self.store.ingest(self.observation("URL/SITE ERROR", "HTTP_404"))
        attempt_id = self.store.start_attempt(repair_id)
        self.store.complete_attempt(attempt_id, {"new_url": "good"})
        self.store.verify(repair_id, VerificationOutcome(True))

        with self.assertRaises(InvalidTransition):
            self.store.transition(repair_id, RepairState.CORRECTION_PLANNED)

    def test_attempt_and_verification_evidence_are_persisted(self):
        repair_id = self.store.ingest(self.observation("URL/SITE ERROR", "HTTP_404"))
        attempt_id = self.store.start_attempt(repair_id, {"strategy": "serper"})
        self.store.complete_attempt(attempt_id, {"candidate_count": 2})
        self.store.verify(
            repair_id,
            VerificationOutcome(True, evidence={"identity_score": 0.95}),
        )

        with closing(sqlite3.connect(self.database)) as db:
            attempt = db.execute(
                "SELECT status, input_json, output_json FROM correction_attempt"
            ).fetchone()
            evidence_count = db.execute(
                "SELECT COUNT(*) FROM repair_evidence"
            ).fetchone()[0]
        self.assertEqual(attempt[0], "completed")
        self.assertIn("serper", attempt[1])
        self.assertIn("candidate_count", attempt[2])
        self.assertEqual(evidence_count, 1)


if __name__ == "__main__":
    unittest.main()
