import unittest
from unittest.mock import patch

from app.classifier import (
    AccountClassification,
    EvidenceItem,
    LlmEvidenceClassifier,
    OpenAICompatibleLlmClient,
)


class FakeSearchClient:
    def __init__(self, results):
        self.results = results
        self.queries = []

    def search(self, query, limit=5):
        self.queries.append((query, limit))
        return self.results


class FailingSearchClient:
    def search(self, query, limit=5):
        raise RuntimeError("HTTP Error 403: Forbidden")


class FakeLlmClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def classify(self, account_name, evidence):
        self.calls.append((account_name, evidence))
        return self.response


class LlmEvidenceClassifierTests(unittest.TestCase):
    def test_classifies_public_entity_from_evidence(self):
        search = FakeSearchClient(
            [
                EvidenceItem(
                    title="Company profile",
                    url="https://example.com/profile",
                    snippet="The company is controlled by a state-owned parent and operates globally.",
                )
            ]
        )
        llm = FakeLlmClient(
            {
                "account_types": [
                    "State-owned Enterprise(SOE)",
                    "Multinational Corporation(MNC)",
                ],
                "confidence": "High",
                "reason": "Public evidence shows state ownership and international operations.",
            }
        )

        result = LlmEvidenceClassifier(search, llm).classify("Example Energy")

        self.assertEqual(
            result.account_types,
            ["State-owned Enterprise(SOE)", "Multinational Corporation(MNC)"],
        )
        self.assertEqual(result.confidence, "High")
        self.assertIn("state ownership", result.reason)
        self.assertEqual(result.evidence_urls, ["https://example.com/profile"])
        self.assertEqual(search.queries, [("Example Energy account ownership organization type", 5)])

    def test_uses_llm_fallback_when_no_evidence_is_found(self):
        llm = FakeLlmClient(
            {
                "account_types": ["Multinational Corporation(MNC)"],
                "confidence": "Medium",
                "reason": "Mitsui is generally known as a diversified international trading company.",
            }
        )

        result = LlmEvidenceClassifier(FakeSearchClient([]), llm).classify(
            "Mitsui & Co., Ltd."
        )

        self.assertEqual(result.account_types, ["Multinational Corporation(MNC)"])
        self.assertEqual(result.confidence, "Medium")
        self.assertIn("LLM fallback - no public evidence found", result.reason)
        self.assertEqual(result.evidence_urls, [])
        self.assertEqual(llm.calls, [("Mitsui & Co., Ltd.", [])])

    def test_marks_needs_review_when_search_provider_rejects_request(self):
        result = LlmEvidenceClassifier(FailingSearchClient(), FakeLlmClient({})).classify(
            "Blocked Search Account"
        )

        self.assertEqual(result.account_types, ["Needs Review"])
        self.assertEqual(result.confidence, "Low")
        self.assertIn("Search failed", result.reason)

    def test_invalid_llm_types_fall_back_to_needs_review(self):
        search = FakeSearchClient(
            [
                EvidenceItem(
                    title="Ambiguous",
                    url="https://example.com/ambiguous",
                    snippet="A short ambiguous search result.",
                )
            ]
        )
        llm = FakeLlmClient(
            {
                "account_types": ["Bank", "Government Organization(GO)"],
                "confidence": "Medium",
                "reason": "One valid and one invalid label.",
            }
        )

        result = LlmEvidenceClassifier(search, llm).classify("Mixed Account")

        self.assertEqual(result.account_types, ["Government Organization(GO)"])
        self.assertEqual(result.confidence, "Medium")

    def test_openai_compatible_client_defaults_to_deepseek(self):
        with patch.dict(
            "os.environ",
            {"DEEPSEEK_API_KEY": "test-key"},
            clear=True,
        ):
            client = OpenAICompatibleLlmClient()

        self.assertEqual(client.api_key, "test-key")
        self.assertEqual(client.model, "deepseek-v4-flash")
        self.assertEqual(client.endpoint_url, "https://api.deepseek.com/chat/completions")


if __name__ == "__main__":
    unittest.main()
