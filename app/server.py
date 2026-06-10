from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
import os
import sys
import traceback
import urllib.parse

from app.classifier import LlmEvidenceClassifier, OpenAICompatibleLlmClient
from app.jobs import JobManager
from app.search import WebSearchClient


MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class AccountToolHandler(BaseHTTPRequestHandler):
    server_version = "AccountTypeTool/1.1"

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self._send_bytes(_index_html().encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/status":
            self._handle_status()
            return
        if path == "/download":
            self._handle_download()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/process":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self._send_json_error("Upload must be a non-empty Excel file smaller than 25MB.", 400)
            return

        filename = urllib.parse.unquote(self.headers.get("X-Filename", "accounts.xlsx"))
        if not filename.lower().endswith((".xlsx", ".xlsm")):
            self._send_json_error("Only .xlsx and .xlsm files are supported.", 400)
            return

        try:
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

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _handle_status(self):
        job_id = _query_param(self.path, "job_id")
        if not job_id:
            self._send_json_error("Missing job_id.", 400)
            return
        try:
            self._send_json(self.server.job_manager.get_status(job_id))
        except KeyError as exc:
            self._send_json_error(str(exc), 404)

    def _handle_download(self):
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
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json_error(self, message: str, status: int):
        self._send_json({"error": message}, status)

    def _send_bytes(self, body: bytes, content_type: str):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(host: str, port: int, job_manager: JobManager | None = None):
    server = ThreadingHTTPServer((host, port), AccountToolHandler)
    server.job_manager = job_manager or JobManager(_build_classifier)
    return server


def run(host: str = "127.0.0.1", port: int = 8000):
    server = create_server(host, port)
    print(f"Account Type tool is running at http://{host}:{port}")
    print("Use the web page API key field, or set DEEPSEEK_API_KEY before processing files.")
    server.serve_forever()


def _build_classifier(llm_api_key: str | None = None, tavily_api_key: str | None = None):
    return LlmEvidenceClassifier(
        WebSearchClient(tavily_api_key=tavily_api_key),
        OpenAICompatibleLlmClient(api_key=llm_api_key),
    )


def _query_param(path: str, name: str) -> str:
    parsed = urllib.parse.urlparse(path)
    return urllib.parse.parse_qs(parsed.query).get(name, [""])[0]


def _index_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Account Type 清洗工具</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Arial, "Microsoft YaHei", sans-serif;
      background: #f7f8fb;
      color: #1f2937;
    }
    body { margin: 0; }
    main {
      max-width: 760px;
      margin: 56px auto;
      padding: 0 24px;
    }
    h1 {
      font-size: 28px;
      margin: 0 0 12px;
    }
    p {
      margin: 0 0 22px;
      line-height: 1.6;
      color: #4b5563;
    }
    .panel {
      background: #ffffff;
      border: 1px solid #d9dee8;
      border-radius: 8px;
      padding: 24px;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06);
    }
    label {
      display: block;
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 10px;
    }
    input[type=file],
    input[type=password] {
      display: block;
      width: 100%;
      box-sizing: border-box;
      padding: 12px 14px;
      border: 1px solid #c7cedb;
      border-radius: 6px;
      margin-bottom: 18px;
      font-size: 14px;
      background: #ffffff;
    }
    input[type=file] {
      padding: 14px;
      border-style: dashed;
      background: #fbfcff;
    }
    button, a.button {
      display: inline-flex;
      align-items: center;
      min-height: 42px;
      padding: 0 18px;
      border: 0;
      border-radius: 6px;
      background: #2563eb;
      color: white;
      font-weight: 700;
      cursor: pointer;
      text-decoration: none;
      font-size: 14px;
    }
    button:disabled {
      background: #9aa4b2;
      cursor: not-allowed;
    }
    #status {
      margin-top: 16px;
      min-height: 24px;
      font-size: 14px;
      color: #374151;
      white-space: pre-wrap;
      line-height: 1.6;
    }
    .error { color: #b91c1c; }
    .progress {
      margin-top: 16px;
      width: 100%;
      height: 10px;
      border-radius: 999px;
      background: #e5e7eb;
      overflow: hidden;
    }
    .bar {
      height: 100%;
      width: 0%;
      background: #2563eb;
      transition: width 150ms ease;
    }
    .hidden { display: none; }
  </style>
</head>
<body>
  <main>
    <h1>Account Type 清洗工具</h1>
    <p>上传包含 Account Name 和 Account Type 的 Excel。工具会联网检索 Public Entity 账号的公开信息，并调用 DeepSeek 输出新分类、置信度、原因和证据链接。</p>
    <section class="panel">
      <label for="apiKey">DeepSeek API Key（仅本次请求使用，不保存）</label>
      <input id="apiKey" type="password" autocomplete="off" placeholder="sk-...">
      <label for="tavilyKey">Tavily Search API Key（可选，用于更稳定的联网检索）</label>
      <input id="tavilyKey" type="password" autocomplete="off" placeholder="tvly-...">
      <label for="file">选择 Excel 文件（.xlsx / .xlsm）</label>
      <input id="file" type="file" accept=".xlsx,.xlsm">
      <button id="submit">处理</button>
      <a id="download" class="button hidden" href="#">下载结果</a>
      <div class="progress"><div id="bar" class="bar"></div></div>
      <div id="status"></div>
    </section>
  </main>
  <script>
    const fileInput = document.getElementById("file");
    const apiKeyInput = document.getElementById("apiKey");
    const tavilyKeyInput = document.getElementById("tavilyKey");
    const submit = document.getElementById("submit");
    const download = document.getElementById("download");
    const status = document.getElementById("status");
    const bar = document.getElementById("bar");
    let pollTimer = null;

    submit.addEventListener("click", async () => {
      const file = fileInput.files[0];
      if (!file) {
        setStatus("请先选择 Excel 文件。", true);
        return;
      }
      const apiKey = apiKeyInput.value.trim();
      if (!apiKey) {
        setStatus("请先输入 DeepSeek API Key。", true);
        return;
      }
      submit.disabled = true;
      download.classList.add("hidden");
      bar.style.width = "0%";
      setStatus("已提交，正在创建处理任务。", false);

      try {
        const headers = {
          "X-Filename": encodeURIComponent(file.name),
          "X-LLM-API-Key": apiKey
        };
        const tavilyKey = tavilyKeyInput.value.trim();
        if (tavilyKey) {
          headers["X-Tavily-API-Key"] = tavilyKey;
        }
        const response = await fetch("/process", {
          method: "POST",
          headers,
          body: await file.arrayBuffer()
        });
        if (!response.ok) {
          const error = await response.json().catch(() => ({ error: response.statusText }));
          throw new Error(error.error || "处理失败");
        }
        const payload = await response.json();
        pollStatus(payload.job_id, file.name);
      } catch (error) {
        submit.disabled = false;
        setStatus(error.message, true);
      }
    });

    async function pollStatus(jobId, originalName) {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(async () => {
        try {
          const response = await fetch(`/status?job_id=${encodeURIComponent(jobId)}`);
          const state = await response.json();
          if (!response.ok) throw new Error(state.error || "状态查询失败");
          renderStatus(state);
          if (["completed", "completed_with_errors", "failed"].includes(state.state)) {
            clearInterval(pollTimer);
            pollTimer = null;
            submit.disabled = false;
            if (state.download_ready) {
              download.href = `/download?job_id=${encodeURIComponent(jobId)}`;
              download.download = originalName.replace(/\\.[^.]+$/, "") + "_classified.xlsx";
              download.classList.remove("hidden");
            }
          }
        } catch (error) {
          clearInterval(pollTimer);
          pollTimer = null;
          submit.disabled = false;
          setStatus(error.message, true);
        }
      }, 1000);
    }

    function renderStatus(state) {
      const total = state.total || 0;
      const processed = state.processed || 0;
      const percent = total ? Math.round((processed / total) * 100) : 0;
      bar.style.width = `${percent}%`;
      const current = state.current_account ? `\\n当前账号：${state.current_account}` : "";
      const counts = `\\n成功：${state.success_count}  Needs Review：${state.needs_review_count}  失败：${state.failure_count}`;
      if (state.state === "failed") {
        setStatus(`任务失败：${state.error || "未知错误"}`, true);
      } else if (state.state === "completed_with_errors") {
        setStatus(`部分失败但已生成文件。已处理 ${processed}/${total}${counts}`, false);
      } else if (state.state === "completed") {
        setStatus(`已完成。已处理 ${processed}/${total}${counts}`, false);
      } else {
        setStatus(`处理中。已处理 ${processed}/${total}${current}${counts}`, false);
      }
    }

    function setStatus(message, isError) {
      status.textContent = message;
      status.className = isError ? "error" : "";
    }
  </script>
</body>
</html>"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    run(args.host, args.port)
