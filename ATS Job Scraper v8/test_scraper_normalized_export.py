import tempfile
import unittest
from pathlib import Path

import pandas as pd

import scraper


class ScraperNormalizedExportTest(unittest.TestCase):
    def test_export_normalizes_and_deduplicates_legacy_jobs(self):
        records = [
            {
                "company": "Example Corp",
                "title": "Software Engineer",
                "location": "Singapore, Singapore",
                "department": "Engineering",
                "job_type": "Full Time",
                "url": "https://jobs.lever.co/example/abc?utm_source=one",
                "posted_date": "2026-07-01",
                "source": "Lever API",
            },
            {
                "company": "Example Corp",
                "title": "Software Engineer",
                "location": "Singapore, Singapore",
                "department": "Engineering",
                "job_type": "Full Time",
                "url": "https://jobs.lever.co/example/abc?utm_source=two",
                "posted_date": "2026-07-01",
                "source": "Lever API",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "jobs.xlsx"
            scraper.DIAG.clear()

            count = scraper.export(records, output)
            jobs = pd.read_excel(output, "All Jobs")

        self.assertEqual(count, 1)
        self.assertEqual(
            jobs[
                [
                    "Company",
                    "Job Title",
                    "Country Code",
                    "Employment Type",
                    "Workplace Type",
                    "Job URL",
                    "ATS Vendor",
                ]
            ].to_dict("records"),
            [
                {
                    "Company": "Example Corp",
                    "Job Title": "Software Engineer",
                    "Country Code": "SG",
                    "Employment Type": "full_time",
                    "Workplace Type": "onsite",
                    "Job URL": "https://jobs.lever.co/example/abc",
                    "ATS Vendor": "lever",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
