from __future__ import annotations

from io import BytesIO

from openpyxl import load_workbook


OUTPUT_HEADERS = [
    "New Account Type",
    "Classification Confidence",
    "Classification Reason",
    "Evidence URLs",
]


def process_workbook(file_bytes: bytes, classifier) -> bytes:
    workbook = load_workbook(BytesIO(file_bytes))
    sheet = workbook.worksheets[0]
    account_name_col, account_type_col = _find_required_columns(sheet)
    output_start_col = sheet.max_column + 1

    for offset, header in enumerate(OUTPUT_HEADERS):
        sheet.cell(row=1, column=output_start_col + offset).value = header

    for row in range(2, sheet.max_row + 1):
        account_name = _cell_text(sheet.cell(row=row, column=account_name_col).value)
        account_type = _cell_text(sheet.cell(row=row, column=account_type_col).value)
        if account_type.casefold() == "public entity".casefold() and account_name:
            result = classifier.classify(account_name)
            values = [
                result.account_type_text,
                result.confidence,
                result.reason,
                result.evidence_url_text,
            ]
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


def _normalize_header(value) -> str:
    return _cell_text(value).casefold()


def _cell_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()
