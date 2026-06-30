import unittest
from unittest.mock import patch

from app.classifier import (
    AccountClassification,
    EvidenceItem,
    LlmEvidenceClassifier,
    OpenAICompatibleLlmClient,
)
from app import classifier


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
            ["State-owned Enterprise(SOE)"],
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
        self.assertEqual(result.review_status, "Needs Review")
        self.assertEqual(result.confidence, "Medium")
        self.assertIn("LLM fallback - no public evidence found", result.reason)
        self.assertEqual(result.evidence_urls, [])
        self.assertEqual(llm.calls, [("Mitsui & Co., Ltd.", [])])

    def test_low_confidence_classification_keeps_recommendation_and_marks_review(self):
        search = FakeSearchClient(
            [
                EvidenceItem(
                    title="Company profile",
                    url="https://example.com/private-profile",
                    snippet="A private company profile with limited ownership details.",
                )
            ]
        )
        llm = FakeLlmClient(
            {
                "account_types": ["Private Enterprise(POE)"],
                "confidence": "Low",
                "reason": "Evidence is thin but suggests private ownership.",
            }
        )

        result = LlmEvidenceClassifier(search, llm).classify("Thin Evidence Co")

        self.assertEqual(result.account_types, ["Private Enterprise(POE)"])
        self.assertEqual(result.review_status, "Needs Review")
        self.assertEqual(result.confidence, "Low")

    def test_private_enterprise_aliases_normalize_to_poe(self):
        search = FakeSearchClient(
            [
                EvidenceItem(
                    title="Company profile",
                    url="https://example.com/private-company",
                    snippet="A privately owned company headquartered in China.",
                )
            ]
        )
        aliases = [
            "Private Enterprise",
            "POE",
            "Private Enterprise (POE)",
            "Private Enterprise（POE）",
            "私营企业",
            "民营企业",
        ]

        for alias in aliases:
            with self.subTest(alias=alias):
                llm = FakeLlmClient(
                    {
                        "account_types": [alias],
                        "confidence": "High",
                        "reason": "Evidence shows a privately owned enterprise.",
                    }
                )

                result = LlmEvidenceClassifier(search, llm).classify("Private Alias Co")

                self.assertEqual(result.account_types, ["Private Enterprise(POE)"])

    def test_reason_private_enterprise_corrects_other_with_review(self):
        search = FakeSearchClient(
            [
                EvidenceItem(
                    title="GreenTree Hospitality",
                    url="https://example.com/greentree",
                    snippet="GreenTree Hospitality is a China-based hotel group headquartered in Shanghai.",
                )
            ]
        )
        llm = FakeLlmClient(
            {
                "account_types": ["Other"],
                "confidence": "Medium",
                "reason": (
                    "证据显示公司名称为GreenTree Hospitality (China) Ltd.，中文名称为格林酒店集团，"
                    "总部位于上海，是一家中国酒店集团。没有证据表明是政府机构、国有企业、"
                    "跨国公司（主要业务在中国）、合资企业、非营利组织或其他类型，因此归类为私营企业。"
                ),
            }
        )

        result = LlmEvidenceClassifier(search, llm).classify(
            "GreenTree Hospitality (China) Ltd."
        )

        self.assertEqual(result.account_types, ["Private Enterprise(POE)"])
        self.assertEqual(result.review_status, "Needs Review")

    def test_marks_needs_review_when_search_provider_rejects_request(self):
        result = LlmEvidenceClassifier(FailingSearchClient(), FakeLlmClient({})).classify(
            "Blocked Search Account"
        )

        self.assertEqual(result.account_types, ["Other"])
        self.assertEqual(result.review_status, "Needs Review")
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

        self.assertEqual(result.account_types, ["State-owned Enterprise(SOE)"])
        self.assertEqual(result.confidence, "Medium")

    def test_normalizes_go_and_private_to_state_owned_enterprise(self):
        search = FakeSearchClient(
            [
                EvidenceItem(
                    title="Government profile",
                    url="https://example.com/government-profile",
                    snippet="A government organization with private commercial operations.",
                )
            ]
        )
        llm = FakeLlmClient(
            {
                "account_types": [
                    "Government Organization(GO)",
                    "Private Enterprise(POE)",
                ],
                "confidence": "High",
                "reason": "Evidence suggests both public-sector ownership and private operations.",
            }
        )

        result = LlmEvidenceClassifier(search, llm).classify("Public Private Account")

        self.assertEqual(result.account_types, ["State-owned Enterprise(SOE)"])

    def test_normalizes_jv_and_ngo_to_other(self):
        search = FakeSearchClient(
            [
                EvidenceItem(
                    title="Organization profile",
                    url="https://example.com/organization-profile",
                    snippet="A joint venture supporting nonprofit programs.",
                )
            ]
        )
        llm = FakeLlmClient(
            {
                "account_types": [
                    "Joint Venture(JV)",
                    "NGO / Non-profit Organization",
                ],
                "confidence": "Medium",
                "reason": "Evidence suggests a joint venture and nonprofit program scope.",
            }
        )

        result = LlmEvidenceClassifier(search, llm).classify("JV NGO Account")

        self.assertEqual(result.account_types, ["Other"])

    def test_mnc_wins_over_private_and_other(self):
        search = FakeSearchClient(
            [
                EvidenceItem(
                    title="Company profile",
                    url="https://example.com/company-profile",
                    snippet="A privately held company operating across multiple countries.",
                )
            ]
        )
        llm = FakeLlmClient(
            {
                "account_types": [
                    "Private Enterprise(POE)",
                    "Other",
                    "Multinational Corporation(MNC)",
                ],
                "confidence": "High",
                "reason": "Evidence supports private ownership and multinational operations.",
            }
        )

        result = LlmEvidenceClassifier(search, llm).classify("Global Private Account")

        self.assertEqual(result.account_types, ["Multinational Corporation(MNC)"])

    def test_chinese_private_mnc_outputs_private_enterprise(self):
        search = FakeSearchClient(
            [
                EvidenceItem(
                    title="Company profile",
                    url="https://example.com/chinese-private-mnc",
                    snippet="A China-based privately owned company with operations in multiple countries.",
                )
            ]
        )
        llm = FakeLlmClient(
            {
                "account_types": [
                    "Multinational Corporation(MNC)",
                    "Private Enterprise(POE)",
                ],
                "is_chinese_company": True,
                "confidence": "High",
                "reason": "Evidence shows a Chinese private company with multinational operations.",
            }
        )

        result = LlmEvidenceClassifier(search, llm).classify("Chinese Private Global")

        self.assertEqual(result.account_types, ["Private Enterprise(POE)"])

    def test_chinese_mnc_only_outputs_private_enterprise(self):
        search = FakeSearchClient(
            [
                EvidenceItem(
                    title="Company profile",
                    url="https://example.com/yutong",
                    snippet=(
                        "Zhengzhou Yutong Bus Co., Ltd. is a Chinese listed "
                        "company headquartered in Zhengzhou, China and a leading "
                        "global bus manufacturer."
                    ),
                )
            ]
        )
        llm = FakeLlmClient(
            {
                "account_types": ["Multinational Corporation(MNC)"],
                "is_chinese_company": True,
                "confidence": "High",
                "reason": (
                    "Evidence shows it is a Chinese listed company that is a "
                    "leading global bus manufacturer, headquartered in Zhengzhou, "
                    "China. It is publicly traded and operates internationally, "
                    "fitting the MNC label."
                ),
            }
        )

        result = LlmEvidenceClassifier(search, llm).classify("Zhengzhou Yutong Bus Co., Ltd.")

        self.assertEqual(result.account_types, ["Private Enterprise(POE)"])

    def test_non_chinese_private_mnc_outputs_multinational_corporation(self):
        search = FakeSearchClient(
            [
                EvidenceItem(
                    title="Company profile",
                    url="https://example.com/non-chinese-private-mnc",
                    snippet="A privately owned company headquartered outside China with global operations.",
                )
            ]
        )
        llm = FakeLlmClient(
            {
                "account_types": [
                    "Multinational Corporation(MNC)",
                    "Private Enterprise(POE)",
                ],
                "is_chinese_company": False,
                "confidence": "High",
                "reason": "Evidence shows private ownership and multinational operations.",
            }
        )

        result = LlmEvidenceClassifier(search, llm).classify("Non Chinese Private Global")

        self.assertEqual(result.account_types, ["Multinational Corporation(MNC)"])

    def test_chinese_state_owned_mnc_still_outputs_state_owned_enterprise(self):
        search = FakeSearchClient(
            [
                EvidenceItem(
                    title="Company profile",
                    url="https://example.com/chinese-state-mnc",
                    snippet="A China-based state-owned company with global operations.",
                )
            ]
        )
        llm = FakeLlmClient(
            {
                "account_types": [
                    "State-owned Enterprise(SOE)",
                    "Multinational Corporation(MNC)",
                    "Private Enterprise(POE)",
                ],
                "is_chinese_company": True,
                "confidence": "High",
                "reason": "Evidence shows state ownership and multinational operations.",
            }
        )

        result = LlmEvidenceClassifier(search, llm).classify("Chinese State Global")

        self.assertEqual(result.account_types, ["State-owned Enterprise(SOE)"])

    def test_missing_chinese_company_flag_defaults_to_non_chinese_priority(self):
        search = FakeSearchClient(
            [
                EvidenceItem(
                    title="Company profile",
                    url="https://example.com/missing-chinese-flag",
                    snippet="A privately owned company with global operations.",
                )
            ]
        )
        llm = FakeLlmClient(
            {
                "account_types": [
                    "Multinational Corporation(MNC)",
                    "Private Enterprise(POE)",
                ],
                "confidence": "Medium",
                "reason": "Legacy response without the Chinese company flag.",
            }
        )

        result = LlmEvidenceClassifier(search, llm).classify("Legacy Private Global")

        self.assertEqual(result.account_types, ["Multinational Corporation(MNC)"])

    def test_invalid_only_llm_types_fall_back_to_needs_review(self):
        search = FakeSearchClient(
            [
                EvidenceItem(
                    title="Ambiguous",
                    url="https://example.com/invalid-only",
                    snippet="A short ambiguous search result.",
                )
            ]
        )
        llm = FakeLlmClient(
            {
                "account_types": ["Bank", "University"],
                "confidence": "Medium",
                "reason": "Only unsupported labels were returned.",
            }
        )

        result = LlmEvidenceClassifier(search, llm).classify("Invalid Account")

        self.assertEqual(result.account_types, ["Other"])
        self.assertEqual(result.review_status, "Needs Review")

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

    def test_normalizes_existing_account_type_labels(self):
        cases = {
            "Specialized Enterprise (SE/央企)": "State-owned Enterprise(SOE)",
            "State-Owned Enterprise (SOE)": "State-owned Enterprise(SOE)",
            "Governmental Organization (GO)": "State-owned Enterprise(SOE)",
            "Private Entity（POE）": "Private Enterprise(POE)",
            "Multinational Corporation（MNC）": "Multinational Corporation(MNC)",
            "Non-Profit Organization (NPO)": "Other",
            "Joint Venture": "Other",
            "Government Organization(GO)": "State-owned Enterprise(SOE)",
            "Private Enterprise(POE)": "Private Enterprise(POE)",
            "NGO / Non-profit Organization": "Other",
        }

        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(
                    classifier.normalize_existing_account_type(raw),
                    expected,
                )

    def test_existing_account_type_priority_selects_single_output(self):
        self.assertEqual(
            classifier.normalize_existing_account_type(
                "Private Entity（POE）; Multinational Corporation（MNC）"
            ),
            "Multinational Corporation(MNC)",
        )
        self.assertEqual(
            classifier.normalize_existing_account_type(
                "Joint Venture; State-Owned Enterprise (SOE); Multinational Corporation（MNC）"
            ),
            "State-owned Enterprise(SOE)",
        )

    def test_existing_account_type_unknown_returns_empty_text(self):
        self.assertEqual(classifier.normalize_existing_account_type("Bank"), "")

    def test_account_type_short_code_maps_final_types_only(self):
        cases = {
            "State-owned Enterprise(SOE)": "SOE",
            "Private Enterprise(POE)": "POE",
            "Multinational Corporation(MNC)": "MNC",
            "Other": "Other",
            "Needs Review": "",
            "": "",
            "Bank": "",
        }

        for account_type, expected in cases.items():
            with self.subTest(account_type=account_type):
                self.assertEqual(
                    classifier.account_type_short_code(account_type),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
