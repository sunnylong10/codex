import unittest
from datetime import datetime, timezone

from job_normalization import (
    JobObservation,
    normalize_date,
    normalize_employment_type,
    normalize_job,
    normalize_locations,
    normalize_url,
)


OBSERVED_AT = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


class JobNormalizationTest(unittest.TestCase):
    def test_employment_type_mapping_preserves_raw_value(self):
        cases = {
            "Regular Full Time": ("full_time", "Regular Full Time"),
            "Part-Time": ("part_time", "Part-Time"),
            "Fixed Term Contract": ("temporary", "Fixed Term Contract"),
            "Graduate Internship": ("internship", "Graduate Internship"),
            "": ("unknown", ""),
        }

        actual = {
            raw: normalize_employment_type(raw)
            for raw in cases
        }

        self.assertEqual(actual, cases)

    def test_locations_preserve_multiple_values_and_iso_countries(self):
        actual = [
            location.__dict__
            for location in normalize_locations(
                ["Singapore, Singapore", "Toronto, Ontario, Canada"]
            )
        ]

        self.assertEqual(
            actual,
            [
                {
                    "raw": "Singapore, Singapore",
                    "country_code": "SG",
                    "country": "Singapore",
                    "region": None,
                    "city": "Singapore",
                    "postal_code": None,
                    "confidence": 0.9,
                },
                {
                    "raw": "Toronto, Ontario, Canada",
                    "country_code": "CA",
                    "country": "Canada",
                    "region": "Ontario",
                    "city": "Toronto",
                    "postal_code": None,
                    "confidence": 0.9,
                },
            ],
        )

    def test_dates_distinguish_relative_and_exact_precision(self):
        actual = {
            "relative": normalize_date("Posted 3 Days Ago", OBSERVED_AT),
            "date": normalize_date("2026-07-01", OBSERVED_AT),
            "timestamp": normalize_date(1_752_883_200_000, OBSERVED_AT),
        }

        self.assertEqual(
            actual,
            {
                "relative": (
                    "2026-07-15T00:00:00+00:00",
                    "inferred_day",
                    "Posted 3 Days Ago",
                ),
                "date": ("2026-07-01T00:00:00+00:00", "day", "2026-07-01"),
                "timestamp": (
                    "2025-07-19T00:00:00+00:00",
                    "second",
                    "1752883200000",
                ),
            },
        )

    def test_complete_lever_observation_normalizes_to_canonical_record(self):
        observation = JobObservation(
            company_name="Example Corp",
            title=" Senior Software Engineer ",
            location_raw="Toronto, Ontario, Canada",
            department_raw="Engineering",
            employment_type_raw="Full Time",
            description_raw="<p>Build systems.</p><script>bad()</script>",
            posted_date_raw="2026-07-01",
            source_url="HTTPS://JOBS.LEVER.CO/example/ABC123?utm_source=test",
            source_name="Lever API",
            external_job_id="ABC123",
            requisition_id="REQ-42",
            tenant_id="example",
            observed_at=OBSERVED_AT,
        )

        job = normalize_job(observation)

        self.assertEqual(
            {
                "company_name": job.company_name,
                "title": job.title,
                "description_text": job.description_text,
                "location": job.locations[0].__dict__,
                "workplace_type": job.workplace_type,
                "department": job.department,
                "employment_type": job.employment_type,
                "posted_at": job.posted_at,
                "canonical_url": job.canonical_url,
                "vendor": job.source.ats_vendor,
                "external_job_id": job.source.external_job_id,
                "requisition_id": job.source.requisition_id,
                "status": job.status,
                "issues": [issue.__dict__ for issue in job.issues],
            },
            {
                "company_name": "Example Corp",
                "title": "Senior Software Engineer",
                "description_text": "Build systems.",
                "location": {
                    "raw": "Toronto, Ontario, Canada",
                    "country_code": "CA",
                    "country": "Canada",
                    "region": "Ontario",
                    "city": "Toronto",
                    "postal_code": None,
                    "confidence": 0.9,
                },
                "workplace_type": "onsite",
                "department": "Engineering",
                "employment_type": "full_time",
                "posted_at": "2026-07-01T00:00:00+00:00",
                "canonical_url": "https://jobs.lever.co/example/ABC123",
                "vendor": "lever",
                "external_job_id": "ABC123",
                "requisition_id": "REQ-42",
                "status": "active",
                "issues": [],
            },
        )

    def test_same_requisition_has_same_job_id_across_sources(self):
        shared = {
            "company_name": "Example Corp",
            "title": "Engineer",
            "location_raw": "Singapore",
            "requisition_id": "REQ-42",
            "observed_at": OBSERVED_AT,
        }
        workday = normalize_job(
            JobObservation(
                **shared,
                source_url="https://example.wd3.myworkdayjobs.com/jobs/1",
                source_name="Workday API",
                external_job_id="1",
            )
        )
        greenhouse = normalize_job(
            JobObservation(
                **shared,
                source_url="https://boards.greenhouse.io/example/jobs/999",
                source_name="Greenhouse API",
                external_job_id="999",
            )
        )

        self.assertEqual(workday.job_id, greenhouse.job_id)

    def test_same_external_id_does_not_collide_across_companies(self):
        first = normalize_job(
            JobObservation(
                company_name="First Corp",
                title="Engineer",
                source_url="https://first.example/jobs/123",
                source_name="Workday API",
                external_job_id="123",
                observed_at=OBSERVED_AT,
            )
        )
        second = normalize_job(
            JobObservation(
                company_name="Second Corp",
                title="Engineer",
                source_url="https://second.example/jobs/123",
                source_name="Workday API",
                external_job_id="123",
                observed_at=OBSERVED_AT,
            )
        )

        self.assertNotEqual(first.job_id, second.job_id)

    def test_invalid_navigation_record_is_not_active(self):
        job = normalize_job(
            JobObservation(
                company_name="Example Corp",
                title="Apply Now",
                source_url="not-a-url",
                observed_at=OBSERVED_AT,
            )
        )

        self.assertEqual(job.status, "invalid")
        self.assertEqual(
            [issue.code for issue in job.issues],
            ["invalid_title", "invalid_url", "missing_location"],
        )

    def test_url_normalization_removes_tracking(self):
        self.assertEqual(
            normalize_url("HTTPS://Jobs.Example.com/role/?utm_source=x&id=7#top"),
            "https://jobs.example.com/role?id=7",
        )


if __name__ == "__main__":
    unittest.main()
