import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import discover_ats
from career_url_discovery import DiscoveryCache, normalize_url, rank_candidates


class CareerUrlDiscoveryTest(unittest.TestCase):
    def test_normalize_url_removes_tracking_and_preserves_functional_query(self):
        self.assertEqual(
            normalize_url(
                "HTTPS://Jobs.Example.COM/openings/?lang=en&utm_source=search#top"
            ),
            "https://jobs.example.com/openings?lang=en",
        )

    def test_ranking_deduplicates_and_rejects_identity_conflicts(self):
        hits = [
            {
                "url": "https://jobs.example.com/?utm_source=one",
                "ats": "greenhouse",
                "identifier": "example",
                "verified_name": "Example Corporation",
                "jobs": 0,
                "sg_jobs": 0,
                "source": "ats_probe",
            },
            {
                "url": "https://jobs.example.com/",
                "ats": "greenhouse",
                "identifier": "example",
                "verified_name": "Unrelated Industries",
                "jobs": 50,
                "sg_jobs": 0,
                "source": "ats_probe",
            },
        ]

        candidates = rank_candidates(hits, "Example Corp")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].validation_status, "validated")
        self.assertIn("valid_empty", candidates[0].reasons)
        self.assertEqual(candidates[0].is_primary, 1)

    def test_discover_all_keeps_candidates_from_multiple_ats_vendors(self):
        def greenhouse(_brand, slug):
            return {
                "url": f"https://boards.greenhouse.io/{slug}",
                "ats": "greenhouse",
                "identifier": slug,
                "verified_name": "Example Corp",
                "jobs": 10,
                "sg_jobs": 1,
            }

        def lever(_brand, slug):
            return {
                "url": f"https://jobs.lever.co/{slug}",
                "ats": "lever",
                "identifier": slug,
                "verified_name": "Example Corp",
                "jobs": 0,
                "sg_jobs": 0,
            }

        with patch.dict(
            discover_ats.PROBES, {"greenhouse": greenhouse, "lever": lever}
        ), patch.object(discover_ats.time, "sleep"):
            candidates = discover_ats.discover_all(
                "Example Corp", ["greenhouse", "lever"], "Singapore", "en-SG"
            )

        self.assertGreaterEqual(len(candidates), 2)
        self.assertEqual({row["ats"] for row in candidates}, {"greenhouse", "lever"})
        self.assertEqual(sum(row["is_primary"] for row in candidates), 1)
        self.assertTrue(all(row["country"] == "Singapore" for row in candidates))

    def test_cache_is_scoped_by_country_and_strategy(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = DiscoveryCache(Path(directory) / "cache.db")
            singapore = cache.key("Example", "Singapore", "en", ["greenhouse"])
            japan = cache.key("Example", "Japan", "ja", ["greenhouse"])
            cache.put(singapore, [{"url": "https://example.com/sg/jobs"}])

            self.assertEqual(
                cache.get(singapore), [{"url": "https://example.com/sg/jobs"}]
            )
            self.assertIsNone(cache.get(japan))


if __name__ == "__main__":
    unittest.main()
