from io import BytesIO
import json
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

from openpyxl import Workbook, load_workbook

from app.classifier import AccountClassification
from app.jobs import JobManager
from app.server import create_server


class FakeClassifierFactory:
    def __call__(self, llm_api_key=None, tavily_api_key=None):
        return FakeClassifier()


class FakeClassifier:
    def classify(self, account_name):
        return AccountClassification(
            account_types=["Private Enterprise(POE)"],
            confidence="Medium",
            reason="LLM fallback - no public evidence found. Name-based classification.",
            evidence_urls=[],
        )


def workbook_bytes():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Account Name", "Account Type"])
    sheet.append(["Alibaba Group Holding Limited", "Public Entity"])
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


class ServerJobApiTests(unittest.TestCase):
    def test_process_status_and_download_job_flow(self):
        manager = JobManager(FakeClassifierFactory())
        server = create_server("127.0.0.1", 0, job_manager=manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            request = urllib.request.Request(
                f"{base_url}/process",
                data=workbook_bytes(),
                headers={"X-Filename": "accounts.xlsx", "X-LLM-API-Key": "test"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertIn("job_id", payload)

            status = _wait_for_complete(base_url, payload["job_id"])
            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["processed"], 1)
            self.assertEqual(status["total"], 1)

            with urllib.request.urlopen(
                f"{base_url}/download?{urllib.parse.urlencode({'job_id': payload['job_id']})}",
                timeout=5,
            ) as response:
                output = response.read()
            workbook = load_workbook(BytesIO(output))
            self.assertEqual(workbook.active.cell(2, 3).value, "Private Enterprise(POE)")
        finally:
            server.shutdown()
            server.server_close()


def _wait_for_complete(base_url, job_id):
    for _ in range(20):
        with urllib.request.urlopen(
            f"{base_url}/status?{urllib.parse.urlencode({'job_id': job_id})}",
            timeout=5,
        ) as response:
            status = json.loads(response.read().decode("utf-8"))
        if status["state"] in {"completed", "completed_with_errors", "failed"}:
            return status
        time.sleep(0.05)
    raise AssertionError("job did not complete")


if __name__ == "__main__":
    unittest.main()
