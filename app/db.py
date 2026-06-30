"""
数据库与持久化模块 (app.db)

使用 Python 内置的 sqlite3 实现本地轻量级数据库存储。
核心表结构：
1. jobs: 存储清洗任务的元数据、输入原始文件和输出处理结果文件。
2. classification_cache: 存储清洗过的账号分类缓存，供后续重用。
3. job_results: 存储单个任务明细行级别的清洗结果，支持网页端人工复核修改。
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3
import time
from io import BytesIO
from openpyxl import load_workbook

from app.classifier import AccountClassification, account_type_short_code, normalize_existing_account_type
from app.excel_processor import OUTPUT_HEADERS

DB_PATH = "cleanse_tool.db"
CACHE_RETENTION_SECONDS = 30 * 24 * 60 * 60  # 缓存保留 30 天


@contextmanager
def get_db_connection():
    """
    获取数据库连接，并在 with 代码块结束后关闭连接。
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """
    初始化数据库表结构。如果表不存在则进行创建。
    """
    # 确保数据库文件所在的目录存在
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    with get_db_connection() as conn:
        # 1. 任务表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                state TEXT NOT NULL,
                total INTEGER DEFAULT 0,
                processed INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                failure_count INTEGER DEFAULT 0,
                needs_review_count INTEGER DEFAULT 0,
                error TEXT DEFAULT '',
                created_at REAL NOT NULL,
                completed_at REAL,
                input_bytes BLOB,
                output_bytes BLOB
            )
        """)

        # 2. 分类结果缓存表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS classification_cache (
                account_name TEXT PRIMARY KEY,
                account_types TEXT NOT NULL,  -- JSON 数组
                confidence TEXT NOT NULL,
                reason TEXT NOT NULL,
                evidence_urls TEXT NOT NULL,  -- JSON 数组
                review_status TEXT DEFAULT '',
                created_at REAL NOT NULL
            )
        """)

        # 3. 任务行明细结果表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS job_results (
                job_id TEXT,
                row_index INTEGER,
                account_name TEXT NOT NULL,
                original_account_type TEXT DEFAULT '',
                new_account_type TEXT DEFAULT '',
                confidence TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                evidence_urls TEXT DEFAULT '',  -- JSON 数组/拼接文本
                review_status TEXT DEFAULT '',
                failed INTEGER DEFAULT 0,
                is_target INTEGER DEFAULT 0,    -- 是否为联网分类目标行
                PRIMARY KEY (job_id, row_index)
            )
        """)
        _ensure_column(conn, "job_results", "failed", "INTEGER DEFAULT 0")
        conn.commit()


def get_cached_classification(account_name: str) -> AccountClassification | None:
    """
    从本地缓存中查找指定账号的分类结果。只返回未过期的缓存。
    """
    now = time.time()
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM classification_cache WHERE account_name = ? AND (? - created_at) < ?",
            (account_name, now, CACHE_RETENTION_SECONDS)
        ).fetchone()

        if row:
            try:
                types = json.loads(row["account_types"])
                urls = json.loads(row["evidence_urls"])
            except json.JSONDecodeError:
                types = [row["account_types"]]
                urls = [row["evidence_urls"]]

            return AccountClassification(
                account_types=types,
                confidence=row["confidence"],
                reason=row["reason"],
                evidence_urls=urls,
                review_status=row["review_status"],
                failed=False
            )
    return None


def save_cached_classification(account_name: str, result: AccountClassification):
    """
    将账号的清洗结果保存到本地缓存数据库中，以便后续任务快速复用。
    """
    if result.failed or result.needs_review or result.confidence == "Low":
        # 失败、低置信度或需要人工复核的结果不进入全局缓存，避免阻止后续更高质量检索。
        return
    now = time.time()
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO classification_cache (account_name, account_types, confidence, reason, evidence_urls, review_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_name) DO UPDATE SET
                account_types = excluded.account_types,
                confidence = excluded.confidence,
                reason = excluded.reason,
                evidence_urls = excluded.evidence_urls,
                review_status = excluded.review_status,
                created_at = excluded.created_at
            """,
            (
                account_name,
                json.dumps(result.account_types, ensure_ascii=False),
                result.confidence,
                result.reason,
                json.dumps(result.evidence_urls, ensure_ascii=False),
                result.review_status,
                now
            )
        )
        conn.commit()


