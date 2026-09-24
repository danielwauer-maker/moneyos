from __future__ import annotations

import io
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.migrations import upgrade_database
from app.db.models import EconomicEvent, ImportBatch, RawImportRecord, SourceTransaction
from app.security.redaction import SensitiveDataFilter, redact_mapping
from app.services.backup import create_backup, restore_backup
from app.services.import_execution import run_atomic_import
from app.services.import_staging import stage_upload
from app.services.retention import apply_retention, plan_retention


@pytest.fixture
def private_store(tmp_path: Path) -> tuple[Settings, sessionmaker[Session], Path]:
    database_path = tmp_path / "database" / "moneyos.sqlite"
    database_path.parent.mkdir()
    database_url = f"sqlite:///{database_path.as_posix()}"
    settings = Settings(
        database_url=database_url,
        private_data_dir=tmp_path / "private",
        backup_dir=tmp_path / "backups",
        log_dir=tmp_path / "logs",
        max_import_file_size_bytes=64,
    )
    settings.ensure_local_directories()
    upgrade_database(database_url)
    engine = create_engine(database_url)
    yield settings, sessionmaker(bind=engine, expire_on_commit=False), database_path
    engine.dispose()


def _stage(
    factory: sessionmaker[Session],
    settings: Settings,
    content: bytes,
    filename: str = "export.csv",
    source_type: str = "sparda",
) -> ImportBatch:
    with factory() as db:
        return stage_upload(
            db,
            source_type=source_type,
            original_filename=filename,
            stream=io.BytesIO(content),
            settings=settings,
        ).batch


def _issue_codes(batch: ImportBatch) -> set[str]:
    return {issue["code"] for issue in batch.validation_json["issues"]}


def test_oversized_file_is_quarantined(private_store: tuple[Settings, sessionmaker, Path]) -> None:
    settings, factory, _ = private_store
    batch = _stage(factory, settings, b"x" * 65)
    assert batch.status == "quarantined"
    assert "file_too_large" in _issue_codes(batch)
    assert not (settings.staging_dir / batch.stored_filename).exists()
    assert (settings.quarantine_dir / batch.stored_filename).is_file()


def test_empty_file_is_quarantined(private_store: tuple[Settings, sessionmaker, Path]) -> None:
    settings, factory, _ = private_store
    batch = _stage(factory, settings, b"")
    assert batch.status == "quarantined"
    assert "empty_file" in _issue_codes(batch)


def test_unsupported_file_type_is_quarantined(
    private_store: tuple[Settings, sessionmaker, Path],
) -> None:
    settings, factory, _ = private_store
    batch = _stage(factory, settings, b"not executable", filename="statement.exe")
    assert batch.status == "quarantined"
    assert "unsupported_extension" in _issue_codes(batch)


def test_renamed_binary_file_is_quarantined(
    private_store: tuple[Settings, sessionmaker, Path],
) -> None:
    settings, factory, _ = private_store
    batch = _stage(factory, settings, b"%PDF-1.7 fake", filename="renamed.csv")
    assert batch.status == "quarantined"
    assert "content_mismatch" in _issue_codes(batch)


def test_suspicious_pdf_quarantine_flow(private_store: tuple[Settings, sessionmaker, Path]) -> None:
    settings, factory, _ = private_store
    batch = _stage(
        factory,
        settings,
        b"%PDF-1.7 /OpenAction /JavaScript",
        filename="statement.pdf",
        source_type="amex",
    )
    assert batch.status == "quarantined"
    assert "active_content" in _issue_codes(batch)
    assert batch.detected_mime == "application/pdf"


def test_staged_file_is_resumable_not_completed_duplicate(
    private_store: tuple[Settings, sessionmaker, Path],
) -> None:
    settings, factory, _ = private_store
    content = b"Datum;Betrag\n2026-01-01;12,34\n"
    first = _stage(factory, settings, content, source_type="bank")
    with factory() as db:
        result = stage_upload(
            db,
            source_type="bank",
            original_filename="renamed.csv",
            stream=io.BytesIO(content),
            settings=settings,
        )
        assert result.duplicate is False
        assert result.batch.id == first.id
        assert db.scalar(select(func.count()).select_from(ImportBatch)) == 1


def test_completed_file_is_duplicate_protected(
    private_store: tuple[Settings, sessionmaker, Path],
) -> None:
    settings, factory, _ = private_store
    content = b"Datum;Betrag\n2026-01-01;12,34\n"
    first = _stage(factory, settings, content, source_type="bank")
    with factory.begin() as db:
        db.get(ImportBatch, first.id).status = "imported"
    with factory() as db:
        result = stage_upload(
            db,
            source_type="bank",
            original_filename="renamed.csv",
            stream=io.BytesIO(content),
            settings=settings,
        )
        assert result.duplicate is True
        assert result.batch.id == first.id


