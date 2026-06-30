"""
Web 服务器模块 (app.server)

基于 Python 内置的 http.server 实现轻量级本地 Web API 及嵌入式网页 UI。
核心职责：
- 提供前端网页访问接口；
- 提供文件上传与分类清洗任务创建接口 (/process)；
- 提供异步任务进度轮询接口 (/status)；
- 提供处理完成文件的下载接口 (/download)；
- 新增：提供历史任务拉取接口 (/history)；
- 新增：提供任务行明细结果获取接口 (/job/results)；
- 新增：提供人工修正与复核更新接口 (/job/update_result)。
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
import os
from pathlib import Path
import sys
import traceback
import urllib.parse

from app.classifier import LlmEvidenceClassifier, OpenAICompatibleLlmClient
from app.jobs import JobManager
from app.search import WebSearchClient


# 限制单个上传文件的大小最大为 25 MB
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
EDITABLE_ACCOUNT_TYPES = {
    "State-owned Enterprise(SOE)",
    "Multinational Corporation(MNC)",
    "Private Enterprise(POE)",
    "Other",
}
EDITABLE_REVIEW_STATUSES = {"", "Needs Review"}


class AccountToolHandler(BaseHTTPRequestHandler):
    """
    HTTP 请求处理器，处理所有的 Web 页面请求和任务 API。
    通过扩展 BaseHTTPRequestHandler 并配合 ThreadingHTTPServer 支持多线程并发请求。
    """
    server_version = "AccountTypeTool/1.2"

    def do_GET(self):
        """
        处理 GET 请求，路由包括主页、查询状态、下载结果等。
        """
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            # 返回前端主页面
            self._send_bytes(_index_html().encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/status":
            # 查询任务处理状态和进度进度
            self._handle_status()
            return
        if path == "/download":
            # 下载处理完成后的 Excel 文件
            self._handle_download()
            return
        if path == "/history":
            # 新增：获取历史任务列表
            self._handle_history()
            return
        if path == "/job/results":
            # 新增：获取明细行结果
            self._handle_job_results()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        """
        处理 POST 请求，核心入口为 /process 上传文件并提交异步处理任务。
        以及 /job/update_result 用户在线修改结果。
        """
        path = urllib.parse.urlparse(self.path).path
        if path == "/process":
            self._handle_process()
        elif path == "/job/update_result":
            self._handle_update_result()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _handle_process(self):
        """
        处理上传文件并提交处理任务。
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json_error("Invalid Content-Length.", 400)
            return
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self._send_json_error("Upload must be a non-empty Excel file smaller than 25MB.", 400)
            return

        # 提取上传时的原始文件名，处理非 ASCII 字符转义
        filename = urllib.parse.unquote(self.headers.get("X-Filename", "accounts.xlsx"))
        if not filename.lower().endswith((".xlsx", ".xlsm")):
            self._send_json_error("Only .xlsx and .xlsm files are supported.", 400)
            return

        try:
            # 异步创建并启动清洗任务，将 API Key 局限在请求生命周期内（不写回文件）
            job_id = self.server.job_manager.create_job(
                self.rfile.read(length),
                filename=filename,
                llm_api_key=self.headers.get("X-LLM-API-Key") or None,
                tavily_api_key=self.headers.get("X-Tavily-API-Key") or None,
            )
            self._send_json({"job_id": job_id, "state": "queued"})
        except Exception as exc:
            traceback.print_exc()
            self._send_json_error(str(exc), 500)

    def _handle_update_result(self):
        """
        新增：网页端人工修改某一行分类结果的回调接口。
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body)
        except Exception as exc:
            self._send_json_error(f"Invalid request body: {exc}", 400)
            return

        job_id = data.get("job_id")
        row_index = data.get("row_index")
        new_account_type = data.get("new_account_type")
        review_status = data.get("review_status", "")
        reason = data.get("reason", "")

        if not job_id or row_index is None or not new_account_type:
            self._send_json_error("Missing job_id, row_index, or new_account_type.", 400)
            return
        if new_account_type not in EDITABLE_ACCOUNT_TYPES:
            self._send_json_error("Invalid new_account_type.", 400)
            return
        if review_status not in EDITABLE_REVIEW_STATUSES:
            self._send_json_error("Invalid review_status.", 400)
            return

        from app.db import update_row_classification_in_db
        try:
            # 更新指定行，并更新全局持久化文件字节
            counts = update_row_classification_in_db(
                job_id=job_id,
                row_index=int(row_index),
                new_account_type=new_account_type,
                review_status=review_status,
                reason=reason
            )
            self._send_json({
                "success": True,
                "success_count": counts["success_count"],
                "needs_review_count": counts["needs_review_count"],
                "failure_count": counts["failure_count"]
            })
        except Exception as exc:
            self._send_json_error(str(exc), 500)

    def log_message(self, fmt, *args):
        """
        重写日志输出，使用标准错误流输出。
        """
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _handle_status(self):
        """
        提取查询参数 job_id，获取任务当前的状态。
        """
        job_id = _query_param(self.path, "job_id")
        if not job_id:
            self._send_json_error("Missing job_id.", 400)
            return
        try:
            self._send_json(self.server.job_manager.get_status(job_id))
        except KeyError as exc:
            self._send_json_error(str(exc), 404)

    def _handle_history(self):
        """
        新增：获取历史任务列表。
        """
        from app.db import list_jobs_from_db
        try:
            jobs = list_jobs_from_db()
            self._send_json(jobs)
        except Exception as exc:
            self._send_json_error(str(exc), 500)

    def _handle_job_results(self):
        """
        新增：获取任务的明细行结果。
        """
        job_id = _query_param(self.path, "job_id")
        if not job_id:
            self._send_json_error("Missing job_id.", 400)
            return
        from app.db import get_job_results_from_db
        try:
            results = get_job_results_from_db(job_id)
            self._send_json(results)
        except Exception as exc:
            self._send_json_error(str(exc), 500)

    def _handle_download(self):
        """
        获取处理好的 Excel 字节流以进行文件附件下载。
        """
        job_id = _query_param(self.path, "job_id")
        if not job_id:
            self._send_json_error("Missing job_id.", 400)
            return
        try:
            output, filename = self.server.job_manager.get_download(job_id)
        except KeyError as exc:
            self._send_json_error(str(exc), 404)
            return
        except ValueError as exc:
            # 文件尚未处理完毕
            self._send_json_error(str(exc), 409)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.send_header(
            "Content-Disposition",
            f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}",
        )
        self.send_header("Content-Length", str(len(output)))
        self.end_headers()
        self.wfile.write(output)

    def _send_json(self, payload: dict, status: int = 200):
        """
        向客户端返回 JSON 格式响应。
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json_error(self, message: str, status: int):
        """
        向客户端返回格式化的 JSON 错误信息。
        """
        self._send_json({"error": message}, status)

    def _send_bytes(self, body: bytes, content_type: str):
        """
        向客户端发送指定 Content-Type 的二进制字节流（如 HTML 页面）。
        """
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(host: str, port: int, job_manager: JobManager | None = None):
    """
    构建并返回一个基于多线程的 HTTP 服务器实例。
    """
    server = ThreadingHTTPServer((host, port), AccountToolHandler)
    server.job_manager = job_manager or JobManager(_build_classifier)
    return server


