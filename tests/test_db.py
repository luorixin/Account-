from io import BytesIO
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from app.classifier import AccountClassification


class DbPersistenceTests(unittest.TestCase):
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
        if os.path.exists("test_cleanse_tool.db"):
            try:
                os.remove("test_cleanse_tool.db")
            except OSError:
                pass

    def test_db_connection_context_closes_connection(self):
        import app.db

        with app.db.get_db_connection() as conn:
            conn.execute("SELECT 1")

        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_importing_db_module_does_not_create_default_database_file(self):
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import app.db",
                ],
                cwd=tmpdir,
                env={**os.environ, "PYTHONPATH": str(repo_root)},
                check=True,
            )
            self.assertFalse(Path(tmpdir, "cleanse_tool.db").exists())

    def test_update_row_recounts_failures_from_structured_failed_flag(self):
        import app.db

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Account Name", "Account Type"])
        sheet.append(["Failed Co", "Public Entity"])
        sheet.append(["Review Co", "Public Entity"])
        stream = BytesIO()
        workbook.save(stream)

        app.db.save_job_to_db(
            job_id="job-1",
            filename="accounts.xlsx",
            state="completed_with_errors",
            total=2,
            created_at=1.0,
            input_bytes=stream.getvalue(),
        )
        app.db.save_job_results_to_db(
            "job-1",
            [
                {
                    "row_index": 2,
                    "account_name": "Failed Co",
                    "original_account_type": "Public Entity",
                    "new_account_type": "Other",
                    "confidence": "Low",
                    "reason": "Classification failed for this account: timeout",
                    "evidence_urls": "",
                    "review_status": "Needs Review",
                    "is_target": 1,
                    "failed": True,
                },
                {
                    "row_index": 3,
                    "account_name": "Review Co",
                    "original_account_type": "Public Entity",
                    "new_account_type": "Other",
                    "confidence": "Low",
                    "reason": "Insufficient evidence.",
                    "evidence_urls": "",
                    "review_status": "Needs Review",
                    "is_target": 1,
                    "failed": False,
                },
            ],
        )

        counts = app.db.update_row_classification_in_db(
            "job-1",
            row_index=3,
            new_account_type="Other",
            review_status="Needs Review",
            reason="Still needs manual review.",
        )

        self.assertEqual(counts["success_count"], 1)
        self.assertEqual(counts["needs_review_count"], 2)
        self.assertEqual(counts["failure_count"], 1)

    def test_only_confident_reviewed_classifications_are_cached(self):
        import app.db

        app.db.save_cached_classification(
            "Needs Review Co",
            AccountClassification(
                account_types=["Other"],
                confidence="Low",
                reason="No public evidence found.",
                evidence_urls=[],
                review_status="Needs Review",
            ),
        )
        app.db.save_cached_classification(
            "Reviewed Co",
            AccountClassification(
                account_types=["Private Enterprise(POE)"],
                confidence="Medium",
                reason="Evidence supports private ownership.",
                evidence_urls=["https://example.com/source"],
            ),
        )

        self.assertIsNone(app.db.get_cached_classification("Needs Review Co"))
        self.assertEqual(
            app.db.get_cached_classification("Reviewed Co").account_types,
            ["Private Enterprise(POE)"],
        )


if __name__ == "__main__":
    unittest.main()
