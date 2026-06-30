"""
分类器模块 (app.classifier)

本模块负责：
- 定义支持的机构类型 (SOE, MNC, POE, Other) 及其简码规则；
- 进行已有账号类型的本地标准化映射；
- 整合联网证据检索 (search) 与 OpenAI 兼容大模型 (LLM) 进行证据支撑下的重分类；
- 处理置信度、复核状态 (Needs Review) 标记逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import urllib.error
import urllib.request


# 工具所支持的所有账号组织标签
VALID_ACCOUNT_TYPES = [
    "Government Organization(GO)",     # 政府机构
    "State-owned Enterprise(SOE)",     # 国有企业
    "Multinational Corporation(MNC)",   # 跨国公司
    "Joint Venture(JV)",               # 合资企业
    "Private Enterprise(POE)",          # 私营/民营企业
    "NGO / Non-profit Organization",   # 非政府组织/非营利机构
    "Other",                           # 其他
]

# 输出最终清洗结果的目标优先级序列
FINAL_ACCOUNT_TYPE_PRIORITY = [
    "State-owned Enterprise(SOE)",
    "Multinational Corporation(MNC)",
    "Private Enterprise(POE)",
    "Other",
]

# LLM 输出标签到本地规范类型的映射表
ACCOUNT_TYPE_NORMALIZATION = {
    "Government Organization(GO)": "State-owned Enterprise(SOE)",
    "State-owned Enterprise(SOE)": "State-owned Enterprise(SOE)",
    "Multinational Corporation(MNC)": "Multinational Corporation(MNC)",
    "Private Enterprise(POE)": "Private Enterprise(POE)",
    "Joint Venture(JV)": "Other",
    "NGO / Non-profit Organization": "Other",
    "Other": "Other",
}

# 常见别名/中文名映射到标准 POE 类型的别名表（增强大模型容错）
LLM_ACCOUNT_TYPE_ALIASES = {
    "Private Enterprise": "Private Enterprise(POE)",
    "POE": "Private Enterprise(POE)",
    "Private Enterprise (POE)": "Private Enterprise(POE)",
    "Private Enterprise（POE）": "Private Enterprise(POE)",
    "私营企业": "Private Enterprise(POE)",
    "民营企业": "Private Enterprise(POE)",
}

# 输出结果简码 (Account Type Short)
ACCOUNT_TYPE_SHORT_CODES = {
    "State-owned Enterprise(SOE)": "SOE",
    "Private Enterprise(POE)": "POE",
    "Multinational Corporation(MNC)": "MNC",
    "Other": "Other",
}

# 现有的 Excel 字段到标准化字段的完整转换映射
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
    """
    通过将字符串转换为小写、统一全半角括号，并去除所有空格与标点符号，
    来生成适合做模糊比对的唯一哈希 Key。
    """
    normalized = value.casefold()
    normalized = normalized.replace("（", "(").replace("）", ")")
    return re.sub(r"[\s\-_()/]+", "", normalized)


# 预计算匹配键字典以提升匹配查询速度
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
    """
    联网检索到的证据条目实体。
    """
    title: str    # 网页标题
    url: str      # 网页 URL 链接
    snippet: str  # 网页摘要片段


@dataclass(frozen=True)
class AccountClassification:
    """
    单个账号分类输出结果数据类。
    """
    account_types: list[str]     # 判定的组织分类列表
    confidence: str              # 置信度 (High, Medium, Low)
    reason: str                  # 判定依据理由描述
    evidence_urls: list[str]     # 使用的证据链接列表
    review_status: str = ""      # 复核标记 ("Needs Review" 或 "")
    failed: bool = False         # 标志此行是否处理失败（True 表示由于接口故障产生的兜底行）

    @property
    def account_type_text(self) -> str:
        """
        获取用分号拼接的分类名称文本。
        """
        return "; ".join(self.account_types)

    @property
    def evidence_url_text(self) -> str:
        """
        获取用分号拼接的证据链接文本。
        """
        return "; ".join(self.evidence_urls)

    @property
    def needs_review(self) -> bool:
        """
        判断此分类是否需要进行人工审核。
        """
        return self.review_status == "Needs Review"


class LlmEvidenceClassifier:
    """
    核心分类器，结合联网搜索客户端与大模型客户端完成证据支撑的智能判定。
    """
    def __init__(self, search_client, llm_client):
        self.search_client = search_client
        self.llm_client = llm_client

    def classify(self, account_name: str) -> AccountClassification:
        """
        对单个账号名发起联网搜索和 LLM 判定。
        """
        try:
            # 搜索与该公司所有制和组织形式相关的公开网页
            evidence = self.search_client.search(
                f"{account_name} account ownership organization type",
                limit=5,
            )
        except Exception as exc:
            # 联网搜索失败，直接退化进入 LLM 纯离线预测，并标记需要复核
            return self._classify_with_llm(
                account_name,
                [],
                force_review_reason=f"Search failed for this account: {exc}",
            )
        if not evidence:
            # 未检索到公开证据，退化为 LLM 离线预测
            return self._classify_with_llm(account_name, [])

        return self._classify_with_llm(account_name, evidence)

    def _classify_with_llm(
        self,
        account_name: str,
        evidence: list[EvidenceItem],
        force_review_reason: str = "",
    ) -> AccountClassification:
        """
        使用收集到的证据和大模型预测最终的组织形式，并处理各种复核标志。
        """
        try:
            # 调用大模型得到结构化的 JSON 返回
            raw = self.llm_client.classify(account_name, evidence)
        except Exception as exc:
            # LLM 服务异常，组装全局兜底并标为失败
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
                failed=True,
            )

        # 检查是否返回了至少一个可以识别的原始类型
        raw_account_types = raw.get("account_types", [])
        has_supported_account_type = any(
            _normalize_raw_account_type(item) for item in raw_account_types
        )
        # 对输出列表依据项目规则和所有所有制进行本地标准化转化
        account_types = _normalize_account_types(
            raw_account_types,
            is_chinese_company=raw.get("is_chinese_company") is True,
        )

        confidence = str(raw.get("confidence") or "Low").strip()
        if confidence not in {"High", "Medium", "Low"}:
            confidence = "Low"

        reason = str(raw.get("reason") or "LLM did not return a usable reason.").strip()
        review_reasons = []

        # 特殊纠偏逻辑：如果模型给的类型为 Other 但描述中包含了明确判为私企的表述，则自动转为 POE
        if (
            account_types == ["Other"]
            and _reason_explicitly_classifies_private_enterprise(reason)
        ):
            account_types = ["Private Enterprise(POE)"]
            review_reasons.append(
                "LLM account_types conflicted with a private enterprise reason."
            )

        # 构建 Needs Review 的触发条件
        if force_review_reason:
            review_reasons.append(force_review_reason)
        if not evidence:
            # 无任何公开检索证据时，强制降级 High 为 Medium，并置为 Needs Review
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
    """
    通过标准化 OpenAI /chat/completions 兼容格式发起请求的 LLM 客户端。
    """
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int = 25,
    ):
        """
        优先度：方法参数入参 > 环境变量 (LLM_API_KEY -> DEEPSEEK_API_KEY -> OPENAI_API_KEY)
        """
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
        """
        构造 Prompt 并调用 OpenAI 兼容模型，返回解析出的 dict 对象。
        要求大模型输出包含特定键名的 strict JSON 对象。
        """
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
    """
    核心规范化业务映射。
    - 如果包含 SOE，不论是否为中国企业，都输出 SOE；
    - 特殊业务规则：中国企业（is_chinese_company=True）即使有跨国公司 (MNC) 证据，若无国资背景，也必须强制输出为 POE。
    - 其它情况依据优先级：SOE > MNC > POE > Other。
    """
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
    """
    将 LLM 返回的原始分类文本转换为内置的标准分类标签。
    """
    if not isinstance(account_type, str):
        return None
    if account_type in VALID_ACCOUNT_TYPES:
        return ACCOUNT_TYPE_NORMALIZATION[account_type]
    return _LLM_ACCOUNT_TYPE_LOOKUP.get(_account_type_match_key(account_type))


def _reason_explicitly_classifies_private_enterprise(reason: str) -> bool:
    """
    辅助判定模型返回的判定理由是否包含了判断为私企的关键词。
    """
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
    """
    对 Excel 中已经存在的标签进行本地标准化，无需大模型处理。
    """
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
    """
    获取标签的简码，用于输出 'Account Type Short' 列。
    """
    return ACCOUNT_TYPE_SHORT_CODES.get(account_type_text.strip(), "")


def _parse_json_object(text: str) -> dict:
    """
    健壮地从 LLM 响应中解析出 JSON 字典（支持 markdown json 块围栏过滤）。
    """
    cleaned = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    return json.loads(cleaned)


def _chat_completions_endpoint(base_url: str) -> str:
    """
    补全并生成完整的 /chat/completions API 请求路径。
    """
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/chat/completions"):
        return trimmed
    return f"{trimmed}/chat/completions"
