from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import urllib.error
import urllib.request


VALID_ACCOUNT_TYPES = [
    "Government Organization(GO)",
    "State-owned Enterprise(SOE)",
    "Multinational Corporation(MNC)",
    "Joint Venture(JV)",
    "Private Enterprise(POE)",
    "NGO / Non-profit Organization",
    "Other",
]


@dataclass(frozen=True)
class EvidenceItem:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class AccountClassification:
    account_types: list[str]
    confidence: str
    reason: str
    evidence_urls: list[str]

    @property
    def account_type_text(self) -> str:
        return "; ".join(self.account_types)

    @property
    def evidence_url_text(self) -> str:
        return "; ".join(self.evidence_urls)


class LlmEvidenceClassifier:
    def __init__(self, search_client, llm_client):
        self.search_client = search_client
        self.llm_client = llm_client

    def classify(self, account_name: str) -> AccountClassification:
        try:
            evidence = self.search_client.search(
                f"{account_name} account ownership organization type",
                limit=5,
            )
        except Exception as exc:
            return AccountClassification(
                account_types=["Needs Review"],
                confidence="Low",
                reason=f"Search failed for this account: {exc}",
                evidence_urls=[],
            )
        if not evidence:
            return AccountClassification(
                account_types=["Needs Review"],
                confidence="Low",
                reason="No public evidence was found for this account.",
                evidence_urls=[],
            )

        raw = self.llm_client.classify(account_name, evidence)
        valid_types = [item for item in raw.get("account_types", []) if item in VALID_ACCOUNT_TYPES]
        if not valid_types:
            valid_types = ["Needs Review"]

        confidence = str(raw.get("confidence") or "Low").strip()
        if confidence not in {"High", "Medium", "Low"}:
            confidence = "Low"

        reason = str(raw.get("reason") or "LLM did not return a usable reason.").strip()
        evidence_urls = [item.url for item in evidence if item.url]

        return AccountClassification(
            account_types=valid_types,
            confidence=confidence,
            reason=reason,
            evidence_urls=evidence_urls,
        )


class OpenAICompatibleLlmClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int = 60,
    ):
        self.api_key = (
            api_key
            or os.getenv("LLM_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        self.model = model or os.getenv("LLM_MODEL") or "deepseek-v4-flash"
        configured_base_url = (
            base_url
            or os.getenv("LLM_BASE_URL")
            or "https://api.deepseek.com"
        )
        self.endpoint_url = _chat_completions_endpoint(configured_base_url)
        self.timeout_seconds = timeout_seconds

    def classify(self, account_name: str, evidence: list[EvidenceItem]) -> dict:
        if not self.api_key:
            raise RuntimeError(
                "Missing LLM API key. Set LLM_API_KEY, DEEPSEEK_API_KEY, or OPENAI_API_KEY before starting the server."
            )

        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You classify account organizations using only supplied public evidence. "
                        "Return strict JSON with account_types, confidence, and reason. "
                        "account_types must be an array using only these labels: "
                        + ", ".join(VALID_ACCOUNT_TYPES)
                        + ". Use multiple labels only when evidence supports each. "
                        "Use Other when evidence shows none of the listed types. "
                        "Use confidence as High, Medium, or Low."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "account_name": account_name,
                            "evidence": [
                                {
                                    "title": item.title,
                                    "url": item.url,
                                    "snippet": item.snippet,
                                }
                                for item in evidence
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        request = urllib.request.Request(
            self.endpoint_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM request failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM request failed: {exc.reason}") from exc

        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
        return _parse_json_object(content)


def _parse_json_object(text: str) -> dict:
    cleaned = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    return json.loads(cleaned)


def _chat_completions_endpoint(base_url: str) -> str:
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/chat/completions"):
        return trimmed
    return f"{trimmed}/chat/completions"
