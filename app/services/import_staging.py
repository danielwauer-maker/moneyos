from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import ImportBatch, utc_now

ALLOWED_EXTENSIONS: dict[str, frozenset[str]] = {
    "sparda": frozenset({".csv"}),
    "bank": frozenset({".csv"}),
    "paypal": frozenset({".csv"}),
    "amex": frozenset({".csv", ".pdf"}),
    "amazon": frozenset({".csv", ".json", ".zip"}),
}
SUSPICIOUS_ZIP_SUFFIXES = frozenset(
    {".exe", ".js", ".vbs", ".ps1", ".bat", ".cmd", ".scr", ".dll", ".msi", ".docm", ".xlsm"}
)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str


@dataclass(frozen=True)
class StageResult:
    batch: ImportBatch
    duplicate: bool = False


def _validation_payload(issues: list[ValidationIssue]) -> dict[str, object]:
    return {"issues": [{"code": issue.code, "message": issue.message} for issue in issues]}


def _inspect_file(
    path: Path, suffix: str, size: int, limit: int
) -> tuple[str, list[ValidationIssue]]:
    issues: list[ValidationIssue] = []
    if size == 0:
        return "application/x-empty", [ValidationIssue("empty_file", "Die Datei ist leer.")]
    if size > limit:
        return "application/octet-stream", [
            ValidationIssue("file_too_large", "Die Datei überschreitet die konfigurierte Größe.")
        ]

    with path.open("rb") as source:
        head = source.read(65536)
    known_binary_signatures = (b"%PDF-", b"PK\x03\x04", b"MZ", b"\x7fELF", b"\xd0\xcf\x11\xe0")
    if suffix in {".csv", ".json"} and head.startswith(known_binary_signatures):
        return "application/octet-stream", [
            ValidationIssue(
                "content_mismatch", "Der Dateiinhalt stimmt nicht mit der Endung überein."
            )
        ]
    if suffix == ".pdf":
        mime = "application/pdf"
        if not head.startswith(b"%PDF-"):
            issues.append(ValidationIssue("content_mismatch", "Der Inhalt ist keine PDF-Datei."))
        lowered = path.read_bytes().lower()
        if any(
            marker in lowered
            for marker in (b"/javascript", b"/js", b"/openaction", b"/launch", b"/embeddedfile")
        ):
            issues.append(
                ValidationIssue(
                    "active_content", "Aktive oder eingebettete PDF-Inhalte sind nicht erlaubt."
                )
            )
    elif suffix == ".zip":
        mime = "application/zip"
        if not head.startswith(b"PK"):
            issues.append(ValidationIssue("content_mismatch", "Der Inhalt ist kein ZIP-Archiv."))
        else:
            try:
                with zipfile.ZipFile(path) as archive:
                    for entry in archive.infolist():
                        entry_path = PurePosixPath(entry.filename.replace("\\", "/"))
                        if entry.flag_bits & 0x1:
                            issues.append(
                                ValidationIssue(
                                    "encrypted_archive",
                                    "Verschlüsselte ZIP-Einträge werden nicht verarbeitet.",
                                )
                            )
                        if entry_path.is_absolute() or ".." in entry_path.parts:
                            issues.append(
                                ValidationIssue(
                                    "unsafe_archive_path", "Das ZIP enthält einen unsicheren Pfad."
                                )
                            )
                        if entry_path.suffix.lower() in SUSPICIOUS_ZIP_SUFFIXES:
                            issues.append(
                                ValidationIssue(
                                    "active_content",
                                    "Das ZIP enthält ausführbaren oder aktiven Inhalt.",
                                )
                            )
            except (OSError, zipfile.BadZipFile):
                issues.append(ValidationIssue("invalid_archive", "Das ZIP-Archiv ist beschädigt."))
    elif suffix == ".json":
        mime = "application/json"
        try:
            json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            issues.append(
                ValidationIssue("invalid_json", "Der Inhalt ist kein gültiges UTF-8-JSON.")
            )
    else:
        mime = "text/csv"
        if b"\x00" in head:
            issues.append(
                ValidationIssue("binary_content", "Binärinhalt ist für CSV nicht erlaubt.")
            )
        else:
            try:
                text = head.decode("utf-8-sig")
            except UnicodeDecodeError:
                try:
                    text = head.decode("cp1252")
                except UnicodeDecodeError:
                    issues.append(
                        ValidationIssue("invalid_text", "Die CSV-Kodierung wird nicht unterstützt.")
                    )
                    text = ""
            control_count = len(re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text))
            if control_count:
                issues.append(
                    ValidationIssue(
                        "active_or_binary_content", "Die CSV enthält unerlaubte Steuerzeichen."
                    )
                )
    return mime, issues


