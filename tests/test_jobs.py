from io import BytesIO
import time
import unittest

from openpyxl import Workbook, load_workbook

from app.classifier import AccountClassification
from app.jobs import JobManager


class FakeClassifierFactory:
    def __call__(self, llm_api_key=None, tavily_api_key=None):
        return FakeClassifier()


class ReviewClassifierFactory:
    def __call__(self, llm_api_key=None, tavily_api_key=None):
        return ReviewClassifier()


class FakeClassifier:
    def classify(self, account_name):
        return AccountClassification(
            account_types=["Multinational Corporation(MNC)"],
            confidence="Medium",
            reason="LLM fallback - no public evidence found. Known multinational company.",
            evidence_urls=[],
        )


class ReviewClassifier:
    def classify(self, account_name):
        return AccountClassification(
            account_types=["Other"],
            confidence="Low",
            reason="Insufficient evidence; default recommendation needs review.",
            evidence_urls=[],
            review_status="Needs Review",
        )


class SearchFailureReviewClassifierFactory:
    def __call__(self, llm_api_key=None, tavily_api_key=None):
        return SearchFailureReviewClassifier()


class SearchFailureReviewClassifier:
    def classify(self, account_name):
        return AccountClassification(
            account_types=["Private Enterprise(POE)"],
            confidence="Medium",
            reason=(
                "Search failed for this account: HTTP Error 403: Forbidden. "
                "LLM fallback still produced a recommendation."
            ),
            evidence_urls=[],
            review_status="Needs Review",
        )


def sample_workbook_bytes():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Account Name", "Account Type"])
    sheet.append(["Mitsui & Co., Ltd.", "Public Entity"])
    sheet.append(["Tyson Foods, Inc.", None])
    sheet.append(["Existing Private Co", "Private Enterprise(POE)"])
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


class JobManagerTests(unittest.TestCase):
    def setUp(self):
        import app.db
        app.db.DB_PATH = "test_cleanse_tool.db"
        app.db.init_db()
        with app.db.get_db_connection() as conn:
            conn.execute("DELETE FROM classification_cache")
            conn.execute("DELETE FROM job_results")
            conn.execute("DELETE FROM jobs")
            conn.commit()

    def tearDown(self):
        import os
        if os.path.exists("test_cleanse_tool.db"):
            try:
                os.remove("test_cleanse_tool.db")
            except OSError:
                pass

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
        self.assertEqual(workbook.active.cell(3, 3).value, "Multinational Corporation(MNC)")
        self.assertEqual(workbook.active.cell(4, 3).value, "Private Enterprise(POE)")

    def test_review_status_counts_separately_from_recommended_type(self):
        manager = JobManager(ReviewClassifierFactory())

        job_id = manager.create_job(
            sample_workbook_bytes(),
            filename="accounts.xlsx",
            llm_api_key="test",
            tavily_api_key="",
            run_inline=True,
        )

        status = manager.get_status(job_id)
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["success_count"], 2)
        self.assertEqual(status["needs_review_count"], 2)
        self.assertEqual(status["failure_count"], 0)

        output, _filename = manager.get_download(job_id)
        workbook = load_workbook(BytesIO(output))
        self.assertEqual(workbook.active.cell(2, 3).value, "Other")
        self.assertEqual(workbook.active.cell(2, 5).value, "Needs Review")

    def test_search_failure_review_does_not_count_as_processing_failure(self):
        manager = JobManager(SearchFailureReviewClassifierFactory())

        job_id = manager.create_job(
            sample_workbook_bytes(),
            filename="accounts.xlsx",
            llm_api_key="test",
            tavily_api_key="",
            run_inline=True,
        )

        status = manager.get_status(job_id)
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["success_count"], 2)
        self.assertEqual(status["needs_review_count"], 2)
        self.assertEqual(status["failure_count"], 0)

    def test_completed_jobs_are_pruned_after_retention_window(self):
        manager = JobManager(FakeClassifierFactory(), retention_seconds=0.001)

        first_job_id = manager.create_job(
            sample_workbook_bytes(),
            filename="first.xlsx",
            llm_api_key="test",
            tavily_api_key="",
            run_inline=True,
        )
        time.sleep(0.01)
        second_job_id = manager.create_job(
            sample_workbook_bytes(),
            filename="second.xlsx",
            llm_api_key="test",
            tavily_api_key="",
            run_inline=True,
        )

        with self.assertRaises(KeyError):
            manager.get_status(first_job_id)
        self.assertEqual(manager.get_status(second_job_id)["state"], "completed")

    def test_classification_failure_counts_as_failure_but_not_success(self):
        class ExceptionClassifier:
            def classify(self, account_name):
                raise RuntimeError("LLM request failed")

        class ExceptionClassifierFactory:
            def __call__(self, llm_api_key=None, tavily_api_key=None):
                return ExceptionClassifier()

        manager = JobManager(ExceptionClassifierFactory())

        job_id = manager.create_job(
            sample_workbook_bytes(),
            filename="accounts.xlsx",
            llm_api_key="test",
            tavily_api_key="",
            run_inline=True,
        )

        status = manager.get_status(job_id)
        self.assertEqual(status["state"], "completed_with_errors")
        self.assertEqual(status["success_count"], 0)
        self.assertEqual(status["needs_review_count"], 2)
        self.assertEqual(status["failure_count"], 2)


if __name__ == "__main__":
    unittest.main()
