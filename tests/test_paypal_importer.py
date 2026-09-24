from __future__ import annotations

import csv
import io
from collections.abc import Generator
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.migrations import upgrade_database
from app.db.models import (
    Account,
    EconomicEvent,
    EventSourceLink,
    ImportBatch,
    RawImportRecord,
    ReviewItem,
    SourceTransaction,
    SourceTransactionAccount,
)
from app.db.session import get_db
from app.importers import paypal
from app.importers.paypal import (
    PayPalFormatError,
    classify_paypal_row,
    dry_run_paypal_file,
    import_paypal_batch,
    match_sparda_funding,
    parse_paypal_csv,
    preview_paypal_file,
)
from app.main import app
from app.services.import_execution import repair_imported_batch_lifecycle
from app.services.import_staging import stage_upload
from app.services.transaction_review import build_transaction_review

D = Decimal

PAYPAL_COLUMNS = (
    "Datum",
    "Uhrzeit",
    "Zeitzone",
    "Name",
    "Typ",
    "Status",
    "Währung",
    "Brutto",
    "Gebühr",
    "Netto",
    "Absender E-Mail-Adresse",
    "Empfänger E-Mail-Adresse",
    "Transaktionscode",
    "Artikelbezeichnung",
    "Zugehöriger Transaktionscode",
    "Betreff",
    "Hinweis",
    "Auswirkung auf Guthaben",
)


@pytest.fixture
def paypal_store(tmp_path: Path) -> tuple[Settings, sessionmaker[Session]]:
    database_path = tmp_path / "database" / "moneyos.sqlite"
    database_path.parent.mkdir()
    database_url = f"sqlite:///{database_path.as_posix()}"
    settings = Settings(
        database_url=database_url,
        private_database_url=database_url,
        demo_mode=False,
        private_data_dir=tmp_path / "private",
        backup_dir=tmp_path / "backups",
        log_dir=tmp_path / "logs",
    )
    settings.ensure_local_directories()
    upgrade_database(database_url)
    engine = create_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        db.add_all(
            [
                Account(name="PayPal", account_type="payment", balance_confirmed=False),
                Account(name="Sparda Girokonto", account_type="checking"),
            ]
        )
    yield settings, factory
    engine.dispose()


def _row(**overrides: str) -> dict[str, str]:
    row = dict.fromkeys(PAYPAL_COLUMNS, "")
    row.update(
        {
            "Datum": "14.09.2026",
            "Uhrzeit": "10:30:00",
            "Zeitzone": "Europe/Berlin",
            "Name": "Fiktiver Händler",
            "Typ": "Allgemeine Zahlung",
            "Status": "Abgeschlossen",
            "Währung": "EUR",
            "Brutto": "-24,90",
            "Gebühr": "0,00",
            "Netto": "-24,90",
            "Transaktionscode": "SYN-MERCHANT-1",
            "Artikelbezeichnung": "Synthetischer Artikel",
            "Auswirkung auf Guthaben": "Soll",
        }
    )
    row.update(overrides)
    return row


def _csv_bytes(rows: list[dict[str, str]], columns: tuple[str, ...] = PAYPAL_COLUMNS) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(columns), lineterminator="\r\n")
    writer.writeheader()
    writer.writerows({column: row.get(column, "") for column in columns} for row in rows)
    return output.getvalue().encode("utf-8-sig")


def _stage(
    settings: Settings,
    factory: sessionmaker[Session],
    rows: list[dict[str, str]],
    *,
    filename: str = "paypal.csv",
) -> ImportBatch:
    with factory() as db:
        return stage_upload(
            db,
            source_type="paypal",
            original_filename=filename,
            stream=io.BytesIO(_csv_bytes(rows)),
            settings=settings,
        ).batch


def _import(
    settings: Settings, factory: sessionmaker[Session], rows: list[dict[str, str]]
) -> ImportBatch:
    batch = _stage(settings, factory, rows)
    assert batch.status == "valid"
    import_paypal_batch(factory, batch.id, settings)
    with factory() as db:
        return db.get(ImportBatch, batch.id)


