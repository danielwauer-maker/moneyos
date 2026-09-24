import csv
import hashlib
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
    BalanceConfirmation,
    EconomicEvent,
    EventSourceLink,
    ImportBatch,
    RawImportRecord,
    ReviewItem,
    SourceTransaction,
)
from app.db.session import get_db
from app.importers import amex
from app.importers.amex import (
    AmexFormatError,
    classify_amex_row,
    dry_run_amex_file,
    import_amex_batch,
    match_sparda_settlements,
    parse_amex_csv,
    preview_amex_file,
)
from app.main import app
from app.services.import_staging import stage_upload
from app.services.transaction_review import build_transaction_review

D = Decimal

AMEX_COLUMNS = (
    "Datum",
    "Transaktionsdatum",
    "Beschreibung",
    "Betrag",
    "Währung",
    "Fremdwährungsbetrag",
    "Fremdwährung",
    "Wechselkurs",
    "Land",
    "Ort",
    "Referenz",
    "Zugehörige Referenz",
    "Kartenreferenz",
    "Erweiterte Details",
    "Typ",
)


@pytest.fixture
def amex_store(tmp_path: Path) -> tuple[Settings, sessionmaker[Session]]:
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
        amex_account = Account(
            name="American Express",
            account_type="credit_card",
            is_liability=True,
            balance=D("-500.00"),
            balance_confirmed=True,
        )
        db.add_all([amex_account, Account(name="Sparda Girokonto", account_type="checking")])
        db.flush()
        db.add(
            BalanceConfirmation(
                account_id=amex_account.id,
                confirmed_at=datetime(2026, 9, 1, 12),
                balance=D("-500.00"),
                currency="EUR",
                source_type="card_statement",
                confidence=D("1"),
                status="confirmed",
            )
        )
    yield settings, factory
    engine.dispose()


def _row(**overrides: str) -> dict[str, str]:
    row = dict.fromkeys(AMEX_COLUMNS, "")
    row.update(
        {
            "Datum": "14.09.2026",
            "Transaktionsdatum": "13.09.2026",
            "Beschreibung": "Fiktiver Händler",
            "Betrag": "24,90",
            "Währung": "EUR",
            "Referenz": "SYN-AMEX-1",
            "Kartenreferenz": "XXXX-1005",
            "Typ": "Kauf",
        }
    )
    row.update(overrides)
    return row


def _csv_bytes(
    rows: list[dict[str, str]],
    columns: tuple[str, ...] = AMEX_COLUMNS,
    *,
    delimiter: str = ";",
) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=list(columns), delimiter=delimiter, lineterminator="\r\n"
    )
    writer.writeheader()
    writer.writerows({column: row.get(column, "") for column in columns} for row in rows)
    return output.getvalue().encode("utf-8-sig")


def _stage(
    settings: Settings,
    factory: sessionmaker[Session],
    rows: list[dict[str, str]],
) -> ImportBatch:
    with factory() as db:
        return stage_upload(
            db,
            source_type="amex",
            original_filename="amex.csv",
            stream=io.BytesIO(_csv_bytes(rows)),
            settings=settings,
        ).batch


def _sparda_settlement(
    db: Session, *, amount: str = "-100.00", day: int = 14
) -> tuple[SourceTransaction, EconomicEvent]:
    source = SourceTransaction(
        source_system="sparda",
        source_transaction_id=f"sparda-amex-{day}",
        booked_at=datetime(2026, 9, day),
        merchant_raw="AMERICAN EXPRESS",
        description_raw="AMERICAN EXPRESS ABRECHNUNG",
        amount=D(amount),
        currency="EUR",
        metadata_json={"sparda_semantic": "credit_card_settlement"},
        fingerprint=hashlib.sha256(f"sparda-{day}-{amount}".encode()).hexdigest(),
    )
    event = EconomicEvent(
        event_type="transfer",
        occurred_at=source.booked_at,
        description="Amex-Abrechnung",
        amount=abs(D(amount)),
        currency="EUR",
    )
    db.add_all([source, event])
    db.flush()
    db.add(
        EventSourceLink(
            economic_event_id=event.id,
            source_transaction_id=source.id,
            link_type="canonical_source",
            confidence=D("1"),
        )
    )
    db.flush()
    return source, event


