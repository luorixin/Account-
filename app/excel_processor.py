"""
Excel 处理器模块 (app.excel_processor)

本模块负责 Excel 工作簿的解析、目标数据行的筛选识别、多线程并发分类调度以及清洗后数据的写回。
本模块依赖 openpyxl 进行 Excel 操作。
已整合本地数据库缓存，自动重用已清洗的数据；并在处理结束时持久化数据行明细结果。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
import os
import re

from openpyxl import load_workbook

from app.classifier import (
    AccountClassification,
    account_type_short_code,
    normalize_existing_account_type,
)


# 定义需要在 Excel 中追加输出的列名
OUTPUT_HEADERS = [
    "New Account Type",          # 新分类
    "Account Type Short",        # 新分类简码 (SOE, MNC, POE, Other)
    "Review Status",             # 复核状态 (如 Needs Review)
    "Classification Confidence", # 置信度 (High, Medium, Low)
    "Classification Reason",     # 分类原因描述
    "Evidence URLs",             # 公开检索到的证据链接
]

# 并发分类处理的默认和最大线程数
DEFAULT_CLASSIFICATION_MAX_WORKERS = 4
MAX_CLASSIFICATION_MAX_WORKERS = 8

# 需要被提取并重新联网/LLM 分类的目标源账号类型 (不区分大小写，去空格)
CLASSIFICATION_TARGET_ACCOUNT_TYPES = {
    "jv",
    "joint venture",
    "joint venture(jv)",
    "joint venture (jv)",
    "public entity",
    "private equity",
    "private equity investee",
}

# 具有高优先级且已知的本地标准化类型（若匹配到则直接本地转化，不发起联网分类）
LOCAL_PRIORITY_ACCOUNT_TYPES = {
    "State-owned Enterprise(SOE)",
    "Multinational Corporation(MNC)",
    "Private Enterprise(POE)",
}


def process_workbook(file_bytes: bytes, classifier, progress_callback=None, job_id: str | None = None) -> bytes:
    """
    解析上传的 Excel 文件，并对符合条件的行进行处理，最后生成处理后的 Excel 二进制数据。
    仅处理第一个 worksheet，保留其他 sheets 且尽量保留格式。
    
    Args:
        file_bytes: 原始 Excel 文件的字节数组
        classifier: 用于分类的 LlmEvidenceClassifier 实例
        progress_callback: 接收处理进度更新的回调函数
        job_id: 任务的唯一标识符（可选，用于数据行持久化）
        
    Returns:
        bytes: 追加了清洗结果列的 Excel 文件字节流
    """
    # 使用 openpyxl 加载工作簿（不开启 data_only=True 以免覆盖原公式）
    workbook = load_workbook(BytesIO(file_bytes))
    sheet = workbook.worksheets[0]
    
    # 查找“Account Name”和“Account Type”列的索引 (1-indexed)
    account_name_col, account_type_col = _find_required_columns(sheet)
    
    # 获取输出结果列写入的起始列索引
    output_start_col = _output_start_column(sheet)
    
    # 筛选出所有需要进行联网 LLM 分类的目标行 (包含行号和账号名)
    target_rows = _classification_target_rows(sheet, account_name_col, account_type_col)
    total_target_rows = len(target_rows)
    
    # 初始化进度条 (已处理为 0)
    if progress_callback:
        progress_callback(
            {
                "processed": 0,
                "total": total_target_rows,
                "current_account": "",
                "result": None,
            }
        )

    # 写入输出表头
    for offset, header in enumerate(OUTPUT_HEADERS):
        sheet.cell(row=1, column=output_start_col + offset).value = header

    # 进行并发分类处理，获取所有目标行的分类结果映射字典
    classification_results = _classify_target_rows(
        target_rows,
        classifier,
        _classification_max_workers(),
        progress_callback=_progress_callback(progress_callback, total_target_rows),
    )

    # 收集行明细结果，以便持久化至本地数据库支持网页端人工复核修改
    row_results = []

    # 逐行处理并填入输出结果
    for row in range(2, sheet.max_row + 1):
        account_name = _cell_text(sheet.cell(row=row, column=account_name_col).value)
        account_type = _cell_text(sheet.cell(row=row, column=account_type_col).value)
        
        if _should_classify_row(account_name, account_type):
            # 该行属于分类目标行，填入并发分类器得出的结果
            result = classification_results[row]
            values = [
                result.account_type_text,
                account_type_short_code(result.account_type_text),
                result.review_status,
                result.confidence,
                result.reason,
                result.evidence_url_text,
            ]
            row_results.append({
                "row_index": row,
                "account_name": account_name,
                "original_account_type": account_type,
                "new_account_type": result.account_type_text,
                "confidence": result.confidence,
                "reason": result.reason,
                "evidence_urls": result.evidence_url_text,
                "review_status": result.review_status,
                "failed": result.failed,
                "is_target": 1
            })
        else:
            # 该行不属于重分类目标（或为空白名），按本地内置规则进行标准化转化
            normalized_account_type = normalize_existing_account_type(account_type)
            values = [
                normalized_account_type,
                account_type_short_code(normalized_account_type),
                "",
                "",
                "",
                "",
            ]
            row_results.append({
                "row_index": row,
                "account_name": account_name,
                "original_account_type": account_type,
                "new_account_type": normalized_account_type,
                "confidence": "",
                "reason": "",
                "evidence_urls": "",
                "review_status": "",
                "failed": False,
                "is_target": 0
            })

        # 写入工作表
        for offset, value in enumerate(values):
            sheet.cell(row=row, column=output_start_col + offset).value = value

    # 如果传入了 job_id，将明细行结果批量入库
    if job_id:
        from app.db import save_job_results_to_db
        save_job_results_to_db(job_id, row_results)

    # 将处理后的工作簿保存至 BytesIO 字节流中返回
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _classify_target_rows(
    target_rows: list[tuple[int, str]],
    classifier,
    max_workers: int,
    progress_callback=None,
) -> dict[int, AccountClassification]:
    """
    使用 ThreadPoolExecutor 线程池并发处理需要重分类的账号，并去重账号以节省 API 成本。
    
    Args:
        target_rows: (行号, 账号名) 列表
        classifier: 包含联网搜索与大模型请求的分类器
        max_workers: 并发工作线程数
        progress_callback: 每完成一个行分类时的进度回调函数
        
    Returns:
        dict[int, AccountClassification]: 行号到分类结果的映射字典
    """
    # 按照账号名对行号进行归类去重，确保相同的账号名仅运行一次网络分类
    rows_by_account: dict[str, list[int]] = {}
    for row, account_name in target_rows:
        rows_by_account.setdefault(account_name, []).append(row)

    results_by_row: dict[int, AccountClassification] = {}
    if not rows_by_account:
        return results_by_row

    # 提交多线程分类任务
    worker_count = min(max_workers, len(rows_by_account))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(_classify_account, classifier, account_name): account_name
            for account_name in rows_by_account
        }
        # 实时等待线程池任务完成并分发结果给对应去重前的数据行
        for future in as_completed(futures):
            account_name = futures[future]
            result = future.result()
            for row in rows_by_account[account_name]:
                results_by_row[row] = result
                # 触发进度回调
                if progress_callback:
                    progress_callback(account_name, result)
    return results_by_row


def _classify_account(classifier, account_name: str) -> AccountClassification:
    """
    对单个账号名发起分类请求。会首先尝试从本地 SQLite 数据库缓存中读取结果。
    捕获异常，确保单行报错不导致全局任务崩溃。
    """
    from app.db import get_cached_classification, save_cached_classification
    
    try:
        # 1. 优先查找本地数据库缓存
        cached = get_cached_classification(account_name)
        if cached:
            return cached
            
        # 2. 无缓存时，发起联网搜索和大模型判定
        result = classifier.classify(account_name)
        
        # 3. 分类成功后，存入本地数据库缓存
        save_cached_classification(account_name, result)
        return result
    except Exception as exc:
        # 单行出错，返回包含 'Needs Review' 和 'failed=True' 的标记，置信度设为 Low，类型设为 Other
        return AccountClassification(
            account_types=["Other"],
            confidence="Low",
            reason=f"Classification failed for this account: {exc}",
            evidence_urls=[],
            review_status="Needs Review",
            failed=True,
        )


def _progress_callback(progress_callback, total_target_rows: int):
    """
    构建统一的进度条回调包裹函数。
    """
    if not progress_callback:
        return None
    processed_target_rows = 0

    def report(account_name: str, result: AccountClassification):
        nonlocal processed_target_rows
        processed_target_rows += 1
        progress_callback(
            {
                "processed": processed_target_rows,
                "total": total_target_rows,
                "current_account": account_name,
                "result": result,
            }
        )

    return report


def _classification_max_workers() -> int:
    """
    获取并发工作的线程数，读取自 CLASSIFICATION_MAX_WORKERS 环境变量，限制在 1..8 之间。
    """
    raw_value = os.getenv("CLASSIFICATION_MAX_WORKERS", "")
    try:
        max_workers = int(raw_value)
    except ValueError:
        max_workers = DEFAULT_CLASSIFICATION_MAX_WORKERS
    return max(1, min(max_workers, MAX_CLASSIFICATION_MAX_WORKERS))


def _find_required_columns(sheet):
    """
    查找 Excel 第一行中包含 'Account Name' 和 'Account Type' 的列索引（忽略大小写和空格）。
    """
    headers = {
        _normalize_header(sheet.cell(row=1, column=col).value): col
        for col in range(1, sheet.max_column + 1)
    }
    account_name_col = headers.get("account name")
    account_type_col = headers.get("account type")
    if not account_name_col or not account_type_col:
        raise ValueError("Missing required columns: Account Name and Account Type.")
    return account_name_col, account_type_col


def _output_start_column(sheet) -> int:
    """
    计算输出列填入的起始列号。如果已存在包含相同输出表头的完整列，则选择覆盖，否则在末尾追加新列。
    """
    existing_start_col = _find_existing_output_start_column(sheet)
    if existing_start_col:
        return existing_start_col
    return sheet.max_column + 1


def _find_existing_output_start_column(sheet) -> int | None:
    """
    检查 Excel 第一行中是否已包含本工具定义的输出头信息。若匹配成功，返回对应的起始列索引。
    """
    output_header_count = len(OUTPUT_HEADERS)
    last_start_col = sheet.max_column - output_header_count + 1
    for start_col in range(1, max(last_start_col, 0) + 1):
        if all(
            _normalize_header(sheet.cell(row=1, column=start_col + offset).value)
            == _normalize_header(header)
            for offset, header in enumerate(OUTPUT_HEADERS)
        ):
            return start_col
    return None


def _classification_target_rows(sheet, account_name_col, account_type_col) -> list[tuple[int, str]]:
    """
    遍历数据行，筛选出符合重分类条件的所有目标数据行列表。
    """
    rows = []
    for row in range(2, sheet.max_row + 1):
        account_name = _cell_text(sheet.cell(row=row, column=account_name_col).value)
        account_type = _cell_text(sheet.cell(row=row, column=account_type_col).value)
        if _should_classify_row(account_name, account_type):
            rows.append((row, account_name))
    return rows


def _should_classify_row(account_name: str, account_type: str) -> bool:
    """
    判断该数据行是否需要运行联网 LLM 分类。
    条件：账号名不为空，且已有的账号类型为空，或属于 JV, Joint Venture, Public Entity, Private Equity 等待重判的标签。
    注意：如果包含本队高优本地标准化标签（SOE, MNC, POE），则不进行联网重判，返回 False。
    """
    normalized_account_type = normalize_existing_account_type(account_type)
    if normalized_account_type in LOCAL_PRIORITY_ACCOUNT_TYPES:
        return False
    return bool(account_name.strip()) and (
        account_type.strip() == ""
        or any(
            token in CLASSIFICATION_TARGET_ACCOUNT_TYPES
            for token in _account_type_tokens(account_type)
        )
    )


def _account_type_tokens(account_type: str) -> list[str]:
    """
    按逗号、分号等切分单元格内的现有 Account Type 内容以提取标签列表。
    """
    return [
        token.strip().casefold()
        for token in re.split(r"[,;，；]", account_type)
        if token.strip()
    ]


def _normalize_header(value) -> str:
    """
    标准化表头值，以方便列名匹配。
    """
    return _cell_text(value).casefold()


def _cell_text(value) -> str:
    """
    安全提取 Excel 单元格的值为 string 去空格类型，当单元格为 None 时返回空字符串。
    """
    if value is None:
        return ""
    return str(value).strip()
