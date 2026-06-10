from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from app.classifier import EvidenceItem


SEARCH_TIMEOUT_SECONDS = 10


class WebSearchClient:
    def __init__(
        self,
        tavily_api_key: str | None = None,
        serpapi_api_key: str | None = None,
        bing_search_api_key: str | None = None,
        allow_public_fallback: bool = False,
    ):
        self.tavily_api_key = tavily_api_key or os.getenv("TAVILY_API_KEY")
        self.serpapi_api_key = serpapi_api_key or os.getenv("SERPAPI_API_KEY")
        self.bing_search_api_key = bing_search_api_key or os.getenv("BING_SEARCH_API_KEY")
        self.allow_public_fallback = allow_public_fallback

    def search(self, query: str, limit: int = 5) -> list[EvidenceItem]:
        providers = []
        if self.tavily_api_key:
            providers.append(self._search_tavily)
        if self.serpapi_api_key:
            providers.append(self._search_serpapi)
        if self.bing_search_api_key:
            providers.append(self._search_bing)
        if self.allow_public_fallback:
            providers.extend([self._search_bing_html, self._search_duckduckgo])

        for provider in providers:
            try:
                results = provider(query, limit)
            except (OSError, RuntimeError, urllib.error.URLError, urllib.error.HTTPError):
                continue
            if results:
                return results[:limit]
        return []

    def _search_tavily(self, query: str, limit: int) -> list[EvidenceItem]:
        payload = {
            "api_key": self.tavily_api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": limit,
        }
        data = _post_json("https://api.tavily.com/search", payload)
        return [
            EvidenceItem(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
            )
            for item in data.get("results", [])
        ][:limit]

    def _search_serpapi(self, query: str, limit: int) -> list[EvidenceItem]:
        params = urllib.parse.urlencode(
            {
                "engine": "google",
                "q": query,
                "api_key": self.serpapi_api_key,
                "num": limit,
            }
        )
        data = _get_json(f"https://serpapi.com/search.json?{params}")
        return [
            EvidenceItem(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
            )
            for item in data.get("organic_results", [])
        ][:limit]

    def _search_bing(self, query: str, limit: int) -> list[EvidenceItem]:
        params = urllib.parse.urlencode({"q": query, "count": limit})
        request = urllib.request.Request(
            f"https://api.bing.microsoft.com/v7.0/search?{params}",
            headers={"Ocp-Apim-Subscription-Key": self.bing_search_api_key},
        )
        with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [
            EvidenceItem(
                title=item.get("name", ""),
                url=item.get("url", ""),
                snippet=item.get("snippet", ""),
            )
            for item in data.get("webPages", {}).get("value", [])
        ][:limit]

    def _search_bing_html(self, query: str, limit: int) -> list[EvidenceItem]:
        params = urllib.parse.urlencode({"q": query})
        request = urllib.request.Request(
            f"https://www.bing.com/search?{params}",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            },
        )
        with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT_SECONDS) as response:
            text = response.read().decode("utf-8", errors="replace")
        results = []
        pattern = re.compile(
            r'<li class="b_algo".*?<h2>.*?<a href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
            r'(?:<p>(?P<snippet>.*?)</p>)',
            re.DOTALL,
        )
        for match in pattern.finditer(text):
            results.append(
                EvidenceItem(
                    title=_clean_html(match.group("title")),
                    url=html.unescape(match.group("url")),
                    snippet=_clean_html(match.group("snippet") or ""),
                )
            )
            if len(results) >= limit:
                break
        return results

    def _search_duckduckgo(self, query: str, limit: int) -> list[EvidenceItem]:
        params = urllib.parse.urlencode({"q": query})
        request = urllib.request.Request(
            f"https://duckduckgo.com/html/?{params}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT_SECONDS) as response:
            text = response.read().decode("utf-8", errors="replace")
        results = []
        pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
            r'<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
            re.DOTALL,
        )
        for match in pattern.finditer(text):
            url = html.unescape(match.group("url"))
            if "uddg=" in url:
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                url = parsed.get("uddg", [url])[0]
            results.append(
                EvidenceItem(
                    title=_clean_html(match.group("title")),
                    url=url,
                    snippet=_clean_html(match.group("snippet")),
                )
            )
            if len(results) >= limit:
                break
        return results


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=SEARCH_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _clean_html(value: str) -> str:
    text = re.sub(r"<.*?>", "", value, flags=re.DOTALL)
    return html.unescape(text).strip()
