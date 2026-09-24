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


def _archive_failure(
    batch: ImportBatch,
    issues: list[dict[str, object]],
    *,
    recorded_at: object | None,
    source: str,
) -> None:
    if not issues:
        return
    metadata = dict(batch.metadata_json or {})
    history = list(metadata.get("attempt_history", []))
    entry = {
        "status": "failed",
        "recorded_at": recorded_at.isoformat() if hasattr(recorded_at, "isoformat") else None,
        "source": source,
        "issues": issues,
    }
    if not any(
        item.get("status") == entry["status"]
        and item.get("recorded_at") == entry["recorded_at"]
        and item.get("issues") == entry["issues"]
        for item in history
    ):
        history.append(entry)
    metadata["attempt_history"] = history
    batch.metadata_json = metadata


def repair_imported_batch_lifecycle(batch: ImportBatch) -> bool:
    """Move a stale current failure marker into audit history for an imported batch."""
    if batch.status != "imported":
        return False
    validation = dict(batch.validation_json or {})
    issues = list(validation.get("issues", []))
    history = list((batch.metadata_json or {}).get("attempt_history", []))
    if issues and not any(
        item.get("status") == "failed" and item.get("issues") == issues for item in history
    ):
        _archive_failure(
            batch,
            issues,
            recorded_at=None,
            source="legacy_current_validation",
        )
    desired = {"issues": [], "result": "imported"}
    changed = bool(issues) or validation != desired
    batch.validation_json = desired
    return changed


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
            if batch is None or batch.status not in {"valid", "failed"}:
                raise ValueError("Only a valid or failed batch can be imported")
            path = batch_file_path(batch, settings)
            if _sha256(path) != batch.source_hash:
                raise ValueError("The staged original no longer matches its recorded hash")
            row_count = callback(db, batch, path)
            if row_count is not None:
                batch.row_count = row_count
            batch.status = "imported"
            repair_imported_batch_lifecycle(batch)
            batch.imported_at = utc_now()
            batch.updated_at = utc_now()
    except Exception:
        with session_factory() as diagnostics_db, diagnostics_db.begin():
            batch = diagnostics_db.get(ImportBatch, batch_id)
            if batch is not None and batch.status in {"valid", "failed"}:
                failed_at = utc_now()
                issues = [
                    {
                        "code": "import_failed",
                        "message": "Der Import wurde vollständig zurückgerollt.",
                    }
                ]
                batch.status = "failed"
                batch.validation_json = {"issues": issues, "result": "failed"}
                _archive_failure(
                    batch,
                    issues,
                    recorded_at=failed_at,
                    source="import_attempt",
                )
                batch.updated_at = failed_at
        raise
