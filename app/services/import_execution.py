from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import ImportBatch, utc_now
from app.services.import_staging import batch_file_path

ImportCallback = Callable[[Session, ImportBatch, Path], int | None]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def run_atomic_import(
    session_factory: sessionmaker[Session],
    batch_id: int,
    callback: ImportCallback,
    settings: Settings,
) -> None:
    """Run a future parser callback as one database transaction."""
    try:
        with session_factory() as db, db.begin():
            batch = db.get(ImportBatch, batch_id)
            if batch is None or batch.status != "valid":
                raise ValueError("Only a valid batch can be imported")
            path = batch_file_path(batch, settings)
            if _sha256(path) != batch.source_hash:
                raise ValueError("The staged original no longer matches its recorded hash")
            row_count = callback(db, batch, path)
            if row_count is not None:
                batch.row_count = row_count
            batch.status = "imported"
            batch.updated_at = utc_now()
    except Exception:
        with session_factory() as diagnostics_db, diagnostics_db.begin():
            batch = diagnostics_db.get(ImportBatch, batch_id)
            if batch is not None and batch.status == "valid":
                batch.status = "failed"
                batch.validation_json = {
                    "issues": [
                        {
                            "code": "import_failed",
                            "message": "Der Import wurde vollständig zurückgerollt.",
                        }
                    ]
                }
                batch.updated_at = utc_now()
        raise