def test_utf8_bom_decimal_flexible_columns_and_required_validation(tmp_path: Path) -> None:
    columns = tuple(reversed(PAYPAL_COLUMNS))
    path = tmp_path / "paypal.csv"
    path.write_bytes(_csv_bytes([_row(**{"Brutto": "-1.234,56", "Netto": "-1.234,56"})], columns))
    rows = parse_paypal_csv(path)
    assert rows[0].gross == D("-1234.56")
    assert rows[0].occurred_at == datetime(2026, 9, 14, 10, 30)
    assert rows[0].raw_values["Hinweis"] == ""

    missing = tuple(column for column in PAYPAL_COLUMNS if column != "Netto")
    path.write_bytes(_csv_bytes([_row()], missing))
    with pytest.raises(PayPalFormatError, match="Netto"):
        parse_paypal_csv(path)


@pytest.mark.parametrize(
    "transaction_type",
    [
        "Allgemeine Gutschrift auf Kreditkarte",
        "Allgemeine Abbuchung von Kreditkarte",
        "Bankgutschrift auf PayPal-Konto",
        "Allgemeine Autorisierung",
        "Einbehaltung für offene Autorisierung",
        "Rückbuchung allgemeiner Einbehaltung",
    ],
)
def test_technical_rows_never_become_expenses(tmp_path: Path, transaction_type: str) -> None:
    path = tmp_path / "paypal.csv"
    path.write_bytes(_csv_bytes([_row(**{"Typ": transaction_type})]))
    decision = classify_paypal_row(parse_paypal_csv(path)[0])
    assert decision.is_technical is True
    assert decision.event_type is None


def test_group_creates_one_expense_and_links_technical_rows(
    paypal_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = paypal_store
    merchant = _row()
    funding = _row(
        **{
            "Name": "PayPal",
            "Typ": "Bankgutschrift auf PayPal-Konto",
            "Brutto": "24,90",
            "Netto": "24,90",
            "Transaktionscode": "SYN-FUNDING-1",
            "Zugehöriger Transaktionscode": "SYN-MERCHANT-1",
        }
    )
    authorization = _row(
        **{
            "Typ": "Allgemeine Autorisierung",
            "Transaktionscode": "SYN-AUTH-1",
            "Zugehöriger Transaktionscode": "SYN-MERCHANT-1",
        }
    )
    batch = _import(settings, factory, [merchant, funding, authorization])
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(RawImportRecord)) == 3
        assert db.scalar(select(func.count()).select_from(SourceTransaction)) == 3
        assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 1
        event = db.scalar(select(EconomicEvent))
        assert event.event_type == "expense"
        assert event.amount == D("24.90")
        assert db.scalar(select(func.count()).select_from(SourceTransactionAccount)) == 3
        assert set(db.scalars(select(EventSourceLink.link_type))) == {
            "canonical_source",
            "funding_leg",
            "authorization",
        }
        assert batch.metadata_json["import_summary"]["merchant_payments"] == 1
        assert batch.metadata_json["import_summary"]["technical_funding_rows"] == 2
        paypal_account = db.scalar(select(Account).where(Account.name == "PayPal"))
        assert paypal_account.balance == D("0.00")
        assert paypal_account.balance_confirmed is False