def test_atomic_import_rolls_back_all_financial_records(
    private_store: tuple[Settings, sessionmaker, Path],
) -> None:
    settings, factory, _ = private_store
    batch = _stage(
        factory,
        settings,
        b"Datum;Betrag\n2026-01-01;12,34\n",
        source_type="bank",
    )
    assert batch.status == "valid"

    def failing_callback(db: Session, batch: ImportBatch, _path: Path) -> int:
        raw = RawImportRecord(
            import_batch_id=batch.id,
            source_row_key="row-1",
            raw_payload_json={"private": "value"},
            raw_text="private raw row",
            raw_hash="a" * 64,
        )
        db.add(raw)
        db.flush()
        db.add(
            SourceTransaction(
                import_batch_id=batch.id,
                raw_record_id=raw.id,
                source_system="sparda",
                source_transaction_id="transaction-1",
                booked_at=datetime(2026, 1, 1),
                description_raw="private description",
                amount=Decimal("12.34"),
                fingerprint="b" * 64,
            )
        )
        db.add(
            EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 1, 1),
                description="private description",
                amount=Decimal("12.34"),
            )
        )
        db.flush()
        raise RuntimeError("synthetic parser failure")

    with pytest.raises(RuntimeError):
        run_atomic_import(factory, batch.id, failing_callback, settings)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(RawImportRecord)) == 0
        assert db.scalar(select(func.count()).select_from(SourceTransaction)) == 0
        assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 0
        assert db.get(ImportBatch, batch.id).status == "failed"


def test_backup_restore_and_automatic_safety_backup(
    private_store: tuple[Settings, sessionmaker, Path],
) -> None:
    settings, factory, database_path = private_store
    with factory() as db:
        db.execute(text("CREATE TABLE restore_probe (value TEXT NOT NULL)"))
        db.execute(text("INSERT INTO restore_probe VALUES ('before')"))
        db.commit()
    archive = create_backup(settings.database_url, settings.backup_dir)
    assert archive.is_file()
    with factory() as db:
        db.execute(text("UPDATE restore_probe SET value='after'"))
        db.commit()
    factory.kw["bind"].dispose()

    result = restore_backup(
        settings.database_url,
        settings.backup_dir,
        archive,
        confirm=True,
    )
    assert result.safety_backup.is_file()
    assert "safety" in result.safety_backup.name
    with create_engine(f"sqlite:///{database_path.as_posix()}").connect() as connection:
        assert connection.scalar(text("SELECT value FROM restore_probe")) == "before"


def test_restore_requires_confirmation(private_store: tuple[Settings, sessionmaker, Path]) -> None:
    settings, _, _ = private_store
    archive = create_backup(settings.database_url, settings.backup_dir)
    with pytest.raises(ValueError, match="confirmation"):
        restore_backup(settings.database_url, settings.backup_dir, archive, confirm=False)


def test_sensitive_log_redaction(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("moneyos.test.redaction")
    filter_ = SensitiveDataFilter()
    caplog.handler.addFilter(filter_)
    caplog.set_level(logging.INFO, logger=logger.name)
    logger.info(
        "customer=%s card=%s iban=%s",
        "person@example.test",
        "9999 9999 9999 9999",
        "DE00 TEST 0000 0000 0000 00",
    )
    logger.info("description=%s", "Private purchase at Example Merchant")
    rendered = caplog.text
    assert "person@example.test" not in rendered
    assert "9999" not in rendered
    assert "DE00" not in rendered
    assert "Private purchase" not in rendered
    assert rendered.count("[REDACTED]") >= 3
    assert redact_mapping({"description_raw": "private shop", "safe": "ok"}) == {
        "description_raw": "[REDACTED]",
        "safe": "ok",
    }


def test_retention_never_deletes_only_backup(
    private_store: tuple[Settings, sessionmaker, Path],
) -> None:
    settings, _, _ = private_store
    settings.backup_retention_days = 0
    settings.minimum_backups_to_keep = 1
    only_backup = settings.backup_dir / "moneyos-old-regular.zip"
    only_backup.write_bytes(b"test")
    candidates = plan_retention(settings)
    assert only_backup not in {candidate.path for candidate in candidates}
    assert apply_retention(candidates, confirm=True) == 0