def save_job_results_to_db(job_id: str, results: list[dict]):
    """
    批量保存任务的明细行结果。
    """
    with get_db_connection() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO job_results (
                job_id, row_index, account_name, original_account_type,
                new_account_type, confidence, reason, evidence_urls, review_status, failed, is_target
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    job_id,
                    r["row_index"],
                    r["account_name"],
                    r["original_account_type"],
                    r["new_account_type"],
                    r["confidence"],
                    r["reason"],
                    r["evidence_urls"],
                    r["review_status"],
                    1 if r.get("failed") else 0,
                    r["is_target"]
                )
                for r in results
            ]
        )
        conn.commit()


def get_job_results_from_db(job_id: str) -> list[dict]:
    """
    获取指定任务的明细行分类结果列表，按行号升序排列。
    """
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM job_results WHERE job_id = ? ORDER BY row_index ASC",
            (job_id,)
        ).fetchall()
        return [dict(row) for row in rows]


def update_row_classification_in_db(
    job_id: str,
    row_index: int,
    new_account_type: str,
    review_status: str,
    reason: str
) -> dict:
    """
    更新某一行账号的分类结果、复核状态和原因，重新将修改同步写入内存 Excel 字节流，并更新统计计数。
    同时，该修改会被同步更新到 classification_cache 全局账号缓存中。
    """
    with get_db_connection() as conn:
        # 1. 查找原有行信息以获取账号名
        old_row = conn.execute(
            "SELECT account_name FROM job_results WHERE job_id = ? AND row_index = ?",
            (job_id, row_index)
        ).fetchone()
        if not old_row:
            raise KeyError(f"Row {row_index} not found for job {job_id}")
        account_name = old_row["account_name"]

        # 2. 更新明细行结果
        conn.execute(
            """
            UPDATE job_results 
            SET new_account_type = ?, review_status = ?, reason = ?, failed = 0
            WHERE job_id = ? AND row_index = ?
            """,
            (new_account_type, review_status, reason, job_id, row_index)
        )

        # 3. 如果该行是目标重分类账号，并且修改为了合法标准类型，同步更新该账号的全局全局缓存
        # 这样未来的上传任务就能直接使用用户人工修正后的完美分类！
        if new_account_type in {"State-owned Enterprise(SOE)", "Multinational Corporation(MNC)", "Private Enterprise(POE)", "Other"}:
            conn.execute(
                """
                INSERT INTO classification_cache (account_name, account_types, confidence, reason, evidence_urls, review_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_name) DO UPDATE SET
                    account_types = excluded.account_types,
                    review_status = excluded.review_status,
                    reason = excluded.reason,
                    created_at = excluded.created_at
                """,
                (
                    account_name,
                    json.dumps([new_account_type], ensure_ascii=False),
                    "High", # 人工确认后置信度升为 High
                    f"User manual adjustment: {reason}" if reason else "User manual adjustment.",
                    json.dumps([], ensure_ascii=False),
                    review_status,
                    time.time()
                )
            )

        # 4. 重新计算任务统计计数
        # 统计规则：只对 is_target = 1 的目标行进行成功、需要复核、失败计数的重新统计
        target_rows = conn.execute(
            "SELECT new_account_type, review_status, failed FROM job_results WHERE job_id = ? AND is_target = 1",
            (job_id,)
        ).fetchall()

        success_count = 0
        needs_review_count = 0
        failure_count = 0

        for r in target_rows:
            is_failed = bool(r["failed"])

            if r["new_account_type"] and not is_failed:
                success_count += 1
            if r["review_status"] == "Needs Review":
                needs_review_count += 1
                if is_failed:
                    failure_count += 1

        # 5. 读取任务的原始输入文件字节，重构 Excel 文件并保存为 output_bytes
        job_row = conn.execute("SELECT input_bytes, filename FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job_row or not job_row["input_bytes"]:
            raise ValueError("Original input bytes not found for regeneration.")

        input_bytes = job_row["input_bytes"]
        workbook = load_workbook(BytesIO(input_bytes))
        sheet = workbook.worksheets[0]

        # 重新定位列
        # 查找需要的列
        headers = {
            str(sheet.cell(row=1, column=col).value).strip().casefold(): col
            for col in range(1, sheet.max_column + 1)
        }
        account_name_col = headers.get("account name")
        account_type_col = headers.get("account type")
        
        # 确定输出列写入起点
        output_start_col = sheet.max_column + 1
        output_header_count = len(OUTPUT_HEADERS)
        last_start_col = sheet.max_column - output_header_count + 1
        for start_col in range(1, max(last_start_col, 0) + 1):
            if all(
                str(sheet.cell(row=1, column=start_col + offset).value).strip().casefold()
                == header.strip().casefold()
                for offset, header in enumerate(OUTPUT_HEADERS)
            ):
                output_start_col = start_col
                break

        # 写入表头
        for offset, header in enumerate(OUTPUT_HEADERS):
            sheet.cell(row=1, column=output_start_col + offset).value = header

        # 读取最新数据库结果，刷入工作表
        all_results = conn.execute(
            "SELECT * FROM job_results WHERE job_id = ? ORDER BY row_index ASC",
            (job_id,)
        ).fetchall()

        for res in all_results:
            row = res["row_index"]
            # 追加写入列的值
            values = [
                res["new_account_type"],
                account_type_short_code(res["new_account_type"]),
                res["review_status"],
                # 置信度：如果是目标行则取值，非目标行本地标准化则为空
                res["confidence"] if res["is_target"] else "",
                res["reason"] if res["is_target"] else "",
                res["evidence_urls"] if res["is_target"] else "",
            ]
            for offset, val in enumerate(values):
                sheet.cell(row=row, column=output_start_col + offset).value = val

        # 保存为输出字节流
        output_stream = BytesIO()
        workbook.save(output_stream)
        output_bytes = output_stream.getvalue()

        # 6. 更新任务总进度数据和 output_bytes 文件
        conn.execute(
            """
            UPDATE jobs 
            SET success_count = ?, needs_review_count = ?, failure_count = ?, output_bytes = ?
            WHERE id = ?
            """,
            (success_count, needs_review_count, failure_count, output_bytes, job_id)
        )
        conn.commit()

        return {
            "success_count": success_count,
            "needs_review_count": needs_review_count,
            "failure_count": failure_count
        }


def save_job_to_db(
    job_id: str,
    filename: str,
    state: str,
    total: int,
    created_at: float,
    input_bytes: bytes,
    output_bytes: bytes | None = None,
    processed: int = 0,
    success_count: int = 0,
    failure_count: int = 0,
    needs_review_count: int = 0,
    error: str = "",
    completed_at: float | None = None
):
    """
    保存或更新一个任务记录。
    """
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, filename, state, total, processed, success_count,
                failure_count, needs_review_count, error, created_at, completed_at, input_bytes, output_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state = excluded.state,
                total = excluded.total,
                processed = excluded.processed,
                success_count = excluded.success_count,
                failure_count = excluded.failure_count,
                needs_review_count = excluded.needs_review_count,
                error = excluded.error,
                completed_at = excluded.completed_at,
                output_bytes = excluded.output_bytes
            """,
            (
                job_id, filename, state, total, processed, success_count,
                failure_count, needs_review_count, error, created_at, completed_at,
                input_bytes, output_bytes
            )
        )
        conn.commit()


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_sql: str):
    existing_columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in existing_columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")


def get_job_from_db(job_id: str) -> dict | None:
    """
    从数据库中查询指定任务。
    """
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def list_jobs_from_db() -> list[dict]:
    """
    获取历史任务列表，按创建时间降序排列。
    """
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, filename, state, total, processed, success_count, failure_count, needs_review_count, error, created_at, completed_at FROM jobs ORDER BY created_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def delete_job_from_db(job_id: str):
    """
    从数据库中彻底物理删除一个任务及其对应的明细行记录（主要在清理过期任务时使用）。
    """
    with get_db_connection() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.execute("DELETE FROM job_results WHERE job_id = ?", (job_id,))
        conn.commit()
