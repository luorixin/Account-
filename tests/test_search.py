import unittest
from unittest.mock import patch

from app.classifier import EvidenceItem
from app.search import WebSearchClient


class WebSearchClientTests(unittest.TestCase):
    def test_uses_request_scoped_tavily_key_before_public_fallbacks(self):
        client = WebSearchClient(tavily_api_key="test-tavily")

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(
                client,
                "_search_tavily",
                return_value=[EvidenceItem("Tavily", "https://example.com/t", "Snippet")],
            ) as tavily,
            patch.object(client, "_search_bing_html") as bing_html,
        ):
            results = client.search("Example Account", limit=3)

        self.assertEqual(results, [EvidenceItem("Tavily", "https://example.com/t", "Snippet")])
        tavily.assert_called_once_with("Example Account", 3)
        bing_html.assert_not_called()

    def test_tries_public_search_fallbacks_when_first_provider_fails(self):
        client = WebSearchClient()

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(client, "_search_bing_html", side_effect=RuntimeError("403")),
            patch.object(
                client,
                "_search_duckduckgo",
                return_value=[EvidenceItem("Title", "https://example.com", "Snippet")],
            ),
        ):
            results = client.search("Example Account", limit=3)

        self.assertEqual(results, [EvidenceItem("Title", "https://example.com", "Snippet")])

    def test_returns_empty_results_when_all_search_providers_fail(self):
        client = WebSearchClient()

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(client, "_search_bing_html", side_effect=RuntimeError("403")),
            patch.object(client, "_search_duckduckgo", side_effect=RuntimeError("403")),
        ):
            results = client.search("Example Account", limit=3)

        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
