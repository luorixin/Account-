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

FINAL_ACCOUNT_TYPE_PRIORITY = [
    "State-owned Enterprise(SOE)",
    "Multinational Corporation(MNC)",
    "Private Enterprise(POE)",
    "Other",
]

ACCOUNT_TYPE_NORMALIZATION = {
    "Government Organization(GO)": "State-owned Enterprise(SOE)",
    "State-owned Enterprise(SOE)": "State-owned Enterprise(SOE)",
    "Multinational Corporation(MNC)": "Multinational Corporation(MNC)",
    "Private Enterprise(POE)": "Private Enterprise(POE)",
    "Joint Venture(JV)": "Other",
    "NGO / Non-profit Organization": "Other",
    "Other": "Other",
}

LLM_ACCOUNT_TYPE_ALIASES = {
    "Private Enterprise": "Private Enterprise(POE)",
    "POE": "Private Enterprise(POE)",
    "Private Enterprise (POE)": "Private Enterprise(POE)",
    "Private Enterprise（POE）": "Private Enterprise(POE)",
    "私营企业": "Private Enterprise(POE)",
    "民营企业": "Private Enterprise(POE)",
}

ACCOUNT_TYPE_SHORT_CODES = {
    "State-owned Enterprise(SOE)": "SOE",
    "Private Enterprise(POE)": "POE",
    "Multinational Corporation(MNC)": "MNC",
    "Other": "Other",
}

EXISTING_ACCOUNT_TYPE_NORMALIZATION = {
    **ACCOUNT_TYPE_NORMALIZATION,
    "Specialized Enterprise (SE/央企)": "State-owned Enterprise(SOE)",
    "State-Owned Enterprise (SOE)": "State-owned Enterprise(SOE)",
    "Governmental Organization (GO)": "State-owned Enterprise(SOE)",
    "Private Entity（POE）": "Private Enterprise(POE)",
    "Multinational Corporation（MNC）": "Multinational Corporation(MNC)",
    "Non-Profit Organization (NPO)": "Other",
    "Joint Venture": "Other",
}


def _account_type_match_key(value: str) -> str:
    normalized = value.casefold()
    normalized = normalized.replace("（", "(").replace("）", ")")
    return re.sub(r"[\s\-_()/]+", "", normalized)


_EXISTING_ACCOUNT_TYPE_LOOKUP = {
    _account_type_match_key(raw): normalized
    for raw, normalized in EXISTING_ACCOUNT_TYPE_NORMALIZATION.items()
}

_LLM_ACCOUNT_TYPE_LOOKUP = {
    _account_type_match_key(raw): normalized
    for raw, normalized in LLM_ACCOUNT_TYPE_ALIASES.items()
}


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
    review_status: str = ""

    @property
    def account_type_text(self) -> str:
        return "; ".join(self.account_types)

    @property
    def evidence_url_text(self) -> str:
        return "; ".join(self.evidence_urls)

    @property
    def needs_review(self) -> bool:
        return self.review_status == "Needs Review"


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
            return self._classify_with_llm(
                account_name,
                [],
                force_review_reason=f"Search failed for this account: {exc}",
            )
        if not evidence:
            return self._classify_with_llm(account_name, [])

        return self._classify_with_llm(account_name, evidence)

    def _classify_with_llm(
        self,
        account_name: str,
        evidence: list[EvidenceItem],
        force_review_reason: str = "",
    ) -> AccountClassification:
        try:
            raw = self.llm_client.classify(account_name, evidence)
        except Exception as exc:
            reason_parts = []
            if force_review_reason:
                reason_parts.append(force_review_reason)
            reason_parts.append(f"Classification failed for this account: {exc}")
            return AccountClassification(
                account_types=["Other"],
                confidence="Low",
                reason=" ".join(reason_parts),
                evidence_urls=[item.url for item in evidence if item.url],
                review_status="Needs Review",
            )

        raw_account_types = raw.get("account_types", [])
        has_supported_account_type = any(
            _normalize_raw_account_type(item) for item in raw_account_types
        )
        account_types = _normalize_account_types(
            raw_account_types,
            is_chinese_company=raw.get("is_chinese_company") is True,
        )

        confidence = str(raw.get("confidence") or "Low").strip()
        if confidence not in {"High", "Medium", "Low"}:
            confidence = "Low"

        reason = str(raw.get("reason") or "LLM did not return a usable reason.").strip()
        review_reasons = []
        if (
            account_types == ["Other"]
            and _reason_explicitly_classifies_private_enterprise(reason)
        ):
            account_types = ["Private Enterprise(POE)"]
            review_reasons.append(
                "LLM account_types conflicted with a private enterprise reason."
            )
        if force_review_reason:
            review_reasons.append(force_review_reason)
        if not evidence:
            review_reasons.append("No public evidence found.")
            if confidence == "High":
                confidence = "Medium"
            reason = f"LLM fallback - no public evidence found. {reason}"
        if not has_supported_account_type:
            review_reasons.append("LLM returned no supported account type.")
        if confidence == "Low":
            review_reasons.append("Low confidence classification.")
        if force_review_reason:
            reason = f"{force_review_reason} {reason}"
        evidence_urls = [item.url for item in evidence if item.url]

        return AccountClassification(
            account_types=account_types,
            confidence=confidence,
            reason=reason,
            evidence_urls=evidence_urls,
            review_status="Needs Review" if review_reasons else "",
        )


class OpenAICompatibleLlmClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int = 25,
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
                        "You classify account organizations. If evidence is supplied, use only that evidence. "
                        "If evidence is empty, use general business knowledge and the account name, "
                        "but do not claim that public evidence was found. "
                        "Return strict JSON with account_types, is_chinese_company, confidence, and reason. "
                        "account_types must be an array using all applicable raw labels from: "
                        + ", ".join(VALID_ACCOUNT_TYPES)
                        + ". Include multiple labels when evidence supports multiple characteristics. "
                        "If the organization is a private or privately owned enterprise, account_types "
                        "must include Private Enterprise(POE), not Other. "
                        "Set is_chinese_company to true only when the organization is headquartered in "
                        "China or is otherwise clearly a Chinese company; otherwise use false. "
                        "For Chinese companies with multinational or global operations, use "
                        "Private Enterprise(POE) unless evidence shows government or state ownership. "
                        "Use Other when evidence shows none of the listed types. "
                        "Use confidence as High, Medium, or Low. "
                        "When evidence is empty, do not use High confidence unless the name itself strongly indicates the type."
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


def _normalize_account_types(account_types, is_chinese_company: bool = False) -> list[str]:
    normalized_types = {
        normalized
        for item in account_types
        for normalized in [_normalize_raw_account_type(item)]
        if normalized
    }
    if "State-owned Enterprise(SOE)" in normalized_types:
        return ["State-owned Enterprise(SOE)"]
    if (
        is_chinese_company
        and "Multinational Corporation(MNC)" in normalized_types
    ):
        return ["Private Enterprise(POE)"]
    for account_type in FINAL_ACCOUNT_TYPE_PRIORITY:
        if account_type in normalized_types:
            return [account_type]
    return ["Other"]


def _normalize_raw_account_type(account_type) -> str | None:
    if not isinstance(account_type, str):
        return None
    if account_type in VALID_ACCOUNT_TYPES:
        return ACCOUNT_TYPE_NORMALIZATION[account_type]
    return _LLM_ACCOUNT_TYPE_LOOKUP.get(_account_type_match_key(account_type))


def _reason_explicitly_classifies_private_enterprise(reason: str) -> bool:
    normalized_reason = reason.casefold()
    return any(
        phrase in normalized_reason
        for phrase in [
            "归类为私营企业",
            "归为私营企业",
            "分类为私营企业",
            "判定为私营企业",
            "归类为民营企业",
            "归为民营企业",
            "分类为民营企业",
            "判定为民营企业",
            "classified as private enterprise",
            "classified as a private enterprise",
        ]
    )


def normalize_existing_account_type(account_type_text: str) -> str:
    match_key = _account_type_match_key(account_type_text)
    if not match_key:
        return ""
    normalized_types = {
        normalized
        for raw_key, normalized in _EXISTING_ACCOUNT_TYPE_LOOKUP.items()
        if raw_key in match_key
    }
    for account_type in FINAL_ACCOUNT_TYPE_PRIORITY:
        if account_type in normalized_types:
            return account_type
    return ""


def account_type_short_code(account_type_text: str) -> str:
    return ACCOUNT_TYPE_SHORT_CODES.get(account_type_text.strip(), "")


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
