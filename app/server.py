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
from app.excel_processor import process_workbook
from app.search import WebSearchClient


MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class AccountToolHandler(BaseHTTPRequestHandler):
    server_version = "AccountTypeTool/1.0"

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        html = _index_html()
        self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")

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
            file_bytes = self.rfile.read(length)
            api_key = self.headers.get("X-LLM-API-Key") or None
            search_client = WebSearchClient(
                tavily_api_key=self.headers.get("X-Tavily-API-Key") or None,
                serpapi_api_key=self.headers.get("X-SerpAPI-Key") or None,
                bing_search_api_key=self.headers.get("X-Bing-Search-API-Key") or None,
            )
            classifier = LlmEvidenceClassifier(
                search_client,
                OpenAICompatibleLlmClient(api_key=api_key),
            )
            output = process_workbook(file_bytes, classifier)
            output_name = _classified_filename(filename)
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{urllib.parse.quote(output_name)}",
            )
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)
        except Exception as exc:
            traceback.print_exc()
            self._send_json_error(str(exc), 500)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json_error(self, message: str, status: int):
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run(host: str = "127.0.0.1", port: int = 8000):
    server = ThreadingHTTPServer((host, port), AccountToolHandler)
    print(f"Account Type tool is running at http://{host}:{port}")
    print("Use the web page API key field, or set DEEPSEEK_API_KEY before processing files.")
    server.serve_forever()


def _classified_filename(filename: str) -> str:
    stem, _ext = os.path.splitext(os.path.basename(filename))
    return f"{stem}_classified.xlsx"


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
    input[type=file] {
      display: block;
      width: 100%;
      box-sizing: border-box;
      padding: 14px;
      border: 1px dashed #9aa4b2;
      border-radius: 6px;
      background: #fbfcff;
      margin-bottom: 18px;
    }
    input[type=password] {
      display: block;
      width: 100%;
      box-sizing: border-box;
      padding: 12px 14px;
      border: 1px solid #c7cedb;
      border-radius: 6px;
      margin-bottom: 18px;
      font-size: 14px;
    }
    button {
      min-height: 42px;
      padding: 0 18px;
      border: 0;
      border-radius: 6px;
      background: #2563eb;
      color: white;
      font-weight: 700;
      cursor: pointer;
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
    }
    .error { color: #b91c1c; }
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
      <button id="submit">处理并下载</button>
      <div id="status"></div>
    </section>
  </main>
  <script>
    const fileInput = document.getElementById("file");
    const apiKeyInput = document.getElementById("apiKey");
    const tavilyKeyInput = document.getElementById("tavilyKey");
    const submit = document.getElementById("submit");
    const status = document.getElementById("status");

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
      setStatus("处理中：正在联网检索并调用 DeepSeek，账号较多时会需要几分钟。", false);
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
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = file.name.replace(/\\.[^.]+$/, "") + "_classified.xlsx";
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
        setStatus("处理完成，已开始下载。", false);
      } catch (error) {
        setStatus(error.message, true);
      } finally {
        submit.disabled = false;
      }
    });

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
