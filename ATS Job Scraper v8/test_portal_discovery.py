import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from portal_discovery import (
    CompanyDiscoveryRequest,
    EvidenceLedger,
    FetchResult,
    PortalDiscoveryService,
    SearchResult,
)


class FakeSearchProvider:
    provider_id = "fake_search"

    def __init__(self, results):
        self.results = results
        self.queries = []

    def search(self, query, limit=10):
        self.queries.append(query)
        return [
            SearchResult(
                **{
                    **result.__dict__,
                    "query": query,
                    "provider": self.provider_id,
                }
            )
            for result in self.results
        ]


class FakeFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return self.responses.get(
            url, FetchResult(url, url, 404, "", error_code="not_found")
        )


class PortalDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "ledger.db"
        self.ledger = EvidenceLedger(self.database)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_official_link_and_search_evidence_promote_same_candidate(self):
        root = "https://acme.example"
        careers = "https://jobs.acme.example/careers"
        fetcher = FakeFetcher(
            {
                root: FetchResult(
                    root,
                    root,
                    200,
                    f'<html><a href="{careers}">Careers</a></html>',
                ),
                f"{root}/sitemap.xml": FetchResult(
                    f"{root}/sitemap.xml", f"{root}/sitemap.xml", 200, "<xml></xml>"
                ),
                careers: FetchResult(
                    careers,
                    careers,
                    200,
                    "<title>Acme Careers</title>Search jobs. Current openings. Join our team.",
                ),
            }
        )
        search = FakeSearchProvider(
            [SearchResult(careers, title="Acme Careers", snippet="Current openings")]
        )
        service = PortalDiscoveryService(self.ledger, fetcher, search)

        results = service.discover(
            CompanyDiscoveryRequest("Acme", official_domains=(root,))
        )

        candidate = next(item for item in results if item.canonical_url == careers)
        self.assertEqual(candidate.sources, {"official_crawl", "search"})
        self.assertEqual(candidate.validation_status, "promoted")
        self.assertTrue(candidate.promoted)

    def test_partial_name_match_does_not_auto_promote_air_canada(self):
        url = "https://boards.example/air"
        search = FakeSearchProvider(
            [SearchResult(url, title="Air Careers", snippet="Air open positions")]
        )
        fetcher = FakeFetcher(
            {
                url: FetchResult(
                    url,
                    url,
                    200,
                    "<title>Air Careers</title>Search jobs. Open positions. Apply now.",
                )
            }
        )
        service = PortalDiscoveryService(self.ledger, fetcher, search)

        results = service.discover(CompanyDiscoveryRequest("Air Canada"))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].identity_score, 0.5)
        self.assertFalse(results[0].promoted)

    def test_identity_conflict_is_rejected_and_not_retried(self):
        url = "https://unrelated.example/careers"
        search = FakeSearchProvider(
            [SearchResult(url, title="Unrelated Industries", snippet="Careers")]
        )
        fetcher = FakeFetcher(
            {
                url: FetchResult(
                    url,
                    url,
                    200,
                    "Unrelated Industries careers search jobs current openings",
                )
            }
        )
        service = PortalDiscoveryService(self.ledger, fetcher, search)
        request = CompanyDiscoveryRequest("Acme")

        first = service.discover(request)
        calls_after_first = len(fetcher.calls)
        second = service.discover(request)

        self.assertEqual(first[0].validation_status, "rejected")
        self.assertEqual(first[0].rejection_reason, "identity_conflict")
        self.assertEqual(second, [])
        self.assertEqual(len(fetcher.calls), calls_after_first)

    def test_search_results_are_cached(self):
        search = FakeSearchProvider([])
        service = PortalDiscoveryService(self.ledger, FakeFetcher({}), search)
        request = CompanyDiscoveryRequest("Acme")

        service.discover(request)
        service.discover(request)

        self.assertEqual(len(search.queries), 1)

    def test_old_validator_rejections_do_not_suppress_new_rules(self):
        company_id = CompanyDiscoveryRequest("Acme").company_id
        url = "https://acme.example/careers"
        with closing(sqlite3.connect(self.database)) as db:
            db.execute(
                """INSERT INTO negative_match
                       (company_id, canonical_url, reason, validator_version, created_at)
                   VALUES (?, ?, 'identity_conflict', '1', '2026-01-01')""",
                (company_id, url),
            )
            db.commit()

        self.assertFalse(self.ledger.is_rejected(company_id, url))

    def test_canonical_selection_prefers_careers_portal_over_faq(self):
        careers = "https://careers.aircanada.com/ca/en"
        faq = "https://careers.aircanada.com/ca/en/faqs"
        content = "Air Canada careers search jobs current openings join our team"
        search = FakeSearchProvider(
            [
                SearchResult(faq, title="Air Canada Careers FAQ"),
                SearchResult(careers, title="Air Canada Careers"),
            ]
        )
        fetcher = FakeFetcher(
            {
                careers: FetchResult(careers, careers, 200, content),
                faq: FetchResult(faq, faq, 200, content),
            }
        )
        service = PortalDiscoveryService(self.ledger, fetcher, search)

        results = service.discover(
            CompanyDiscoveryRequest(
                "Air Canada", official_domains=("https://aircanada.com",)
            )
        )

        promoted = [item for item in results if item.promoted]
        self.assertEqual(len(promoted), 1)
        self.assertEqual(promoted[0].canonical_url, careers)
        self.assertEqual(promoted[0].portal_type, "careers")

    def test_regional_third_party_job_boards_are_blacklisted(self):
        self.assertTrue(
            PortalDiscoveryService._blacklisted(
                "https://www.glassdoor.sg/Jobs/Example-Jobs"
            )
        )
        self.assertTrue(
            PortalDiscoveryService._blacklisted("https://hk.jobsdb.com/example-jobs")
        )

    def test_work_in_path_is_classified_as_careers(self):
        candidate = type(
            "Candidate",
            (),
            {
                "final_url": "https://example.com/work-in-example",
                "canonical_url": "https://example.com/work-in-example",
                "ats_vendor": "unknown",
            },
        )()

        self.assertEqual(PortalDiscoveryService._portal_type(candidate), "careers")

    def test_multiple_country_scopes_are_stored_independently(self):
        singapore = "https://acme.example/sg/careers"
        canada = "https://acme.example/ca/careers"
        pages = {
            singapore: FetchResult(
                singapore,
                singapore,
                200,
                "Acme Singapore careers search jobs current openings join our team",
            ),
            canada: FetchResult(
                canada,
                canada,
                200,
                "Acme Canada careers search jobs current openings join our team",
            ),
        }
        root = "https://acme.example"
        sitemap = f"{root}/sitemap.xml"
        singapore_pages = {
            **pages,
            root: FetchResult(root, root, 200, f'<a href="{singapore}">Careers</a>'),
            sitemap: FetchResult(sitemap, sitemap, 200, "<xml></xml>"),
        }
        canada_pages = {
            **pages,
            root: FetchResult(root, root, 200, f'<a href="{canada}">Careers</a>'),
            sitemap: FetchResult(sitemap, sitemap, 200, "<xml></xml>"),
        }
        singapore_service = PortalDiscoveryService(
            self.ledger,
            FakeFetcher(singapore_pages),
            FakeSearchProvider([SearchResult(singapore, title="Acme Singapore Careers")]),
        )
        canada_service = PortalDiscoveryService(
            self.ledger,
            FakeFetcher(canada_pages),
            FakeSearchProvider([SearchResult(canada, title="Acme Canada Careers")]),
        )

        singapore_service.discover(
            CompanyDiscoveryRequest(
                "Acme", official_domains=(root,), country="Singapore"
            )
        )
        canada_service.discover(
            CompanyDiscoveryRequest("Acme", official_domains=(root,), country="Canada")
        )

        with closing(sqlite3.connect(self.database)) as db:
            scopes = db.execute(
                "SELECT DISTINCT country FROM portal_selection ORDER BY country"
            ).fetchall()
        self.assertEqual(scopes, [("Canada",), ("Singapore",)])


if __name__ == "__main__":
    unittest.main()
