from __future__ import annotations

from io import BytesIO

from openpyxl import load_workbook

from app.classifier import AccountClassification


OUTPUT_HEADERS = [
    "New Account Type",
    "Classification Confidence",
    "Classification Reason",
    "Evidence URLs",
]


def process_workbook(file_bytes: bytes, classifier, progress_callback=None) -> bytes:
    workbook = load_workbook(BytesIO(file_bytes))
    sheet = workbook.worksheets[0]
    account_name_col, account_type_col = _find_required_columns(sheet)
    output_start_col = sheet.max_column + 1
    total_public_rows = len(_public_entity_rows(sheet, account_name_col, account_type_col))
    processed_public_rows = 0
    if progress_callback:
        progress_callback(
            {
                "processed": 0,
                "total": total_public_rows,
                "current_account": "",
                "result": None,
            }
        )

    for offset, header in enumerate(OUTPUT_HEADERS):
        sheet.cell(row=1, column=output_start_col + offset).value = header

    for row in range(2, sheet.max_row + 1):
        account_name = _cell_text(sheet.cell(row=row, column=account_name_col).value)
        account_type = _cell_text(sheet.cell(row=row, column=account_type_col).value)
        if account_type.casefold() == "public entity".casefold() and account_name:
            try:
                result = classifier.classify(account_name)
            except Exception as exc:
                result = AccountClassification(
                    account_types=["Needs Review"],
                    confidence="Low",
                    reason=f"Classification failed for this account: {exc}",
                    evidence_urls=[],
                )
            values = [
                result.account_type_text,
                result.confidence,
                result.reason,
                result.evidence_url_text,
            ]
            processed_public_rows += 1
            if progress_callback:
                progress_callback(
                    {
                        "processed": processed_public_rows,
                        "total": total_public_rows,
                        "current_account": account_name,
                        "result": result,
                    }
                )
        else:
            values = [account_type, "", "", ""]

        for offset, value in enumerate(values):
            sheet.cell(row=row, column=output_start_col + offset).value = value

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _find_required_columns(sheet):
    headers = {
        _normalize_header(sheet.cell(row=1, column=col).value): col
        for col in range(1, sheet.max_column + 1)
    }
    account_name_col = headers.get("account name")
    account_type_col = headers.get("account type")
    if not account_name_col or not account_type_col:
        raise ValueError("Missing required columns: Account Name and Account Type.")
    return account_name_col, account_type_col


def _public_entity_rows(sheet, account_name_col, account_type_col) -> list[int]:
    rows = []
    for row in range(2, sheet.max_row + 1):
        account_name = _cell_text(sheet.cell(row=row, column=account_name_col).value)
        account_type = _cell_text(sheet.cell(row=row, column=account_type_col).value)
        if account_name and account_type.casefold() == "public entity".casefold():
            rows.append(row)
    return rows


def _normalize_header(value) -> str:
    return _cell_text(value).casefold()


def _cell_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()