def run(host: str = "127.0.0.1", port: int = 8000):
    """
    服务器的启动运行主入口。
    """
    server = create_server(host, port)
    print(f"Account Type tool is running at http://{host}:{port}")
    print("Use the web page API key field, or set DEEPSEEK_API_KEY before processing files.")
    server.serve_forever()


def _build_classifier(llm_api_key: str | None = None, tavily_api_key: str | None = None):
    """
    根据请求中的 API Key 分别实例化搜索引擎客户端与 LLM 客户端，最终生成分类器实例。
    """
    return LlmEvidenceClassifier(
        WebSearchClient(
            tavily_api_key=tavily_api_key,
            allow_public_fallback=True, # 允许没有 Tavily 密匙时自动使用国内各大公开搜索引擎
        ),
        OpenAICompatibleLlmClient(api_key=llm_api_key),
    )


def _query_param(path: str, name: str) -> str:
    """
    简易解析 GET 查询 URL 中的单个 Query 参数值。
    """
    parsed = urllib.parse.urlparse(path)
    return urllib.parse.parse_qs(parsed.query).get(name, [""])[0]


def _index_html() -> str:
    """
    从 static 目录下读取 index.html 网页文件。
    """
    return _static_file("index.html").read_text(encoding="utf-8")


def _static_file(filename: str) -> Path:
    """
    获取静态资源文件相对当前脚本的完整绝对路径。
    """
    return Path(__file__).with_name("static") / filename



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    run(args.host, args.port)
