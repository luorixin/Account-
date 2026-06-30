from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
import uuid

from app.excel_processor import process_workbook


@dataclass
class Job:
    id: str
    filename: str
    state: str = "queued"
    total: int = 0
    processed: int = 0
    current_account: str = ""
    success_count: int = 0
    failure_count: int = 0
    needs_review_count: int = 0
    error: str = ""
    output: bytes | None = None
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None


class JobManager:
    def __init__(self, classifier_factory):
        self.classifier_factory = classifier_factory
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()

    def create_job(
        self,
        file_bytes: bytes,
        filename: str,
        llm_api_key: str | None,
        tavily_api_key: str | None,
        run_inline: bool = False,
    ) -> str:
        job = Job(id=uuid.uuid4().hex, filename=filename)
        with self._lock:
            self._jobs[job.id] = job

        args = (job.id, file_bytes, llm_api_key, tavily_api_key)
        if run_inline:
            self._run_job(*args)
        else:
            thread = threading.Thread(target=self._run_job, args=args, daemon=True)
            thread.start()
        return job.id

    def get_status(self, job_id: str) -> dict:
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
        job = self._get_job(job_id)
        with self._lock:
            job.state = "running"

        classifier = self.classifier_factory(
            llm_api_key=llm_api_key,
            tavily_api_key=tavily_api_key,
        )

        try:
            output = process_workbook(
                file_bytes,
                classifier,
                progress_callback=lambda event: self._update_progress(job_id, event),
            )
        except Exception as exc:
            with self._lock:
                job.state = "failed"
                job.error = str(exc)
                job.completed_at = time.time()
            return

        with self._lock:
            job.output = output
            job.state = "completed" if job.failure_count == 0 else "completed_with_errors"
            job.completed_at = time.time()

    def _update_progress(self, job_id: str, event: dict):
        job = self._get_job(job_id)
        result = event.get("result")
        with self._lock:
            job.total = event.get("total", job.total)
            job.processed = event.get("processed", job.processed)
            job.current_account = event.get("current_account", job.current_account)
            if result and result.account_type_text:
                job.success_count += 1
            if result and result.needs_review:
                job.needs_review_count += 1
                if "failed" in result.reason.casefold():
                    job.failure_count += 1

    def _get_job(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            raise KeyError(f"Unknown job_id: {job_id}")
        return job


def _classified_filename(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0]
    return f"{stem}_classified.xlsx"
