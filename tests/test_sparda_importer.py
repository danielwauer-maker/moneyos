from __future__ import annotations

import csv
import io
import logging
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
    Category,
    EconomicEvent,
    EventSourceLink,
    ImportBatch,
    RawImportRecord,
    ReviewItem,
    SourceTransaction,
)
from app.db.session import get_db
from app.domain.sparda import classify_sparda_transaction, suggest_sparda_category
from app.importers.sparda import (
    KNOWN_COLUMNS,
    SpardaFormatError,
    import_sparda_batch,
    parse_german_date,
    parse_german_decimal,
    parse_sparda_csv,
)
from app.main import app
from app.services.import_staging import stage_upload

D = Decimal


@pytest.fixture
def sparda_store(tmp_path: Path) -> tuple[Settings, sessionmaker[Session]]:
    database_path = tmp_path / "database" / "moneyos.sqlite"
    database_path.parent.mkdir()
    database_url = f"sqlite:///{database_path.as_posix()}"
    settings = Settings(
        database_url=database_url,
        private_data_dir=tmp_path / "private",
        backup_dir=tmp_path / "backups",
        log_dir=tmp_path / "logs",
        max_import_file_size_bytes=1024 * 1024,
    )
    settings.ensure_local_directories()
    upgrade_database(database_url)
    engine = create_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        db.add(Account(name="Sparda Giro", account_type="checking", balance=D("0")))
        category_paths = {
            "Lebensmittel": ["Supermarkt", "Bäckerei", "Metzgerei"],
            "Auto & Mobilität": ["Tanken", "Parken", "KFZ-Steuer"],
            "Wohnen & Haushalt": ["Baumarkt", "Rundfunk", "Strom"],
            "Gesundheit": ["Apotheke"],
            "Kommunikation": ["Internet", "Mobilfunk"],
            "Drogerie & Pflege": [],
            "Spenden & Unterstützung": [],
        }
        for parent_name, child_names in category_paths.items():
            parent = Category(name=parent_name)
            db.add(parent)
            db.flush()
            db.add_all(Category(name=name, parent_id=parent.id) for name in child_names)
    yield settings, factory
    engine.dispose()


def _row(**overrides: str) -> dict[str, str]:
    values = dict.fromkeys(KNOWN_COLUMNS, "")
    values.update(
        {
            "Bezeichnung Auftragskonto": "Sparda Giro",
            "IBAN Auftragskonto": "DE00SYNTHETIC0000000000",
            "BIC Auftragskonto": "TESTDEFFXXX",
            "Bankname Auftragskonto": "Fiktive Sparda-Bank",
            "Buchungstag": "01.09.2026",
            "Valutadatum": "02.09.2026",
            "Name Zahlungsbeteiligter": "Fiktiver Händler",
            "IBAN Zahlungsbeteiligter": "DE00COUNTERPARTY0000000",
            "BIC (SWIFT-Code) Zahlungsbeteiligter": "FAKEDEFFXXX",
            "Buchungstext": "Kartenzahlung",
            "Verwendungszweck": "Synthetischer Testumsatz",
            "Betrag": "-48,50",
            "Waehrung": "EUR",
            "Saldo nach Buchung": "7.116,83",
        }
    )
    values.update(overrides)
    return values


def _csv_bytes(rows: list[dict[str, str]], columns: tuple[str, ...] = KNOWN_COLUMNS) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(columns), delimiter=";", lineterminator="\r\n")
    writer.writeheader()
    writer.writerows({column: row.get(column, "") for column in columns} for row in rows)
    return output.getvalue().encode("utf-8-sig")


def _stage(
    settings: Settings, factory: sessionmaker[Session], content: bytes, filename: str = "sparda.csv"
) -> ImportBatch:
    with factory() as db:
        return stage_upload(
            db,
            source_type="sparda",
            original_filename=filename,
            stream=io.BytesIO(content),
            settings=settings,
        ).batch


def _import(
    settings: Settings, factory: sessionmaker[Session], rows: list[dict[str, str]]
) -> ImportBatch:
    batch = _stage(settings, factory, _csv_bytes(rows))
    assert batch.status == "valid"
    import_sparda_batch(factory, batch.id, settings)
    with factory() as db:
        return db.get(ImportBatch, batch.id)