def test_refund_is_linked_to_origin_and_not_income(
    paypal_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = paypal_store
    refund = _row(
        **{
            "Datum": "16.09.2026",
            "Typ": "Rückzahlung",
            "Brutto": "24,90",
            "Netto": "24,90",
            "Transaktionscode": "SYN-REFUND-1",
            "Zugehöriger Transaktionscode": "SYN-MERCHANT-1",
        }
    )
    _import(settings, factory, [_row(), refund])
    with factory() as db:
        assert list(db.scalars(select(EconomicEvent.event_type).order_by(EconomicEvent.id))) == [
            "expense",
            "refund",
        ]
        assert "refund_origin" in set(db.scalars(select(EventSourceLink.link_type)))


def test_conservative_sparda_matching_and_no_double_count(
    paypal_store: tuple[Settings, sessionmaker[Session]], tmp_path: Path
) -> None:
    settings, factory = paypal_store
    with factory.begin() as db:
        sparda_source = SourceTransaction(
            source_system="sparda",
            source_transaction_id="sparda-synthetic-paypal",
            booked_at=datetime(2026, 9, 14),
            value_at=datetime(2026, 9, 14),
            merchant_raw="PayPal Europe",
            description_raw="Synthetische PayPal-Abbuchung",
            amount=D("-24.90"),
            currency="EUR",
            status="booked",
            metadata_json={"sparda_semantic": "paypal_funding_leg"},
            fingerprint="a" * 64,
        )
        sparda_transfer = EconomicEvent(
            event_type="transfer",
            occurred_at=datetime(2026, 9, 14),
            description="Synthetischer PayPal-Transfer",
            amount=D("24.90"),
            currency="EUR",
            status="booked",
        )
        db.add_all([sparda_source, sparda_transfer])
        db.flush()
        db.add(
            EventSourceLink(
                economic_event_id=sparda_transfer.id,
                source_transaction_id=sparda_source.id,
                link_type="canonical_source",
                confidence=D("0.99"),
            )
        )
    merchant = _row()
    funding = _row(
        **{
            "Name": "PayPal",
            "Typ": "Bankgutschrift auf PayPal-Konto",
            "Brutto": "24,90",
            "Netto": "24,90",
            "Transaktionscode": "SYN-FUNDING-1",
            "Zugehöriger Transaktionscode": "SYN-MERCHANT-1",
        }
    )
    path = tmp_path / "paypal.csv"
    path.write_bytes(_csv_bytes([merchant, funding]))
    with factory() as db:
        rows = parse_paypal_csv(path)
        matches = match_sparda_funding(db, rows)
        funding_match = matches[
            next(row.row_number for row in rows if "Bankgutschrift" in row.transaction_type)
        ]
        assert funding_match.confidence == "high"

    _import(settings, factory, [merchant, funding])
    with factory() as db:
        assert list(db.scalars(select(EconomicEvent.event_type).order_by(EconomicEvent.id))) == [
            "transfer",
            "expense",
        ]
        assert db.scalar(select(func.count()).select_from(EventSourceLink)) == 4


def test_multi_event_group_uses_direct_references_and_reviews_ambiguity(
    paypal_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = paypal_store
    merchant_one = _row(
        **{
            "Transaktionscode": "SYN-M1",
            "Zugehöriger Transaktionscode": "SYN-F1",
            "Brutto": "-10,00",
            "Netto": "-10,00",
        }
    )
    funding_one = _row(
        **{
            "Name": "PayPal",
            "Typ": "Bankgutschrift auf PayPal-Konto",
            "Transaktionscode": "SYN-F1",
            "Zugehöriger Transaktionscode": "SYN-M1",
            "Brutto": "10,00",
            "Netto": "10,00",
        }
    )
    bridge = _row(
        **{
            "Name": "PayPal",
            "Typ": "Allgemeine Autorisierung",
            "Transaktionscode": "SYN-A1",
            "Zugehöriger Transaktionscode": "SYN-F1",
        }
    )
    funding_two = _row(
        **{
            "Name": "PayPal",
            "Typ": "Bankgutschrift auf PayPal-Konto",
            "Transaktionscode": "SYN-F2",
            "Zugehöriger Transaktionscode": "SYN-A1",
            "Brutto": "20,00",
            "Netto": "20,00",
        }
    )
    merchant_two = _row(
        **{
            "Transaktionscode": "SYN-M2",
            "Zugehöriger Transaktionscode": "SYN-F2",
            "Brutto": "-20,00",
            "Netto": "-20,00",
        }
    )
    _import(
        settings,
        factory,
        [merchant_one, funding_one, bridge, funding_two, merchant_two],
    )
    with factory() as db:
        raw_codes = {
            raw.id: raw.raw_payload_json["Transaktionscode"]
            for raw in db.scalars(select(RawImportRecord))
        }
        sources = {
            raw_codes[source.raw_record_id]: source
            for source in db.scalars(
                select(SourceTransaction).where(SourceTransaction.source_system == "paypal")
            )
        }
        canonical = {
            link.source_transaction_id: link.economic_event_id
            for link in db.scalars(
                select(EventSourceLink).where(EventSourceLink.link_type == "canonical_source")
            )
        }
        funding_links = {
            link.source_transaction_id: link.economic_event_id
            for link in db.scalars(
                select(EventSourceLink).where(EventSourceLink.link_type == "funding_leg")
            )
        }
        assert funding_links[sources["SYN-F1"].id] == canonical[sources["SYN-M1"].id]
        assert funding_links[sources["SYN-F2"].id] == canonical[sources["SYN-M2"].id]
        bridge_review = db.scalar(
            select(ReviewItem).where(ReviewItem.source_transaction_id == sources["SYN-A1"].id)
        )
        assert bridge_review.review_type == "paypal_group_link"


def test_unresolved_row_only_creates_review_and_technical_rows_stay_out_of_review_workspace(
    paypal_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = paypal_store
    unresolved = _row(
        **{
            "Name": "",
            "Typ": "Unbekannter Vorgang",
            "Status": "Offen",
            "Brutto": "0,00",
            "Netto": "0,00",
            "Transaktionscode": "SYN-UNKNOWN-1",
        }
    )
    technical = _row(
        **{
            "Name": "PayPal",
            "Typ": "Allgemeine Autorisierung",
            "Transaktionscode": "SYN-AUTH-ONLY",
        }
    )
    _import(settings, factory, [unresolved, technical])
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 0
        assert db.scalar(select(func.count()).select_from(ReviewItem)) == 1
        rows, _progress = build_transaction_review(db)
        assert len(rows) == 1
        assert rows[0].source.metadata_json["paypal_semantic"] == "unresolved"


def test_duplicate_file_and_row_detection_distinguishes_staged_and_completed(
    paypal_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = paypal_store
    row = _row()
    first = _stage(settings, factory, [row])
    with factory() as db:
        duplicate = stage_upload(
            db,
            source_type="paypal",
            original_filename="renamed.csv",
            stream=io.BytesIO(_csv_bytes([row])),
            settings=settings,
        )
        assert duplicate.duplicate is False
        assert duplicate.batch.id == first.id
    import_paypal_batch(factory, first.id, settings)
    with factory() as db:
        completed = stage_upload(
            db,
            source_type="paypal",
            original_filename="completed-copy.csv",
            stream=io.BytesIO(_csv_bytes([row])),
            settings=settings,
        )
        assert completed.duplicate is True
    second = _import(
        settings,
        factory,
        [row, _row(**{"Transaktionscode": "SYN-MERCHANT-2", "Brutto": "-5,00", "Netto": "-5,00"})],
    )
    assert second.metadata_json["import_summary"]["duplicate_rows"] == 1


def test_full_import_rolls_back_on_failure(
    paypal_store: tuple[Settings, sessionmaker[Session]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, factory = paypal_store
    batch = _stage(settings, factory, [_row()])
    original = paypal._import_rows

    def fail_after_work(db: Session, current: ImportBatch, path: Path) -> int:
        original(db, current, path)
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(paypal, "_import_rows", fail_after_work)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        import_paypal_batch(factory, batch.id, settings)
    with factory() as db:
        assert db.get(ImportBatch, batch.id).status == "failed"
        assert db.scalar(select(func.count()).select_from(RawImportRecord)) == 0
        assert db.scalar(select(func.count()).select_from(SourceTransaction)) == 0
        assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 0


def test_failed_batch_can_retry_without_partial_or_duplicate_records(
    paypal_store: tuple[Settings, sessionmaker[Session]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, factory = paypal_store
    batch = _stage(settings, factory, [_row()])
    original = paypal._import_rows

    def fail_once(db: Session, current: ImportBatch, path: Path) -> int:
        original(db, current, path)
        raise RuntimeError("synthetic first attempt failure")

    monkeypatch.setattr(paypal, "_import_rows", fail_once)
    with pytest.raises(RuntimeError):
        import_paypal_batch(factory, batch.id, settings)
    with factory() as db:
        failed_batch = db.get(ImportBatch, batch.id)
        assert failed_batch.validation_json["result"] == "failed"
        assert failed_batch.validation_json["issues"][0]["code"] == "import_failed"
        assert len(failed_batch.metadata_json["attempt_history"]) == 1
        resumed = stage_upload(
            db,
            source_type="paypal",
            original_filename="retry.csv",
            stream=io.BytesIO(_csv_bytes([_row()])),
            settings=settings,
        )
        assert resumed.duplicate is False
        assert resumed.batch.id == batch.id
        assert resumed.batch.status == "failed"
    monkeypatch.setattr(paypal, "_import_rows", original)
    import_paypal_batch(factory, batch.id, settings)

    with factory() as db:
        imported_batch = db.get(ImportBatch, batch.id)
        assert imported_batch.status == "imported"
        assert imported_batch.validation_json == {"issues": [], "result": "imported"}
        assert len(imported_batch.metadata_json["attempt_history"]) == 1
        assert imported_batch.metadata_json["attempt_history"][0]["status"] == "failed"
        assert db.scalar(select(func.count()).select_from(RawImportRecord)) == 1
        assert db.scalar(select(func.count()).select_from(SourceTransaction)) == 1
        assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 1

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    monkeypatch.setattr("app.web.routes.get_settings", lambda: settings)
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            page = client.get(f"/import?batch_id={batch.id}")
            assert page.status_code == 200
            assert "Import erfolgreich" in page.text
            assert "Historische Fehler: 1" in page.text
            assert "import_failed" not in page.text
    finally:
        app.dependency_overrides.clear()
    with pytest.raises(ValueError, match="valid or failed"):
        import_paypal_batch(factory, batch.id, settings)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(RawImportRecord)) == 1
        assert db.scalar(select(func.count()).select_from(SourceTransaction)) == 1
        assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 1


def test_repeated_paypal_transaction_code_uses_unique_stable_source_ids(
    paypal_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = paypal_store
    rows = [
        _row(**{"Transaktionscode": "SYN-REPEATED", "Brutto": "-10,00", "Netto": "-10,00"}),
        _row(
            **{
                "Datum": "15.09.2026",
                "Transaktionscode": "SYN-REPEATED",
                "Brutto": "-20,00",
                "Netto": "-20,00",
            }
        ),
    ]
    _import(settings, factory, rows)
    with factory() as db:
        source_ids = list(
            db.scalars(
                select(SourceTransaction.source_transaction_id).where(
                    SourceTransaction.source_system == "paypal"
                )
            )
        )
        assert len(source_ids) == len(set(source_ids)) == 2


def test_legacy_stale_failure_is_archived_without_changing_import_counts(
    paypal_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = paypal_store
    batch = _import(settings, factory, [_row()])
    with factory.begin() as db:
        current = db.get(ImportBatch, batch.id)
        current.validation_json = {
            "issues": [
                {
                    "code": "import_failed",
                    "message": "Der Import wurde vollständig zurückgerollt.",
                }
            ]
        }
        current.metadata_json = {
            key: value for key, value in current.metadata_json.items() if key != "attempt_history"
        }
        before = (
            db.scalar(select(func.count()).select_from(RawImportRecord)),
            db.scalar(select(func.count()).select_from(SourceTransaction)),
            db.scalar(select(func.count()).select_from(EconomicEvent)),
            db.scalar(select(func.count()).select_from(ReviewItem)),
        )
        assert repair_imported_batch_lifecycle(current) is True
        after = (
            db.scalar(select(func.count()).select_from(RawImportRecord)),
            db.scalar(select(func.count()).select_from(SourceTransaction)),
            db.scalar(select(func.count()).select_from(EconomicEvent)),
            db.scalar(select(func.count()).select_from(ReviewItem)),
        )
        assert current.validation_json == {"issues": [], "result": "imported"}
        assert current.metadata_json["attempt_history"][0]["status"] == "failed"
        assert current.metadata_json["attempt_history"][0]["source"] == (
            "legacy_current_validation"
        )
        assert before == after == (1, 1, 1, 0)


def test_paypal_stage_preview_execute_and_completed_reexecute_blocked(
    paypal_store: tuple[Settings, sessionmaker[Session]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, factory = paypal_store
    content = _csv_bytes([_row()])

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    monkeypatch.setattr("app.web.routes.get_settings", lambda: settings)
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            staged = client.post(
                "/import/stage",
                data={"source_type": "paypal"},
                files={"upload": ("paypal.csv", content, "text/csv")},
                follow_redirects=False,
            )
            assert staged.status_code == 303
            assert staged.headers["location"].endswith("/preview")
            preview = client.get(staged.headers["location"])
            assert preview.status_code == 200
            assert "PayPal-Import prüfen" in preview.text
            batch_id = int(staged.headers["location"].split("/")[-2])
            executed = client.post(f"/import/{batch_id}/execute", follow_redirects=False)
            assert executed.status_code == 303
            repeated = client.post(f"/import/{batch_id}/execute", follow_redirects=False)
            assert repeated.status_code == 404
    finally:
        app.dependency_overrides.clear()

    with factory() as db:
        assert db.get(ImportBatch, batch_id).status == "imported"
        assert db.scalar(select(func.count()).select_from(SourceTransaction)) == 1
        assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 1


def test_preview_redacts_email_and_exposes_mutually_exclusive_counts(
    paypal_store: tuple[Settings, sessionmaker[Session]], tmp_path: Path
) -> None:
    _settings, factory = paypal_store
    path = tmp_path / "paypal.csv"
    path.write_bytes(_csv_bytes([_row(**{"Name": "private.person@example.test"})]))
    with factory() as db:
        rows, summary, _matches = preview_paypal_file(path, db)
    assert rows[0].merchant == "[REDACTED]"
    assert summary.source_rows_parsed == (
        summary.merchant_payments
        + summary.refunds
        + summary.technical_funding_rows
        + summary.unresolved_rows
    )


def test_dry_run_is_read_only(
    paypal_store: tuple[Settings, sessionmaker[Session]], tmp_path: Path
) -> None:
    _settings, factory = paypal_store
    path = tmp_path / "paypal.csv"
    path.write_bytes(_csv_bytes([_row()]))
    with factory() as db:
        before = (
            db.scalar(select(func.count()).select_from(ImportBatch)),
            db.scalar(select(func.count()).select_from(RawImportRecord)),
            db.scalar(select(func.count()).select_from(SourceTransaction)),
        )
        report = dry_run_paypal_file(path, db)
        after = (
            db.scalar(select(func.count()).select_from(ImportBatch)),
            db.scalar(select(func.count()).select_from(RawImportRecord)),
            db.scalar(select(func.count()).select_from(SourceTransaction)),
        )
    assert report.source_rows == 1
    assert report.duplicate_file is False
    assert report.existing_batch_status is None
    assert before == after == (0, 0, 0)


def test_dry_run_duplicate_flag_only_marks_completed_import(
    paypal_store: tuple[Settings, sessionmaker[Session]], tmp_path: Path
) -> None:
    settings, factory = paypal_store
    content = _csv_bytes([_row()])
    path = tmp_path / "paypal.csv"
    path.write_bytes(content)
    with factory() as db:
        batch = stage_upload(
            db,
            source_type="paypal",
            original_filename="paypal.csv",
            stream=io.BytesIO(content),
            settings=settings,
        ).batch
        staged = dry_run_paypal_file(path, db)
        assert staged.duplicate_file is False
        assert staged.existing_batch_status == "valid"
        batch.status = "failed"
        db.commit()
        failed = dry_run_paypal_file(path, db)
        assert failed.duplicate_file is False
        assert failed.existing_batch_status == "failed"
        batch.status = "imported"
        db.commit()
        completed = dry_run_paypal_file(path, db)
        assert completed.duplicate_file is True
        assert completed.existing_batch_status == "imported"
