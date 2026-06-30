"""
任务管理模块 (app.jobs)

本模块定义了分类清洗任务的生命周期、状态维护与数据持久化逻辑。
已集成本地数据库 (app.db) 进行任务的完整持久化。使得任务记录在服务器重启后仍可保留、查询和下载。
支持异步多线程启动任务、进度统计更新，以及在设定时间后自动清理过期任务。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
import uuid

from app.excel_processor import process_workbook
from app.db import init_db, save_job_to_db, get_job_from_db, list_jobs_from_db


# 默认的已完成任务保留时间：1 小时 (60分钟 * 60秒)
DEFAULT_JOB_RETENTION_SECONDS = 60 * 60


@dataclass
class Job:
    """
    表示一个 Excel 清洗任务的实体类。
    用于记录任务的基本信息、当前进度、分类结果计数以及输出的二进制文件数据。
    """
    id: str                   # 任务的唯一标识符 (UUID hex)
    filename: str             # 上传的原始文件名
    state: str = "queued"     # 任务的当前状态 (queued, running, completed, completed_with_errors, failed)
    total: int = 0            # 待分类的目标总行数
    processed: int = 0        # 当前已处理的总行数
    current_account: str = "" # 当前正在分类处理的账号名称
    success_count: int = 0    # 成功分类的行数（不包含抛出异常分类失败的行）
    failure_count: int = 0    # 分类处理失败的行数（捕获到 API 或系统异常的行）
    needs_review_count: int = 0 # 标记为“需要复核 (Needs Review)”的行数
    error: str = ""           # 任务全局失败时的错误信息
    output: bytes | None = None # 处理完成后生成的 Excel 字节流数据
    created_at: float = field(default_factory=time.time)  # 任务创建的时间戳
    completed_at: float | None = None # 任务完成/失败的时间戳


class JobManager:
    """
    清洗任务管理器类，负责任务的创建、状态查询、结果下载以及垃圾回收清理。
    已结合本地数据库进行持久化。所有操作保证线程安全。
    """
    def __init__(self, classifier_factory, retention_seconds: float = DEFAULT_JOB_RETENTION_SECONDS):
        """
        初始化任务管理器。同时初始化 SQLite 数据库。
        
        Args:
            classifier_factory: 用于构建分类器的工厂函数/类
            retention_seconds: 已完成任务在内存中的保留秒数，过期后将被自动剪裁清理
        """
        self.classifier_factory = classifier_factory
        self.retention_seconds = retention_seconds
        self._jobs: dict[str, Job] = {} # 存放所有任务的字典，Key 为 job_id，Value 为 Job 实例
        self._lock = threading.RLock()   # 用于保证线程安全的重入锁
        # 初始化数据库连接并建表
        init_db()

    def create_job(
        self,
        file_bytes: bytes,
        filename: str,
        llm_api_key: str | None,
        tavily_api_key: str | None,
        run_inline: bool = False,
    ) -> str:
        """
        创建并启动一个新任务。同时将任务记录持久化写入本地 SQLite。
        在创建任务前，会自动触发一次过期任务的清理。
        
        Args:
            file_bytes: 上传的 Excel 文件字节流
            filename: 上传的文件名
            llm_api_key: 网页请求传入的临时 LLM API Key
            tavily_api_key: 网页请求传入的临时 Tavily API Key
            run_inline: 是否在当前线程同步执行任务（主要用于测试）
            
        Returns:
            str: 任务的唯一标识符 job_id
        """
        job = Job(id=uuid.uuid4().hex, filename=filename)
        with self._lock:
            # 清理过期任务，防止内存无限上涨
            self._prune_expired_jobs_locked(time.time())
            self._jobs[job.id] = job

        # 写入数据库记录任务的初始信息，保存原始输入数据 input_bytes
        save_job_to_db(
            job_id=job.id,
            filename=filename,
            state="queued",
            total=0,
            created_at=job.created_at,
            input_bytes=file_bytes
        )

        args = (job.id, file_bytes, llm_api_key, tavily_api_key)
        if run_inline:
            self._run_job(*args)
        else:
            # 在后台守护线程中异步执行任务
            thread = threading.Thread(target=self._run_job, args=args, daemon=True)
            thread.start()
        return job.id

    def get_status(self, job_id: str) -> dict:
        """
        根据 job_id 获取任务的当前状态与进度信息。支持从数据库中读取历史数据（即使服务器重启）。
        
        Args:
            job_id: 任务的唯一标识符
            
        Returns:
            dict: 包含状态、进度、统计计数的字典数据
        """
        # 1. 优先读取持久化数据库，实现重启后的持久查询
        db_job = get_job_from_db(job_id)
        if db_job:
            return {
                "job_id": db_job["id"],
                "filename": db_job["filename"],
                "state": db_job["state"],
                "total": db_job["total"],
                "processed": db_job["processed"],
                "current_account": "", # 重启加载历史时不显示实时运行账号
                "success_count": db_job["success_count"],
                "failure_count": db_job["failure_count"],
                "needs_review_count": db_job["needs_review_count"],
                "error": db_job["error"],
                "download_ready": db_job["output_bytes"] is not None,
            }

        # 2. 如果数据库未检索到（多为并发创建初期的内存状态），回退检查内存
        job = self._get_job(job_id)
        with self._lock:
            return {
                "job_id": job.id,
                "filename": job.filename,
                "state": job.state,
                "total": job.total,
                "processed": job.processed,
                "current_account": job.current_account,
                "success_count": job.success_count,
                "failure_count": job.failure_count,
                "needs_review_count": job.needs_review_count,
                "error": job.error,
                "download_ready": job.output is not None,
            }

    def get_download(self, job_id: str) -> tuple[bytes, str]:
        """
        获取处理完成的任务生成的 Excel 文件数据与对应的输出文件名。支持重启后从数据库直接读取。
        
        Args:
            job_id: 任务的唯一标识符
            
        Returns:
            tuple[bytes, str]: 包含 Excel 文件字节流和推荐文件名的元组
        """
        # 1. 优先从数据库中获取已保存的输出文件字节
        db_job = get_job_from_db(job_id)
        if db_job and db_job["output_bytes"] is not None:
            return db_job["output_bytes"], _classified_filename(db_job["filename"])

        # 2. 回退内存读取
        job = self._get_job(job_id)
        with self._lock:
            if job.output is None:
                raise ValueError("Job output is not ready.")
            return job.output, _classified_filename(job.filename)

    def _run_job(
        self,
        job_id: str,
        file_bytes: bytes,
        llm_api_key: str | None,
        tavily_api_key: str | None,
    ):
        """
        执行任务的核心工作方法。构建对应的分类器，解析 Excel，并捕获异常状态。
        """
        job = self._get_job(job_id)
        with self._lock:
            job.state = "running"

        # 同步数据库状态为 running
        save_job_to_db(
            job_id=job_id,
            filename=job.filename,
            state="running",
            total=job.total,
            created_at=job.created_at,
            input_bytes=file_bytes
        )

        # 使用传入的临时 API key 生成本次任务专属的分类器
        classifier = self.classifier_factory(
            llm_api_key=llm_api_key,
            tavily_api_key=tavily_api_key,
        )

        try:
            output = process_workbook(
                file_bytes,
                classifier,
                progress_callback=lambda event: self._update_progress(job_id, event),
                job_id=job_id, # 传参以允许明细行结果自动入库
            )
        except Exception as exc:
            # 捕获解析 Excel 或全局未处理的错误，将任务标为失败
            with self._lock:
                job.state = "failed"
                job.error = str(exc)
                job.completed_at = time.time()
                
                # 同步写入数据库失败记录
                save_job_to_db(
                    job_id=job_id,
                    filename=job.filename,
                    state="failed",
                    total=job.total,
                    created_at=job.created_at,
                    input_bytes=file_bytes,
                    output_bytes=None,
                    processed=job.processed,
                    success_count=job.success_count,
                    failure_count=job.failure_count,
                    needs_review_count=job.needs_review_count,
                    error=job.error,
                    completed_at=job.completed_at
                )
            return

        with self._lock:
            job.output = output
            # 若有部分行处理失败，状态设为 completed_with_errors
            job.state = "completed" if job.failure_count == 0 else "completed_with_errors"
            job.completed_at = time.time()
            
            # 同步写入数据库完成状态和生成的 Excel 字节流
            save_job_to_db(
                job_id=job_id,
                filename=job.filename,
                state=job.state,
                total=job.total,
                created_at=job.created_at,
                input_bytes=file_bytes,
                output_bytes=job.output,
                processed=job.processed,
                success_count=job.success_count,
                failure_count=job.failure_count,
                needs_review_count=job.needs_review_count,
                error="",
                completed_at=job.completed_at
            )

    def _update_progress(self, job_id: str, event: dict):
        """
        Excel 处理器每处理完一个账号时回调的方法，用于更新任务在内存中的统计数据与当前正在处理的账号。
        并同时将增量进度更新同步写入本地数据库中，实现前台实时进度的双重保障。
        """
        job = self._get_job(job_id)
        result = event.get("result")
        with self._lock:
            job.total = event.get("total", job.total)
            job.processed = event.get("processed", job.processed)
            job.current_account = event.get("current_account", job.current_account)
            # 只有当分类正常返回且没有标记为 failed 时，才算分类成功
            if result and result.account_type_text and not result.failed:
                job.success_count += 1
            # 记录需要复核的计数和真实的报错计数
            if result and result.needs_review:
                job.needs_review_count += 1
                if result.failed:
                    job.failure_count += 1

            # 同步更新数据库的任务状态与进度
            save_job_to_db(
                job_id=job_id,
                filename=job.filename,
                state=job.state,
                total=job.total,
                created_at=job.created_at,
                input_bytes=b"", # 避免重复写入大文件字节流
                output_bytes=job.output,
                processed=job.processed,
                success_count=job.success_count,
                failure_count=job.failure_count,
                needs_review_count=job.needs_review_count,
                error=job.error,
                completed_at=job.completed_at
            )

    def _get_job(self, job_id: str) -> Job:
        """
        线程安全地根据 job_id 获取 Job 实例，不存在时抛出 KeyError。
        """
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            raise KeyError(f"Unknown job_id: {job_id}")
        return job

    def _prune_expired_jobs_locked(self, now: float):
        """
        清除所有已经结束（完成或失败）且超出保留时间的内存与数据库任务记录。
        调用此方法时必须已持有 self._lock。
        """
        expired_job_ids = [
            job_id
            for job_id, job in self._jobs.items()
            if job.completed_at is not None
            and now - job.completed_at > self.retention_seconds
        ]
        for job_id in expired_job_ids:
            del self._jobs[job_id]
            # 同步删除数据库中的过期任务明细和元数据，避免数据库膨胀
            from app.db import delete_job_from_db
            delete_job_from_db(job_id)


def _classified_filename(filename: str) -> str:
    """
    根据原始文件名生成转换后的输出文件名 (在原文件名前缀后加 _classified)。
    """
    stem = filename.rsplit(".", 1)[0]
    return f"{stem}_classified.xlsx"
