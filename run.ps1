$ErrorActionPreference = "Stop"

$python = "C:\Users\EY\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

& $python -m app.server
