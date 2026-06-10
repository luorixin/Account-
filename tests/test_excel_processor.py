from io import BytesIO
import unittest

from openpyxl import Workbook, load_workbook

from app.classifier import AccountClassification
from app.excel_processor import process_workbook


class FakeClassifier:
    def __init__(self):
        self.seen = []

    def classify(self, account_name):
        self.seen.append(account_name)
        return AccountClassification(
            account_types=["State-owned Enterprise(SOE)"],
            confidence="High",
            reason=f"{account_name} has public evidence of state ownership.",
            evidence_urls=["https://example.com/source"],
        )


class PartiallyFailingClassifier:
    def __init__(self):
        self.seen = []

    def classify(self, account_name):
        self.seen.append(account_name)
        if account_name == "Public Co":
            raise TimeoutError("LLM request timed out")
        return AccountClassification(
            account_types=["Multinational Corporation(MNC)"],
            confidence="Medium",
            reason="Fallback classification.",
            evidence_urls=[],
        )


def workbook_bytes():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Accounts"
    sheet.append(["Account Name", "Account Type", "Region"])
    sheet.append(["Public Co", "Public Entity", "CN"])
    sheet.append(["Private Co", "Private Enterprise(POE)", "US"])
    other = workbook.create_sheet("Do Not Touch")
    other.append(["Account Name", "Account Type"])
    other.append(["Other Public", "Public Entity"])

    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


class ExcelProcessorTests(unittest.TestCase):
    def test_processes_first_sheet_and_appends_classification_columns(self):
        classifier = FakeClassifier()

        output = process_workbook(workbook_bytes(), classifier)

        result = load_workbook(BytesIO(output))
        sheet = result["Accounts"]
        self.assertEqual(
            [sheet.cell(1, col).value for col in range(1, 8)],
            [
                "Account Name",
                "Account Type",
                "Region",
                "New Account Type",
                "Classification Confidence",
                "Classification Reason",
                "Evidence URLs",
            ],
        )
        self.assertEqual(sheet.cell(2, 4).value, "State-owned Enterprise(SOE)")
        self.assertEqual(sheet.cell(2, 5).value, "High")
        self.assertIn("state ownership", sheet.cell(2, 6).value)
        self.assertEqual(sheet.cell(2, 7).value, "https://example.com/source")
        self.assertEqual(sheet.cell(3, 4).value, "Private Enterprise(POE)")
        self.assertEqual(classifier.seen, ["Public Co"])
        self.assertEqual(result["Do Not Touch"].max_column, 2)

    def test_missing_required_columns_raises_value_error(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Name", "Type"])
        stream = BytesIO()
        workbook.save(stream)

        with self.assertRaisesRegex(ValueError, "Account Name.*Account Type"):
            process_workbook(stream.getvalue(), FakeClassifier())

    def test_classifier_failure_marks_row_needs_review_and_continues(self):
        classifier = PartiallyFailingClassifier()

        output = process_workbook(workbook_bytes(), classifier)

        result = load_workbook(BytesIO(output))
        sheet = result["Accounts"]
        self.assertEqual(sheet.cell(2, 4).value, "Needs Review")
        self.assertEqual(sheet.cell(2, 5).value, "Low")
        self.assertIn("Classification failed for this account", sheet.cell(2, 6).value)
        self.assertEqual(sheet.cell(3, 4).value, "Private Enterprise(POE)")

    def test_progress_callback_reports_public_entity_progress(self):
        events = []

        process_workbook(workbook_bytes(), FakeClassifier(), progress_callback=events.append)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["processed"], 0)
        self.assertEqual(events[0]["total"], 1)
        self.assertEqual(events[1]["processed"], 1)
        self.assertEqual(events[1]["total"], 1)
        self.assertEqual(events[1]["current_account"], "Public Co")


if __name__ == "__main__":
    unittest.main()