def test_bom_flexible_delimiter_columns_decimals_dates_and_foreign_currency(
    tmp_path: Path,
) -> None:
    columns = tuple(reversed(AMEX_COLUMNS))
    row = _row(
        **{
            "Betrag": "1.234,56",
            "Fremdwährungsbetrag": "1,350.25",
            "Fremdwährung": "USD",
            "Wechselkurs": "0,91432",
            "Land": "US",
        }
    )
    path = tmp_path / "amex.csv"
    path.write_bytes(_csv_bytes([row], columns, delimiter=","))
    parsed = parse_amex_csv(path)[0]
    assert parsed.amount == D("1234.56")
    assert parsed.original_amount == D("1350.25")
    assert parsed.original_currency == "USD"
    assert parsed.exchange_rate == D("0.91432")
    assert parsed.booked_at == datetime(2026, 9, 14)
    assert parsed.transaction_at == datetime(2026, 9, 13)

    missing = tuple(column for column in AMEX_COLUMNS if column != "Betrag")
    path.write_bytes(_csv_bytes([row], missing))
    with pytest.raises(AmexFormatError, match="amount"):
        parse_amex_csv(path)


def test_structural_failure_quarantines_whole_amex_file(
    amex_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = amex_store
    columns = tuple(column for column in AMEX_COLUMNS if column != "Betrag")
    with factory() as db:
        result = stage_upload(
            db,
            source_type="amex",
            original_filename="invalid.csv",
            stream=io.BytesIO(_csv_bytes([_row()], columns)),
            settings=settings,
        )
        assert result.batch.status == "quarantined"
        assert result.batch.validation_json["issues"][0]["code"] == "missing_required_columns"
        assert db.scalar(select(func.count()).select_from(RawImportRecord)) == 0
        assert db.scalar(select(func.count()).select_from(SourceTransaction)) == 0


def test_semantics_are_mutually_exclusive_and_conservative(tmp_path: Path) -> None:
    rows = [
        _row(),
        _row(**{"Beschreibung": "Rückerstattung Händler", "Betrag": "-24,90"}),
        _row(**{"Beschreibung": "ZAHLUNG ERHALTEN - DANKE", "Betrag": "-100,00"}),
        _row(**{"Beschreibung": "Jahresgebühr", "Betrag": "55,00"}),
        _row(**{"Beschreibung": "Manuelle Anpassung", "Betrag": "12,00"}),
    ]
    path = tmp_path / "amex.csv"
    path.write_bytes(_csv_bytes(rows))
    decisions = [classify_amex_row(row) for row in parse_amex_csv(path)]
    assert [decision.semantic for decision in decisions] == [
        "merchant_purchase",
        "refund",
        "statement_payment",
        "fee_interest",
        "unresolved",
    ]
    assert [decision.event_type for decision in decisions] == [
        "expense",
        "refund",
        None,
        "expense",
        None,
    ]


def test_real_statement_payment_precedes_negative_refund_fallback(tmp_path: Path) -> None:
    path = tmp_path / "amex.csv"
    path.write_bytes(
        _csv_bytes(
            [
                _row(
                    **{
                        "Beschreibung": "ZAHLUNG/ÜBERWEISUNG ERHALTEN BESTEN DANK",
                        "Betrag": "-1.146,24",
                    }
                ),
                _row(
                    **{
                        "Beschreibung": "Beispielhändler",
                        "Betrag": "-24,90",
                    }
                ),
            ]
        )
    )

    settlement, refund = [classify_amex_row(row) for row in parse_amex_csv(path)]

    assert settlement.semantic == "statement_payment"
    assert settlement.event_type is None
    assert settlement.needs_review is False
    assert refund.semantic == "refund"
    assert refund.event_type == "refund"


def test_real_extrapunkte_participation_charge_is_explicit_fee(tmp_path: Path) -> None:
    path = tmp_path / "amex.csv"
    path.write_bytes(
        _csv_bytes(
            [
                _row(
                    **{
                        "Beschreibung": "ExtraPunkte Teilnahmegebuehr",
                        "Betrag": "30,00",
                    }
                )
            ]
        )
    )

    decision = classify_amex_row(parse_amex_csv(path)[0])

    assert decision.semantic == "fee_interest"
    assert decision.event_type == "expense"
    assert decision.needs_review is False


def test_duplicate_reference_ids_do_not_collapse_distinct_rows(tmp_path: Path) -> None:
    path = tmp_path / "amex.csv"
    path.write_bytes(
        _csv_bytes(
            [
                _row(**{"Referenz": "SYN-REPEATED", "Betrag": "10,00"}),
                _row(
                    **{
                        "Referenz": "SYN-REPEATED",
                        "Datum": "15.09.2026",
                        "Betrag": "20,00",
                    }
                ),
            ]
        )
    )
    rows = parse_amex_csv(path)
    assert len({row.fingerprint for row in rows}) == 2


def test_fingerprint_is_stable_across_column_order(tmp_path: Path) -> None:
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    first.write_bytes(_csv_bytes([_row()]))
    second.write_bytes(_csv_bytes([_row()], tuple(reversed(AMEX_COLUMNS))))
    assert parse_amex_csv(first)[0].fingerprint == parse_amex_csv(second)[0].fingerprint


def test_high_and_medium_sparda_settlement_matching(
    amex_store: tuple[Settings, sessionmaker[Session]], tmp_path: Path
) -> None:
    _settings, factory = amex_store
    path = tmp_path / "amex.csv"
    path.write_bytes(
        _csv_bytes(
            [
                _row(**{"Beschreibung": "ZAHLUNG ERHALTEN - DANKE", "Betrag": "-100,00"}),
                _row(
                    **{
                        "Datum": "20.09.2026",
                        "Beschreibung": "ZAHLUNG ERHALTEN - DANKE",
                        "Betrag": "-200,00",
                    }
                ),
            ]
        )
    )
    with factory.begin() as db:
        _sparda_settlement(db, amount="-100.00", day=14)
        _sparda_settlement(db, amount="-200.00", day=16)
    with factory() as db:
        matches = match_sparda_settlements(db, parse_amex_csv(path))
    assert matches[2].confidence == "high"
    assert matches[3].confidence == "medium"


def test_import_creates_events_links_reviews_and_preserves_confirmed_balance(
    amex_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = amex_store
    rows = [
        _row(**{"Beschreibung": "Fiktiver Shop", "Betrag": "40,00", "Referenz": "PUR-1"}),
        _row(
            **{
                "Datum": "16.09.2026",
                "Beschreibung": "Fiktiver Shop Refund",
                "Betrag": "-40,00",
                "Referenz": "REF-1",
                "Zugehörige Referenz": "PUR-1",
            }
        ),
        _row(**{"Beschreibung": "ZAHLUNG ERHALTEN - DANKE", "Betrag": "-100,00"}),
        _row(**{"Beschreibung": "Jahresgebühr", "Betrag": "55,00"}),
        _row(**{"Beschreibung": "Manuelle Anpassung", "Betrag": "12,00"}),
    ]
    with factory.begin() as db:
        _sparda_settlement(db, amount="-100.00", day=14)
        before_confirmation = db.scalar(select(BalanceConfirmation)).balance
    batch = _stage(settings, factory, rows)
    import_amex_batch(factory, batch.id, settings)

    with factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(RawImportRecord)
                .where(RawImportRecord.import_batch_id == batch.id)
            )
            == 5
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(SourceTransaction)
                .where(SourceTransaction.source_system == "amex")
            )
            == 5
        )
        amex_events = list(
            db.scalars(
                select(EconomicEvent)
                .join(EventSourceLink)
                .join(SourceTransaction)
                .where(
                    SourceTransaction.source_system == "amex",
                    EventSourceLink.link_type == "canonical_source",
                )
            )
        )
        assert [event.event_type for event in amex_events].count("expense") == 2
        assert [event.event_type for event in amex_events].count("refund") == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(EventSourceLink)
                .where(EventSourceLink.link_type == "settlement_leg")
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(EventSourceLink)
                .where(EventSourceLink.link_type == "refund_origin")
            )
            == 1
        )
        assert db.scalar(select(func.count()).select_from(ReviewItem)) == 1
        assert db.scalar(select(BalanceConfirmation)).balance == before_confirmation
        account = db.scalar(select(Account).where(Account.name == "American Express"))
        assert account.balance == D("-500.00")
        chronological, _ = build_transaction_review(db)
        assert sum(row.source.source_system == "amex" for row in chronological) == 4
        assert not any(
            row.source.source_system == "amex"
            and (row.source.metadata_json or {}).get("amex_semantic") == "statement_payment"
            for row in chronological
        )