def test_bom_semicolon_decimal_date_optional_fields_and_changed_order(tmp_path: Path) -> None:
    columns = tuple(reversed(KNOWN_COLUMNS))
    content = _csv_bytes(
        [
            _row(
                **{
                    "Bemerkung": "",
                    "Mandatsreferenz": "",
                    "Saldo nach Buchung": "7.116,83",
                }
            )
        ],
        columns,
    )
    path = tmp_path / "sparda.csv"
    path.write_bytes(content)
    rows = parse_sparda_csv(path)
    assert content.startswith(b"\xef\xbb\xbf")
    assert rows[0].amount == D("-48.50")
    assert rows[0].balance_after == D("7116.83")
    assert rows[0].booked_at == datetime(2026, 9, 1)
    assert rows[0].value_at == datetime(2026, 9, 2)
    assert rows[0].raw_values["Bemerkung"] == ""
    assert ";" in rows[0].raw_text
    assert parse_german_decimal("7.116,83") == D("7116.83")
    assert parse_german_date("03.09.2026") == datetime(2026, 9, 3)


def test_missing_required_columns_is_rejected_and_quarantined(
    sparda_store: tuple[Settings, sessionmaker[Session]], tmp_path: Path
) -> None:
    settings, factory = sparda_store
    columns = tuple(column for column in KNOWN_COLUMNS if column != "Betrag")
    content = _csv_bytes([_row()], columns)
    path = tmp_path / "missing.csv"
    path.write_bytes(content)
    with pytest.raises(SpardaFormatError, match="Betrag"):
        parse_sparda_csv(path)
    batch = _stage(settings, factory, content)
    assert batch.status == "quarantined"
    assert batch.validation_json["issues"][0]["code"] == "missing_required_columns"


