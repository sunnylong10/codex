import unittest
from unittest.mock import patch

import discover_ats
from ats_detection import (
    DetectionContext,
    GreenhousePlugin,
    LeverPlugin,
    OraclePlugin,
    SmartRecruitersPlugin,
    SuccessFactorsPlugin,
    UnknownPlugin,
    VerificationResponse,
    WorkdayPlugin,
    default_registry,
)


class FakeVerificationClient:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.response


class ATSDetectionTest(unittest.TestCase):
    def test_default_registry_uses_concrete_vendor_plugins(self):
        expected_types = {
            WorkdayPlugin,
            GreenhousePlugin,
            LeverPlugin,
            OraclePlugin,
            SuccessFactorsPlugin,
            SmartRecruitersPlugin,
        }

        self.assertEqual({type(plugin) for plugin in default_registry.detectors}, expected_types)
        self.assertIsInstance(default_registry.unknown_detector, UnknownPlugin)

    def test_workday_url_extracts_tenant_and_board(self):
        results = default_registry.detect(
            DetectionContext(
                original_url="https://acme.wd3.myworkdayjobs.com/External_Careers"
            )
        )

        self.assertEqual(results[0].vendor, "workday")
        self.assertEqual(results[0].tenant_id, "acme")
        self.assertEqual(results[0].board_id, "External_Careers")
        self.assertGreaterEqual(results[0].confidence_score, 0.8)
        self.assertTrue(results[0].is_primary)

    def test_sap_extracts_company_parameter(self):
        results = default_registry.detect(
            DetectionContext(
                original_url=(
                    "https://career5.successfactors.eu/career?company=acme&lang=en_US"
                )
            )
        )

        self.assertEqual(results[0].vendor, "sap_successfactors")
        self.assertEqual(results[0].tenant_id, "acme")

    def test_multiple_ats_evidence_is_preserved_and_marked_conflicting(self):
        html = """
            <a href="https://boards.greenhouse.io/acme">Corporate jobs</a>
            <a href="https://jobs.lever.co/acme-labs">Labs jobs</a>
        """
        results = default_registry.detect(
            DetectionContext(original_url="https://acme.example/careers", html=html)
        )

        self.assertEqual({result.vendor for result in results}, {"greenhouse", "lever"})
        self.assertTrue(all(result.conflicting for result in results))

    def test_custom_is_positive_classification_and_unknown_is_unresolved(self):
        custom = default_registry.detect(
            DetectionContext(
                original_url="https://acme.example/careers",
                html=(
                    '<script type="application/ld+json">JobPosting</script>'
                    '<a class="apply now">Current openings</a>'
                ),
            )
        )
        unknown = default_registry.detect(
            DetectionContext(
                original_url="https://acme.example/about", html="<h1>About us</h1>"
            )
        )

        self.assertEqual(custom[0].vendor, "custom")
        self.assertGreater(custom[0].confidence_score, 0)
        self.assertEqual(unknown[0].vendor, "unknown")
        self.assertEqual(unknown[0].confidence_score, 0)

    def test_network_evidence_has_high_confidence(self):
        results = default_registry.detect(
            DetectionContext(
                original_url="https://careers.acme.example",
                network_urls=("https://boards.greenhouse.io/acme",),
            )
        )

        self.assertEqual(results[0].vendor, "greenhouse")
        self.assertEqual(results[0].evidence[0].evidence_type, "network")
        self.assertGreaterEqual(results[0].confidence_score, 0.9)

    def test_discovery_api_verification_is_recorded_as_evidence(self):
        url = "https://acme.wd3.myworkdayjobs.com/External"
        with patch.object(discover_ats, "_cxs_probe", return_value=(200, 0)):
            hit = discover_ats._hit_from_ats_url("Acme", "acme", url)

        self.assertEqual(hit["ats"], "workday")
        self.assertEqual(hit["jobs"], 0)
        self.assertEqual(hit["ats_confidence"], 1.0)
        self.assertEqual(hit["ats_verification_status"], "verified")
        self.assertIn("api_verified", hit["ats_evidence"])

    def test_registry_verifies_through_uniform_plugin_interface(self):
        context = DetectionContext(
            original_url="https://boards.greenhouse.io/acme"
        )
        detected = default_registry.detect(context)
        client = FakeVerificationClient(VerificationResponse(200, json_data={}))

        verified = default_registry.verify(detected, context, client)

        self.assertEqual(verified[0].verification_status, "verified")
        self.assertEqual(verified[0].confidence_score, 1.0)
        self.assertTrue(verified[0].evidence[-1].verified)
        self.assertIn("boards-api.greenhouse.io", client.requests[0][1])

    def test_retryable_verification_failure_preserves_detection(self):
        context = DetectionContext(
            original_url="https://jobs.lever.co/acme"
        )
        detected = default_registry.detect(context)
        client = FakeVerificationClient(
            VerificationResponse(429, error_code="rate_limited", retryable=True)
        )

        verified = default_registry.verify(detected, context, client)

        self.assertEqual(verified[0].vendor, "lever")
        self.assertEqual(verified[0].verification_status, "temporarily_unavailable")
        self.assertEqual(verified[0].error_code, "rate_limited")
        self.assertTrue(verified[0].retryable)


if __name__ == "__main__":
    unittest.main()