def test_unmatched_settlement_and_ambiguous_refund_are_reviewable(
    amex_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = amex_store
    batch = _stage(
        settings,
        factory,
        [
            _row(**{"Beschreibung": "ZAHLUNG ERHALTEN - DANKE", "Betrag": "-900,00"}),
            _row(**{"Beschreibung": "Unbekannter Refund", "Betrag": "-12,00"}),
        ],
    )
    import_amex_batch(factory, batch.id, settings)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 1
        assert db.scalar(select(func.count()).select_from(ReviewItem)) == 2


def test_full_card_number_is_not_persisted(
    amex_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = amex_store
    full_card = "3782 822463 10005"
    batch = _stage(
        settings,
        factory,
        [_row(**{"Kartenreferenz": full_card, "Erweiterte Details": f"Karte {full_card}"})],
    )
    import_amex_batch(factory, batch.id, settings)
    with factory() as db:
        raw = db.scalar(select(RawImportRecord))
        source = db.scalar(
            select(SourceTransaction).where(SourceTransaction.source_system == "amex")
        )
        assert full_card not in raw.raw_text
        assert full_card not in str(raw.raw_payload_json)
        assert full_card not in source.description_raw
        assert full_card not in str(source.metadata_json)


def test_atomic_failure_retry_and_completed_reexecute_protection(
    amex_store: tuple[Settings, sessionmaker[Session]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, factory = amex_store
    batch = _stage(settings, factory, [_row()])
    original = amex._import_rows

    def fail_after_work(db: Session, current: ImportBatch, path: Path) -> int:
        original(db, current, path)
        raise RuntimeError("synthetic Amex failure")

    monkeypatch.setattr(amex, "_import_rows", fail_after_work)
    with pytest.raises(RuntimeError, match="synthetic Amex failure"):
        import_amex_batch(factory, batch.id, settings)
    with factory() as db:
        assert db.get(ImportBatch, batch.id).status == "failed"
        assert db.scalar(select(func.count()).select_from(RawImportRecord)) == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(SourceTransaction)
                .where(SourceTransaction.source_system == "amex")
            )
            == 0
        )
    monkeypatch.setattr(amex, "_import_rows", original)
    import_amex_batch(factory, batch.id, settings)
    with pytest.raises(ValueError, match="valid or failed"):
        import_amex_batch(factory, batch.id, settings)
    with factory() as db:
        assert db.get(ImportBatch, batch.id).status == "imported"
        assert db.scalar(select(func.count()).select_from(RawImportRecord)) == 1


def test_duplicate_file_overlap_and_dry_run_are_idempotent_and_read_only(
    amex_store: tuple[Settings, sessionmaker[Session]], tmp_path: Path
) -> None:
    settings, factory = amex_store
    content = _csv_bytes([_row()])
    path = tmp_path / "amex.csv"
    path.write_bytes(content)
    with factory() as db:
        before = (
            db.scalar(select(func.count()).select_from(ImportBatch)),
            db.scalar(select(func.count()).select_from(SourceTransaction)),
        )
        report = dry_run_amex_file(path, db)
        after = (
            db.scalar(select(func.count()).select_from(ImportBatch)),
            db.scalar(select(func.count()).select_from(SourceTransaction)),
        )
    assert report.duplicate_file is False
    assert before == after

    batch = _stage(settings, factory, [_row()])
    import_amex_batch(factory, batch.id, settings)
    with factory() as db:
        duplicate = stage_upload(
            db,
            source_type="amex",
            original_filename="copy.csv",
            stream=io.BytesIO(content),
            settings=settings,
        )
        assert duplicate.duplicate is True
        completed_report = dry_run_amex_file(path, db)
        assert completed_report.duplicate_file is True
        assert completed_report.duplicate_rows == 1


def test_stage_preview_execute_flow_and_redacted_preview(
    amex_store: tuple[Settings, sessionmaker[Session]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, factory = amex_store
    content = _csv_bytes([_row(**{"Beschreibung": "shop@example.test"})])

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    monkeypatch.setattr("app.web.routes.get_settings", lambda: settings)
    monkeypatch.setattr("app.web.routes.create_backup", lambda *_args, **_kwargs: None)
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            staged = client.post(
                "/import/stage",
                data={"source_type": "amex"},
                files={"upload": ("amex.csv", content, "text/csv")},
                follow_redirects=False,
            )
            assert staged.status_code == 303
            preview = client.get(staged.headers["location"])
            assert preview.status_code == 200
            assert "American-Express-Import prüfen" in preview.text
            assert "shop@example.test" not in preview.text
            batch_id = int(staged.headers["location"].split("/")[-2])
            executed = client.post(f"/import/{batch_id}/execute", follow_redirects=False)
            assert executed.status_code == 303
            repeated = client.post(f"/import/{batch_id}/execute", follow_redirects=False)
            assert repeated.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_preview_counts_are_mutually_exclusive(
    amex_store: tuple[Settings, sessionmaker[Session]], tmp_path: Path
) -> None:
    _settings, factory = amex_store
    path = tmp_path / "amex.csv"
    path.write_bytes(
        _csv_bytes(
            [
                _row(),
                _row(**{"Beschreibung": "Refund", "Betrag": "-10,00"}),
                _row(**{"Beschreibung": "ZAHLUNG ERHALTEN - DANKE", "Betrag": "-20,00"}),
                _row(**{"Beschreibung": "Jahresgebühr"}),
                _row(**{"Beschreibung": "Adjustment"}),
            ]
        )
    )
    with factory() as db:
        _preview, summary, _matches = preview_amex_file(path, db)
    assert summary.source_rows_parsed == (
        summary.merchant_purchases
        + summary.refunds
        + summary.settlement_rows
        + summary.fees_interest_other
        + summary.unresolved_rows
    )
    assert summary.repeated_reference_ids == 1
