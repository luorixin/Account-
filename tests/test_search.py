import unittest
from unittest.mock import patch

from app.classifier import EvidenceItem
from app.search import WebSearchClient


class WebSearchClientTests(unittest.TestCase):
    def test_uses_request_scoped_tavily_key_before_domestic_fallbacks(self):
        client = WebSearchClient(tavily_api_key="test-tavily", allow_public_fallback=True)

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(
                client,
                "_search_tavily",
                return_value=[EvidenceItem("Tavily", "https://example.com/t", "Snippet")],
            ) as tavily,
            patch.object(client, "_search_baidu_html", create=True) as baidu_html,
        ):
            results = client.search("Example Account", limit=3)

        self.assertEqual(results, [EvidenceItem("Tavily", "https://example.com/t", "Snippet")])
        tavily.assert_called_once_with("Example Account", 3)
        baidu_html.assert_not_called()

    def test_tries_domestic_public_search_fallbacks_in_order_without_api_keys(self):
        client = WebSearchClient(allow_public_fallback=True)

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(client, "_search_baidu_html", create=True, return_value=[]) as baidu,
            patch.object(
                client,
                "_search_sogou_html",
                create=True,
                return_value=[EvidenceItem("Sogou", "https://example.com/s", "Snippet")],
            ) as sogou,
            patch.object(client, "_search_so360_html", create=True) as so360,
        ):
            results = client.search("Example Account", limit=3)

        self.assertEqual(results, [EvidenceItem("Sogou", "https://example.com/s", "Snippet")])
        baidu.assert_called_once_with("Example Account", 3)
        sogou.assert_called_once_with("Example Account", 3)
        so360.assert_not_called()

    def test_uses_baidu_results_before_later_domestic_fallbacks(self):
        client = WebSearchClient(allow_public_fallback=True)

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(
                client,
                "_search_baidu_html",
                create=True,
                return_value=[EvidenceItem("Baidu", "https://example.com/b", "Snippet")],
            ) as baidu,
            patch.object(client, "_search_sogou_html", create=True) as sogou,
            patch.object(client, "_search_so360_html", create=True) as so360,
        ):
            results = client.search("Example Account", limit=3)

        self.assertEqual(results, [EvidenceItem("Baidu", "https://example.com/b", "Snippet")])
        baidu.assert_called_once_with("Example Account", 3)
        sogou.assert_not_called()
        so360.assert_not_called()

    def test_uses_domestic_fallbacks_when_tavily_fails(self):
        client = WebSearchClient(tavily_api_key="test-tavily", allow_public_fallback=True)

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(client, "_search_tavily", side_effect=RuntimeError("503")) as tavily,
            patch.object(
                client,
                "_search_baidu_html",
                create=True,
                return_value=[EvidenceItem("Baidu", "https://example.com/b", "Snippet")],
            ) as baidu,
        ):
            results = client.search("Example Account", limit=3)

        self.assertEqual(results, [EvidenceItem("Baidu", "https://example.com/b", "Snippet")])
        tavily.assert_called_once_with("Example Account", 3)
        baidu.assert_called_once_with("Example Account", 3)

    def test_uses_domestic_fallbacks_when_tavily_returns_no_results(self):
        client = WebSearchClient(tavily_api_key="test-tavily", allow_public_fallback=True)

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(client, "_search_tavily", return_value=[]) as tavily,
            patch.object(
                client,
                "_search_baidu_html",
                create=True,
                return_value=[EvidenceItem("Baidu", "https://example.com/b", "Snippet")],
            ) as baidu,
        ):
            results = client.search("Example Account", limit=3)

        self.assertEqual(results, [EvidenceItem("Baidu", "https://example.com/b", "Snippet")])
        tavily.assert_called_once_with("Example Account", 3)
        baidu.assert_called_once_with("Example Account", 3)

    def test_returns_empty_results_when_all_search_providers_fail(self):
        client = WebSearchClient(allow_public_fallback=True)

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(client, "_search_baidu_html", create=True, side_effect=RuntimeError("403")),
            patch.object(client, "_search_sogou_html", create=True, side_effect=RuntimeError("403")),
            patch.object(client, "_search_so360_html", create=True, side_effect=RuntimeError("403")),
        ):
            results = client.search("Example Account", limit=3)

        self.assertEqual(results, [])

    def test_returns_empty_results_without_api_keys_by_default(self):
        client = WebSearchClient()

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(client, "_search_baidu_html", create=True) as baidu_html,
            patch.object(client, "_search_sogou_html", create=True) as sogou_html,
            patch.object(client, "_search_so360_html", create=True) as so360_html,
        ):
            results = client.search("Example Account", limit=3)

        self.assertEqual(results, [])
        baidu_html.assert_not_called()
        sogou_html.assert_not_called()
        so360_html.assert_not_called()


if __name__ == "__main__":
    unittest.main()