def stage_upload(
    db: Session,
    *,
    source_type: str,
    original_filename: str,
    stream: BinaryIO,
    settings: Settings,
) -> StageResult:
    """Persist an immutable original and validate it without invoking a source parser."""
    settings.ensure_local_directories()
    source_type = source_type.lower().strip()
    suffix = Path(original_filename).suffix.lower()
    temp_path = settings.staging_dir / f".upload-{secrets.token_hex(16)}.tmp"
    digest = hashlib.sha256()
    size = 0
    batch: ImportBatch | None = None
    try:
        with temp_path.open("xb") as target:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                target.write(chunk)
        source_hash = digest.hexdigest()
        duplicate = db.scalar(select(ImportBatch).where(ImportBatch.source_hash == source_hash))
        if duplicate is not None:
            temp_path.unlink(missing_ok=True)
            return StageResult(batch=duplicate, duplicate=True)

        stored_filename = f"{source_hash}{suffix}"
        staged_path = settings.staging_dir / stored_filename
        os.replace(temp_path, staged_path)
        batch = ImportBatch(
            source_type=source_type,
            filename=Path(original_filename).name[:255] or "upload",
            source_hash=source_hash,
            status="uploaded",
            stored_filename=stored_filename,
            size_bytes=size,
            validation_json={},
            metadata_json={"original_preserved": True},
            updated_at=utc_now(),
        )
        db.add(batch)
        db.commit()
        db.refresh(batch)

        batch.status = "validating"
        batch.updated_at = utc_now()
        db.commit()

        issues: list[ValidationIssue] = []
        allowed = ALLOWED_EXTENSIONS.get(source_type)
        if allowed is None:
            issues.append(
                ValidationIssue("unsupported_source", "Der Quelltyp wird nicht unterstützt.")
            )
        elif suffix not in allowed:
            issues.append(
                ValidationIssue(
                    "unsupported_extension", "Der Dateityp ist für diese Quelle nicht erlaubt."
                )
            )
        detected_mime, content_issues = _inspect_file(
            staged_path, suffix, size, settings.max_import_file_size_bytes
        )
        issues.extend(content_issues)
        if source_type == "sparda" and not issues:
            from app.importers.sparda import SpardaFormatError, parse_sparda_csv

            try:
                parse_sparda_csv(staged_path)
            except SpardaFormatError as exc:
                issues.append(ValidationIssue(exc.code, exc.message))

        if issues:
            quarantine_path = settings.quarantine_dir / stored_filename
            os.replace(staged_path, quarantine_path)
            batch.status = "quarantined"
        else:
            batch.status = "valid"
        batch.detected_mime = detected_mime
        batch.validation_json = _validation_payload(issues)
        batch.updated_at = utc_now()
        db.commit()
        db.refresh(batch)
        return StageResult(batch=batch)
    except Exception:
        temp_path.unlink(missing_ok=True)
        db.rollback()
        if batch is not None and batch.id is not None:
            persisted = db.get(ImportBatch, batch.id)
            if persisted is not None and persisted.status in {"uploaded", "validating"}:
                persisted.status = "failed"
                persisted.validation_json = {
                    "issues": [
                        {
                            "code": "validation_failed",
                            "message": (
                                "Die sichere Dateiprüfung konnte nicht abgeschlossen werden."
                            ),
                        }
                    ]
                }
                persisted.updated_at = utc_now()
                db.commit()
        raise


def batch_file_path(batch: ImportBatch, settings: Settings) -> Path:
    if not batch.stored_filename:
        raise FileNotFoundError("No local file is retained for this import batch")
    roots = (
        (settings.quarantine_dir,)
        if batch.status == "quarantined"
        else (settings.staging_dir, settings.quarantine_dir)
    )
    for root in roots:
        candidate = (root / batch.stored_filename).resolve()
        if not candidate.is_relative_to(root.resolve()):
            raise ValueError("Stored import path escapes its private data directory")
        if candidate.exists():
            return candidate
    return (roots[0] / batch.stored_filename).resolve()


def delete_staged_file(db: Session, batch_id: int, settings: Settings, *, confirm: bool) -> None:
    if not confirm:
        raise ValueError("Explicit confirmation is required")
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise LookupError("Import batch not found")
    path = batch_file_path(batch, settings)
    path.unlink(missing_ok=True)
    batch.stored_filename = None
    batch.metadata_json = {**(batch.metadata_json or {}), "file_deleted_at": utc_now().isoformat()}
    batch.updated_at = utc_now()
    db.commit()