def test_exact_duplicate_file_is_not_staged_twice(
    sparda_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = sparda_store
    content = _csv_bytes([_row(**{"Name Zahlungsbeteiligter": "LIDL"})])
    first = _stage(settings, factory, content)
    with factory() as db:
        result = stage_upload(
            db,
            source_type="sparda",
            original_filename="renamed.csv",
            stream=io.BytesIO(content),
            settings=settings,
        )
        assert result.duplicate is True
        assert result.batch.id == first.id
        assert db.scalar(select(func.count()).select_from(ImportBatch)) == 1


def test_duplicate_source_row_is_skipped_across_different_files(
    sparda_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = sparda_store
    duplicate = _row(**{"Name Zahlungsbeteiligter": "LIDL"})
    _import(settings, factory, [duplicate])
    second = _import(
        settings,
        factory,
        [duplicate, _row(**{"Name Zahlungsbeteiligter": "REWE", "Betrag": "-19,90"})],
    )
    summary = second.metadata_json["import_summary"]
    assert summary["duplicate_rows"] == 1
    assert summary["source_transactions_created"] == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(SourceTransaction)) == 2


@pytest.mark.parametrize(
    ("row", "semantic", "event_type", "link_type"),
    [
        (
            _row(**{"Name Zahlungsbeteiligter": "AMERICAN EXPRESS EUROPE"}),
            "american_express_settlement",
            "transfer",
            "settlement_leg",
        ),
        (
            _row(**{"Name Zahlungsbeteiligter": "PayPal Europe S.a.r.l."}),
            "paypal_funding_leg",
            "transfer",
            "funding_leg",
        ),
    ],
)
def test_settlements_are_transfers_not_expenses(
    sparda_store: tuple[Settings, sessionmaker[Session]],
    row: dict[str, str],
    semantic: str,
    event_type: str,
    link_type: str,
) -> None:
    settings, factory = sparda_store
    batch = _import(settings, factory, [row])
    with factory() as db:
        source = db.scalar(
            select(SourceTransaction).where(SourceTransaction.import_batch_id == batch.id)
        )
        event = db.scalar(select(EconomicEvent))
        link = db.scalar(select(EventSourceLink))
        assert source.metadata_json["sparda_semantic"] == semantic
        assert event.event_type == event_type
        assert link.link_type == link_type
        assert (
            db.scalar(
                select(func.count())
                .select_from(EconomicEvent)
                .where(EconomicEvent.event_type == "expense")
            )
            == 0
        )


def test_cash_withdrawal_is_transfer_with_target_review(
    sparda_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = sparda_store
    _import(settings, factory, [_row(**{"Buchungstext": "Auszahlung girocard"})])
    with factory() as db:
        event = db.scalar(select(EconomicEvent))
        review = db.scalar(select(ReviewItem))
        assert event.event_type == "transfer"
        assert review.review_type == "transfer_target"
        assert review.proposed_event_type == "transfer"


def test_amazon_visa_settlement_creates_gap_review_not_expense(
    sparda_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = sparda_store
    batch = _import(
        settings,
        factory,
        [_row(**{"Verwendungszweck": "Abrechnung Amazon Visa-Kreditkarte"})],
    )
    with factory() as db:
        event = db.scalar(select(EconomicEvent))
        review = db.scalar(select(ReviewItem))
        assert event.event_type == "transfer"
        assert review.review_type == "missing_card_source"
        assert batch.metadata_json["import_summary"]["expenses"] == 0


def test_salary_and_merchant_refund_are_distinct_economic_types(
    sparda_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = sparda_store
    _import(
        settings,
        factory,
        [
            _row(
                **{
                    "Name Zahlungsbeteiligter": "Fiktive Arbeitgeber GmbH",
                    "Verwendungszweck": "Gehalt September",
                    "Betrag": "3.500,00",
                }
            ),
            _row(
                **{
                    "Name Zahlungsbeteiligter": "Fiktiver Versandhandel",
                    "Verwendungszweck": "Erstattung Retoure",
                    "Betrag": "48,50",
                    "Saldo nach Buchung": "7.165,33",
                }
            ),
        ],
    )
    with factory() as db:
        assert list(db.scalars(select(EconomicEvent.event_type).order_by(EconomicEvent.id))) == [
            "income",
            "refund",
        ]


@pytest.mark.parametrize(
    ("merchant", "purpose", "expected"),
    [
        ("LIDL", "Einkauf", "Lebensmittel / Supermarkt"),
        ("Bäckerei Morgen", "Brötchen", "Lebensmittel / Bäckerei"),
        ("Metzgerei Muster", "Einkauf", "Lebensmittel / Metzgerei"),
        ("ARAL", "Kraftstoff", "Auto & Mobilität / Tanken"),
        ("dm Drogerie", "Einkauf", "Drogerie & Pflege"),
        ("Apotheke am Markt", "Arznei", "Gesundheit / Apotheke"),
        ("PARKSTER", "Parkvorgang", "Auto & Mobilität / Parken"),
        ("BAUHAUS", "Material", "Wohnen & Haushalt / Baumarkt"),
        ("Deutsche Glasfaser", "Internet", "Kommunikation / Internet"),
        ("klarmobil", "Mobilfunk", "Kommunikation / Mobilfunk"),
        ("Beitragsservice", "Rundfunkbeitrag", "Wohnen & Haushalt / Rundfunk"),
        ("Vattenfall", "Strom Abschlag", "Wohnen & Haushalt / Strom"),
        ("Hauptzollamt", "KFZ-Steuer", "Auto & Mobilität / KFZ-Steuer"),
        ("donate.jw.org", "Spende", "Spenden & Unterstützung"),
    ],
)
def test_category_suggestions_are_separate_from_event_type(
    merchant: str, purpose: str, expected: str
) -> None:
    suggestion = suggest_sparda_category(merchant, "Lastschrift", purpose)
    decision = classify_sparda_transaction(
        amount=D("-10"),
        counterparty=merchant,
        booking_text="Lastschrift",
        purpose=purpose,
    )
    assert suggestion.path == expected
    assert decision.event_type == "expense"
    assert decision.category == suggestion
    assert not hasattr(decision, "envelope")


def test_unknown_debit_creates_review_without_guessed_event(
    sparda_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = sparda_store
    _import(settings, factory, [_row()])
    with factory() as db:
        review = db.scalar(select(ReviewItem))
        assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 0
        assert review.review_type == "economic_type"
        assert review.proposed_event_type == "expense"


def test_ambiguous_positive_transfer_does_not_force_income_or_refund(
    sparda_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = sparda_store
    _import(settings, factory, [_row(**{"Betrag": "125,00"})])
    with factory() as db:
        review = db.scalar(select(ReviewItem))
        assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 0
        assert review.review_type == "economic_type"
        assert review.proposed_event_type is None


def test_parser_failure_rolls_back_sparda_import_atomically(
    sparda_store: tuple[Settings, sessionmaker[Session]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, factory = sparda_store
    content = _csv_bytes(
        [
            _row(**{"Name Zahlungsbeteiligter": "LIDL"}),
            _row(**{"Name Zahlungsbeteiligter": "REWE", "Betrag": "-12,00"}),
        ]
    )
    batch = _stage(settings, factory, content)
    from app.importers import sparda

    original = sparda._decision
    calls = 0

    def fail_second(row: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic parser failure")
        return original(row)

    monkeypatch.setattr(sparda, "_decision", fail_second)
    with pytest.raises(RuntimeError, match="synthetic parser failure"):
        import_sparda_batch(factory, batch.id, settings)
    with factory() as db:
        assert db.get(ImportBatch, batch.id).status == "failed"
        assert db.scalar(select(func.count()).select_from(RawImportRecord)) == 0
        assert db.scalar(select(func.count()).select_from(SourceTransaction)) == 0
        assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 0
        assert db.scalar(select(func.count()).select_from(ReviewItem)) == 0


def test_money_stays_decimal_and_summary_logs_ui_hide_identifiers(
    sparda_store: tuple[Settings, sessionmaker[Session]], caplog: pytest.LogCaptureFixture
) -> None:
    settings, factory = sparda_store
    caplog.set_level(logging.INFO)
    batch = _import(settings, factory, [_row(**{"Name Zahlungsbeteiligter": "LIDL"})])
    with factory() as db:
        source = db.scalar(select(SourceTransaction))
        event = db.scalar(select(EconomicEvent))
        assert isinstance(source.amount, Decimal)
        assert isinstance(source.balance_after, Decimal)
        assert isinstance(event.amount, Decimal)

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.get(f"/import?batch_id={batch.id}")
            assert response.status_code == 200
            page = response.text
    finally:
        app.dependency_overrides.clear()
    combined = page + caplog.text
    for sensitive in (
        "DE00SYNTHETIC0000000000",
        "DE00COUNTERPARTY0000000",
        "TESTDEFFXXX",
        "FAKEDEFFXXX",
    ):
        assert sensitive not in combined
    assert "Quellzeilen" in page
    assert "Source Transactions" in page


def test_synthetic_sparda_import_end_to_end_through_ui(
    sparda_store: tuple[Settings, sessionmaker[Session]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, factory = sparda_store
    content = _csv_bytes([_row(**{"Name Zahlungsbeteiligter": "LIDL"})])

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    monkeypatch.setattr("app.web.routes.get_settings", lambda: settings)
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            staged = client.post(
                "/import/stage",
                data={"source_type": "sparda"},
                files={"upload": ("sparda.csv", content, "text/csv")},
                follow_redirects=False,
            )
            assert staged.status_code == 303
            assert staged.headers["location"].endswith("/preview")
            preview = client.get(staged.headers["location"])
            assert preview.status_code == 200
            assert "Lebensmittel / Supermarkt" in preview.text
            assert "DE00SYNTHETIC0000000000" not in preview.text

            batch_id = int(staged.headers["location"].split("/")[-2])
            imported = client.post(f"/import/{batch_id}/execute", follow_redirects=False)
            assert imported.status_code == 303
            summary = client.get(imported.headers["location"])
            assert summary.status_code == 200
            assert "Import abgeschlossen" in summary.text
            assert "Source Transactions" in summary.text
            assert "DE00COUNTERPARTY0000000" not in summary.text
    finally:
        app.dependency_overrides.clear()

    with factory() as db:
        batch = db.get(ImportBatch, batch_id)
        assert batch.status == "imported"
        assert batch.metadata_json["import_summary"]["expenses"] == 1
