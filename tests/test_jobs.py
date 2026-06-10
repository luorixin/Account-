from io import BytesIO
import unittest

from openpyxl import Workbook, load_workbook

from app.classifier import AccountClassification
from app.jobs import JobManager


class FakeClassifierFactory:
    def __call__(self, llm_api_key=None, tavily_api_key=None):
        return FakeClassifier()


class FakeClassifier:
    def classify(self, account_name):
        return AccountClassification(
            account_types=["Multinational Corporation(MNC)"],
            confidence="Medium",
            reason="LLM fallback - no public evidence found. Known multinational company.",
            evidence_urls=[],
        )


def sample_workbook_bytes():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Account Name", "Account Type"])
    sheet.append(["Mitsui & Co., Ltd.", "Public Entity"])
    sheet.append(["Tyson Foods, Inc.", "Public Entity"])
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


class JobManagerTests(unittest.TestCase):
    def test_job_reports_progress_and_exposes_download_when_complete(self):
        manager = JobManager(FakeClassifierFactory())

        job_id = manager.create_job(
            sample_workbook_bytes(),
            filename="accounts.xlsx",
            llm_api_key="test",
            tavily_api_key="",
            run_inline=True,
        )

        status = manager.get_status(job_id)
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["total"], 2)
        self.assertEqual(status["processed"], 2)
        self.assertEqual(status["success_count"], 2)
        self.assertEqual(status["needs_review_count"], 0)

        output, filename = manager.get_download(job_id)
        self.assertEqual(filename, "accounts_classified.xlsx")
        workbook = load_workbook(BytesIO(output))
        self.assertEqual(workbook.active.cell(2, 3).value, "Multinational Corporation(MNC)")


if __name__ == "__main__":
    unittest.main()
