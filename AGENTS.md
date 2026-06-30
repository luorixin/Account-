# Agent Instructions

## Project Summary

This repository is a local Account Type cleansing tool. It serves a small web UI at
`http://127.0.0.1:8000`, accepts an Account basic-info Excel workbook, classifies
selected accounts with web evidence plus an OpenAI-compatible LLM, and appends the
normalized classification result columns to the first worksheet.

## Functional Specification

- The app must run locally with `.\run.ps1` from the project root.
- The backend listens on `http://127.0.0.1:8000` by default.
- The web UI must accept only `.xlsx` and `.xlsm` uploads smaller than 25 MB.
- The uploaded workbook must contain `Account Name` and `Account Type` headers on
  the first worksheet. Header matching is case-insensitive after trimming.
- Only the first worksheet is processed. Other worksheets must remain untouched.
- Output columns must be appended after the existing columns in this order:
  `New Account Type`, `Account Type Short`, `Review Status`,
  `Classification Confidence`, `Classification Reason`, `Evidence URLs`.
- Rows are sent to LLM classification only when `Account Name` is non-empty and
  `Account Type` is blank or contains one of these target labels:
  `JV`, `Joint Venture`, `Joint Venture(JV)`, `Joint Venture (JV)`,
  `Public Entity`, `Private Equity`, `Private Equity Investee`.
- If a row contains a higher-priority already-known local type, do not call the
  classifier for that row. Normalize it locally instead.
- Duplicate target account names should be classified once and reused across all
  matching rows.
- Target classifications run concurrently. `CLASSIFICATION_MAX_WORKERS` controls
  concurrency, defaults to `4`, and is clamped to `1..8`.
- Classification failures for a single row must not abort the whole workbook.
  Failed rows should output `Other`, `Low`, a failure reason, and `Needs Review`.
- Job status must report total target rows, processed rows, current account,
  success count, needs-review count, failure count, state, and download readiness.
- Completed downloads should be named `<original_stem>_classified.xlsx`.

## Classification Rules

- Final output account types are intentionally narrowed to:
  `State-owned Enterprise(SOE)`, `Multinational Corporation(MNC)`,
  `Private Enterprise(POE)`, or `Other`.
- `Account Type Short` maps only final output types:
  `SOE`, `MNC`, `POE`, or `Other`.
- Existing account type normalization includes these important mappings:
  `Government Organization(GO)`, `Governmental Organization (GO)`,
  `State-Owned Enterprise (SOE)`, and `Specialized Enterprise (SE/央企)` -> SOE;
  `Private Entity（POE）` -> POE;
  `Multinational Corporation（MNC）` -> MNC;
  `Joint Venture`, `Joint Venture(JV)`, `NGO / Non-profit Organization`, and
  `Non-Profit Organization (NPO)` -> Other.
- When multiple normalized types are present, use priority:
  SOE, then MNC, then POE, then Other.
- LLM raw labels may include broader labels, but final normalization must follow
  the project priority. Government Organization normalizes to SOE; JV and NGO
  normalize to Other.
- A Chinese company with MNC evidence but no government/state ownership should
  output `Private Enterprise(POE)`, not MNC. Chinese state-owned companies still
  output SOE.
- When no public evidence is found, continue with LLM fallback, downgrade High
  confidence to Medium, and mark `Needs Review`.
- Low confidence, unsupported LLM account types, search failure, or conflicting
  Other-vs-private-enterprise reasoning must mark `Needs Review`.

## External Services and Configuration

- LLM access uses an OpenAI-compatible `/chat/completions` endpoint.
- API key resolution order is request header, then `LLM_API_KEY`,
  `DEEPSEEK_API_KEY`, then `OPENAI_API_KEY`.
- Default model is `deepseek-v4-flash`.
- Default base URL is `https://api.deepseek.com`.
- Search provider priority is Tavily, SerpAPI, Bing, then public fallback search
  when enabled.
- The server classifier enables public fallback search. Public fallback order is
  Baidu, Sogou, then 360 search.
- API keys entered in the web UI are request-scoped and must not be written to
  project files.

## Code Map

- `app/server.py` owns HTTP routes, the embedded web UI, upload validation, and
  classifier construction.
- `app/jobs.py` owns asynchronous job lifecycle, progress accounting, and
  download readiness.
- `app/excel_processor.py` owns workbook parsing, target-row detection,
  concurrent classification, local normalization, and output column writes.
- `app/classifier.py` owns evidence-backed LLM classification, type
  normalization, confidence/review logic, and OpenAI-compatible requests.
- `app/search.py` owns search-provider ordering, result parsing, and fallback
  behavior.
- `tests/` contains the regression suite and should be updated with behavior
  changes.

## Development Rules

- Keep this tool dependency-light. Do not add dependencies unless explicitly
  requested or clearly necessary.
- Preserve workbook formatting and non-target sheets as much as openpyxl allows.
- Keep classification behavior deterministic around normalization priorities and
  `Needs Review` triggers.
- Prefer expanding focused tests before changing classification, search fallback,
  Excel processing, or job-status behavior.
- After any backend code update, automatically restart the backend before
  reporting completion.
- Restart the backend with `.\run.ps1` from the project root unless the user gives
  a different command.

## Verification

- Run the regression suite before claiming behavior is complete:

  ```bash
  python -m unittest discover -s tests
  ```

- For HTTP or UI changes, also start the app with `.\run.ps1` and manually verify
  upload, progress polling, completion state, and download.

需求完成写好更新文档，包括新增功能、修复问题、性能优化等。放入 `docs/` 目录。

代码需要有详细的中文注释，包括函数、类、模块等。
