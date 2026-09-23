$ErrorActionPreference = "Stop"
if (-not (Test-Path ".venv")) { py -3.12 -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
& .\.venv\Scripts\python.exe -m alembic upgrade head
& .\.venv\Scripts\python.exe -m app.seed.demo
Write-Host "MoneyOS ist bereit. Starte mit: .\.venv\Scripts\python.exe -m uvicorn app.main:app --reload"
