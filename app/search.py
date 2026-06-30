"""
网页搜索模块 (app.search)

本模块负责依据优先级进行网页证据检索。
搜索接口优先级：
1. Tavily Search API
2. SerpAPI (Google)
3. Bing Search API
4. 国内公开网页搜索引擎爬虫兜底 (Baidu -> Sogou -> 360)
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from app.classifier import EvidenceItem


# 搜索接口的默认超时时间（秒）
SEARCH_TIMEOUT_SECONDS = 10


class WebSearchClient:
    """
    统一的网络检索客户端。根据当前可用的 API Key 按优先级依次检索，
    在没有配置任何 Key 或者 API 失败时，如果允许，会进入公开搜索引擎 HTML 爬取阶段。
    """
    def __init__(
        self,
        tavily_api_key: str | None = None,
        serpapi_api_key: str | None = None,
        bing_search_api_key: str | None = None,
        allow_public_fallback: bool = False,
    ):
        """
        初始化搜索客户端，API Key 优先采用实例化传入的临时 Key，无临时 Key 时读取系统环境变量。
        """
        self.tavily_api_key = tavily_api_key or os.getenv("TAVILY_API_KEY")
        self.serpapi_api_key = serpapi_api_key or os.getenv("SERPAPI_API_KEY")
        self.bing_search_api_key = bing_search_api_key or os.getenv("BING_SEARCH_API_KEY")
        self.allow_public_fallback = allow_public_fallback

    def search(self, query: str, limit: int = 5) -> list[EvidenceItem]:
        """
        统一搜索入口，按优先级调度底层搜索引擎，返回检索到的证据列表。
        
        Args:
            query: 搜索查询词
            limit: 最大返回结果条数
            
        Returns:
            list[EvidenceItem]: 网页结果的证据实体列表
        """
        providers = []
        # 按优先级注册搜索来源
        if self.tavily_api_key:
            providers.append(self._search_tavily)
        if self.serpapi_api_key:
            providers.append(self._search_serpapi)
        if self.bing_search_api_key:
            providers.append(self._search_bing)
        # 如果开启了公开爬虫兜底，注册国内三大搜索引擎 HTML 爬取
        if self.allow_public_fallback:
            providers.extend(
                [
                    self._search_baidu_html,
                    self._search_sogou_html,
                    self._search_so360_html,
                ]
            )

        # 遍历执行搜索引擎，直到某一个检索源返回非空结果
        for provider in providers:
            try:
                results = provider(query, limit)
            except (OSError, RuntimeError, urllib.error.URLError, urllib.error.HTTPError):
                # 捕获网络或 API 错误，静默重试下一个检索源
                continue
            if results:
                return results[:limit]
        return []

    def _search_tavily(self, query: str, limit: int) -> list[EvidenceItem]:
        """
        调用 Tavily 搜索服务。
        """
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
        """
        调用 SerpAPI (Google 搜索镜像) 服务。
        """
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
        """
        调用微软 Bing Web Search API 服务。
        """
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

    def _search_baidu_html(self, query: str, limit: int) -> list[EvidenceItem]:
        """
        直接请求百度并使用正则表达式抓取清洗 HTML 页面以提取基本证据。
        """
        params = urllib.parse.urlencode({"wd": query})
        request = urllib.request.Request(
            f"https://www.baidu.com/s?{params}",
            headers=_browser_headers(),
        )
        with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT_SECONDS) as response:
            text = response.read().decode("utf-8", errors="replace")

        results = []
        # 搜索结果卡片 block
        block_pattern = re.compile(
            r'<div[^>]+(?:class="[^"]*(?:result|c-container)[^"]*"|tpl="[^"]+")[^>]*>'
            r"(?P<block>.*?)"
            r"(?=<div[^>]+(?:class=\"[^\"]*(?:result|c-container)[^\"]*\"|tpl=\"[^\"]+\")|</body>)",
            re.DOTALL,
        )
        # 链接和标题
        link_pattern = re.compile(
            r"<h3[^>]*>.*?<a[^>]+href=\"(?P<url>[^\"]+)\"[^>]*>(?P<title>.*?)</a>",
            re.DOTALL,
        )
        # 网页抽象摘要
        snippet_pattern = re.compile(
            r'<(?:div|span|p)[^>]+class="[^"]*(?:c-abstract|content-right|c-span-last|result-op)[^"]*"[^>]*>'
            r"(?P<snippet>.*?)"
            r"</(?:div|span|p)>",
            re.DOTALL,
        )
        for match in block_pattern.finditer(text):
            block = match.group("block")
            link = link_pattern.search(block)
            if not link:
                continue
            snippet = snippet_pattern.search(block)
            results.append(
                EvidenceItem(
                    title=_clean_html(link.group("title")),
                    url=html.unescape(link.group("url")),
                    snippet=_clean_html(snippet.group("snippet") if snippet else block),
                )
            )
            if len(results) >= limit:
                break
        return results

    def _search_sogou_html(self, query: str, limit: int) -> list[EvidenceItem]:
        """
        直接请求搜狗搜索并使用正则表达式抓取 HTML 提取基本证据。
        """
        params = urllib.parse.urlencode({"query": query})
        request = urllib.request.Request(
            f"https://www.sogou.com/web?{params}",
            headers=_browser_headers(),
        )
        with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT_SECONDS) as response:
            text = response.read().decode("utf-8", errors="replace")
        results = []
        block_pattern = re.compile(
            r'<div[^>]+class="[^"]*(?:vrwrap|results?)[^"]*"[^>]*>'
            r"(?P<block>.*?)"
            r"(?=<div[^>]+class=\"[^\"]*(?:vrwrap|results?)[^\"]*\"|</body>)",
            re.DOTALL,
        )
        link_pattern = re.compile(
            r"<h3[^>]*>.*?<a[^>]+href=\"(?P<url>[^\"]+)\"[^>]*>(?P<title>.*?)</a>",
            re.DOTALL,
        )
        snippet_pattern = re.compile(
            r'<(?:p|div)[^>]+class="[^"]*(?:str_info|ft|fz-mid|star-wiki)[^"]*"[^>]*>'
            r"(?P<snippet>.*?)"
            r"</(?:p|div)>",
            re.DOTALL,
        )
        for match in block_pattern.finditer(text):
            block = match.group("block")
            link = link_pattern.search(block)
            if not link:
                continue
            snippet = snippet_pattern.search(block)
            results.append(
                EvidenceItem(
                    title=_clean_html(link.group("title")),
                    url=html.unescape(link.group("url")),
                    snippet=_clean_html(snippet.group("snippet") if snippet else block),
                )
            )
            if len(results) >= limit:
                break
        return results

    def _search_so360_html(self, query: str, limit: int) -> list[EvidenceItem]:
        """
        直接请求 360 搜索并使用正则表达式抓取 HTML 提取基本证据。
        """
        params = urllib.parse.urlencode({"q": query})
        request = urllib.request.Request(
            f"https://www.so.com/s?{params}",
            headers=_browser_headers(),
        )
        with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT_SECONDS) as response:
            text = response.read().decode("utf-8", errors="replace")
        results = []
        block_pattern = re.compile(
            r'<li[^>]+class="[^"]*(?:res-list|result)[^"]*"[^>]*>'
            r"(?P<block>.*?)"
            r"(?=<li[^>]+class=\"[^\"]*(?:res-list|result)[^\"]*\"|</body>)",
            re.DOTALL,
        )
        link_pattern = re.compile(
            r"<h3[^>]*>.*?<a[^>]+href=\"(?P<url>[^\"]+)\"[^>]*>(?P<title>.*?)</a>",
            re.DOTALL,
        )
        snippet_pattern = re.compile(
            r'<(?:p|div)[^>]+class="[^"]*(?:res-desc|mh-summary|js-summary)[^"]*"[^>]*>'
            r"(?P<snippet>.*?)"
            r"</(?:p|div)>",
            re.DOTALL,
        )
        for match in block_pattern.finditer(text):
            block = match.group("block")
            link = link_pattern.search(block)
            if not link:
                continue
            snippet = snippet_pattern.search(block)
            results.append(
                EvidenceItem(
                    title=_clean_html(link.group("title")),
                    url=html.unescape(link.group("url")),
                    snippet=_clean_html(snippet.group("snippet") if snippet else block),
                )
            )
            if len(results) >= limit:
                break
        return results


def _post_json(url: str, payload: dict) -> dict:
    """
    辅助发送 POST JSON 请求并返回反序列化后的字典对象。
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(url: str) -> dict:
    """
    辅助发送 GET JSON 请求并返回反序列化后的字典对象。
    """
    with urllib.request.urlopen(url, timeout=SEARCH_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _browser_headers() -> dict[str, str]:
    """
    伪造浏览器请求头，以绕过搜索引擎的基础爬虫检测。
    """
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    }


def _clean_html(value: str) -> str:
    """
    过滤 HTML 标签并统一空格换行字符，产出干净的文本。
    """
    text = re.sub(r"<.*?>", "", value, flags=re.DOTALL)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()
