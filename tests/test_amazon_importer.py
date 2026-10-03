from __future__ import annotations

import csv
import io
import zipfile
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
    AmazonEnrichmentRecord,
    AmazonPaymentMatch,
    Category,
    EconomicEvent,
    EventSourceLink,
    ImportBatch,
    ImportConflict,
    RawImportRecord,
    SourceTransaction,
)
from app.db.session import get_db
from app.importers import amazon
from app.importers.amazon import (
    analyze_amazon_export,
    import_amazon_batch,
    parse_amazon_export,
)
from app.main import app
from app.services.import_staging import stage_upload

D = Decimal

ORDER_COLUMNS = (
    "ASIN",
    "Order Date",
    "Order ID",
    "Order Status",
    "Original Quantity",
    "Payment Method Type",
    "Product Name",
    "Ship Date",
    "Shipment Item Subtotal",
    "Shipment Item Subtotal Tax",
    "Total Amount",
    "Unit Price",
    "Currency",
)
RETURN_COLUMNS = (
    "Return Item ID",
    "Order ID",
    "Order Item ID",
    "Request Date",
    "Refund Status",
    "Refund Amount",
    "Currency Code",
)


@pytest.fixture
def amazon_store(tmp_path: Path) -> tuple[Settings, sessionmaker[Session]]:
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
    yield settings, factory
    engine.dispose()


def _order(index: int, **overrides: str) -> dict[str, str]:
    row = {
        "ASIN": f"SYN-ASIN-{index}",
        "Order Date": f"2026-09-{index + 1:02d}",
        "Order ID": f"SYN-ORDER-{index}",
        "Order Status": "Shipped",
        "Original Quantity": "1",
        "Payment Method Type": "American Express",
        "Product Name": f"Synthetic household item {index}",
        "Ship Date": f"2026-09-{index + 2:02d}",
        "Shipment Item Subtotal": "20.00",
        "Shipment Item Subtotal Tax": "0.00",
        "Total Amount": "20.00",
        "Unit Price": "20.00",
        "Currency": "EUR",
    }
    row.update(overrides)
    return row


def _return(index: int = 1) -> dict[str, str]:
    return {
        "Return Item ID": f"SYN-RETURN-{index}",
        "Order ID": f"SYN-ORDER-{index}",
        "Order Item ID": f"SYN-ITEM-{index}",
        "Request Date": "2026-09-20",
        "Refund Status": "Refunded",
        "Refund Amount": "20.00",
        "Currency Code": "EUR",
    }


def _csv_bytes(columns: tuple[str, ...], rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _zip_bytes(orders: list[dict[str, str]], returns: list[dict[str, str]] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Your Amazon Orders/Order History.csv",
            _csv_bytes(ORDER_COLUMNS, orders),
        )
        if returns is not None:
            archive.writestr(
                "Additional Data/Your Orders.Returns.2/Your Orders.Returns.2.csv",
                _csv_bytes(RETURN_COLUMNS, returns),
            )
    return output.getvalue()


def _stage(
    settings: Settings, factory: sessionmaker[Session], content: bytes, name: str = "orders.zip"
) -> ImportBatch:
    with factory() as db:
        return stage_upload(
            db,
            source_type="amazon",
            original_filename=name,
            stream=io.BytesIO(content),
            settings=settings,
        ).batch


def _amazon_event(
    db: Session, *, amount: str = "20.00", day: int = 1
) -> tuple[SourceTransaction, EconomicEvent]:
    source = SourceTransaction(
        source_system="amex",
        source_transaction_id=f"synthetic-amazon-{day}-{amount}",
        booked_at=datetime(2026, 9, day),
        merchant_raw="AMAZON SYNTHETIC",
        description_raw="Synthetic Amazon purchase",
        amount=D(amount),
        currency="EUR",
        fingerprint=(f"amazon-{day}-{amount}" * 8)[:64],
    )
    event = EconomicEvent(
        event_type="expense",
        occurred_at=source.booked_at,
        description="Synthetic Amazon purchase",
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
    return source, event


def test_parse_exact_duplicate_row_and_order_item_identity(tmp_path: Path) -> None:
    path = tmp_path / "orders.zip"
    path.write_bytes(_zip_bytes([_order(1), _order(1)]))

    rows = parse_amazon_export(path)

    assert len(rows) == 2
    assert rows[0].natural_key == rows[1].natural_key
    assert rows[0].content_hash == rows[1].content_hash
    assert rows[0].amount == D("20.00")


def test_productive_scope_reports_full_archive_but_imports_only_from_2026(
    amazon_store: tuple[Settings, sessionmaker[Session]], tmp_path: Path
) -> None:
    settings, factory = amazon_store
    historical = _order(
        1,
        **{"Order Date": "2025-12-31", "Ship Date": "2025-12-31"},
    )
    current = _order(2, **{"Order Date": "2026-01-01", "Ship Date": "2026-01-02"})
    old_return = _return(1)
    old_return["Request Date"] = "2025-12-31"
    current_return = _return(2)
    current_return["Request Date"] = "2026-01-02"
    content = _zip_bytes([historical, current], [old_return, current_return])
    path = tmp_path / "scope.zip"
    path.write_bytes(content)

    with factory() as db:
        analysis = analyze_amazon_export(path, db, settings.amazon_import_start_date)
    assert analysis.summary.archive_source_rows == 4
    assert analysis.summary.archive_orders == 2
    assert analysis.summary.archive_order_items == 2
    assert analysis.summary.archive_returns_refunds == 2
    assert analysis.summary.archive_date_from == "2025-12-31"
    assert analysis.summary.source_rows_parsed == 2
    assert analysis.summary.orders == 1
    assert analysis.summary.order_items == 1
    assert analysis.summary.returns_refunds == 1
    assert analysis.summary.excluded_before_scope == 2

    batch = _stage(settings, factory, content)
    import_amazon_batch(factory, batch.id, settings)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AmazonEnrichmentRecord)) == 2
        assert db.scalar(select(func.count()).select_from(SourceTransaction)) == 0
        assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 0


def test_same_file_twice_is_file_duplicate_and_writes_nothing(
    amazon_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = amazon_store
    content = _zip_bytes([_order(1)])
    batch = _stage(settings, factory, content)
    import_amazon_batch(factory, batch.id, settings)

    with factory() as db:
        before = db.scalar(select(func.count()).select_from(AmazonEnrichmentRecord))
        duplicate = stage_upload(
            db,
            source_type="amazon",
            original_filename="same.zip",
            stream=io.BytesIO(content),
            settings=settings,
        )
        after = db.scalar(select(func.count()).select_from(AmazonEnrichmentRecord))

    assert duplicate.duplicate is True
    assert duplicate.batch.id == batch.id
    assert before == after == 1


def test_eighty_percent_overlap_inserts_only_new_row(
    amazon_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = amazon_store
    first = _stage(settings, factory, _zip_bytes([_order(i) for i in range(1, 5)]))
    import_amazon_batch(factory, first.id, settings)
    second = _stage(settings, factory, _zip_bytes([_order(i) for i in range(1, 6)]), "next.zip")
    import_amazon_batch(factory, second.id, settings)

    with factory() as db:
        current = db.get(ImportBatch, second.id)
        assert current is not None
        assert current.metadata_json["import_summary"]["existing_exact_rows"] == 4
        assert current.metadata_json["import_summary"]["new_rows"] == 1
        assert db.scalar(select(func.count()).select_from(AmazonEnrichmentRecord)) == 5


def test_same_source_record_across_three_batches_exists_once(
    amazon_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = amazon_store
    for index, extra in enumerate(([], [_order(2)], [_order(3)]), start=1):
        batch = _stage(
            settings,
            factory,
            _zip_bytes([_order(1), *extra]),
            f"batch-{index}.zip",
        )
        import_amazon_batch(factory, batch.id, settings)

    with factory() as db:
        rows = list(
            db.scalars(
                select(AmazonEnrichmentRecord).where(
                    AmazonEnrichmentRecord.order_key
                    == parse_amazon_export(
                        _write_zip(Path(settings.private_data_dir) / "probe.zip", [_order(1)])
                    )[0].order_key
                )
            )
        )
        assert len(rows) == 1


def _write_zip(path: Path, orders: list[dict[str, str]]) -> Path:
    path.write_bytes(_zip_bytes(orders))
    return path


def test_material_change_creates_conflict_without_overwrite(
    amazon_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = amazon_store
    first = _stage(settings, factory, _zip_bytes([_order(1)]))
    import_amazon_batch(factory, first.id, settings)
    changed = _order(1, **{"Total Amount": "25.00"})
    second = _stage(settings, factory, _zip_bytes([changed]), "changed.zip")
    import_amazon_batch(factory, second.id, settings)

    with factory() as db:
        original = db.scalar(select(AmazonEnrichmentRecord))
        conflict = db.scalar(select(ImportConflict))
        assert original is not None and original.amount == D("20.00")
        assert conflict is not None and conflict.status == "open"
        assert db.scalar(select(func.count()).select_from(AmazonEnrichmentRecord)) == 1


def test_refund_repeated_in_overlapping_export_is_stored_once(
    amazon_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = amazon_store
    first = _stage(settings, factory, _zip_bytes([_order(1)], [_return()]))
    import_amazon_batch(factory, first.id, settings)
    second = _stage(settings, factory, _zip_bytes([_order(1), _order(2)], [_return()]), "later.zip")
    import_amazon_batch(factory, second.id, settings)

    with factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AmazonEnrichmentRecord)
                .where(AmazonEnrichmentRecord.record_type == "refund")
            )
            == 1
        )


def test_high_confidence_match_is_one_link_and_no_new_event(
    amazon_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = amazon_store
    with factory.begin() as db:
        _source, event = _amazon_event(db, day=2)
        category = Category(name="Synthetic category")
        db.add(category)
        db.flush()
        event.category_id = category.id
        original_category_id = category.id
    batch = _stage(settings, factory, _zip_bytes([_order(1)]))
    with factory() as db:
        event_count = db.scalar(select(func.count()).select_from(EconomicEvent))
    import_amazon_batch(factory, batch.id, settings)

    with factory() as db:
        assert db.scalar(select(func.count()).select_from(EconomicEvent)) == event_count
        assert db.scalar(select(func.count()).select_from(AmazonPaymentMatch)) == 1
        match = db.scalar(select(AmazonPaymentMatch))
        assert match is not None and match.status == "linked"
        persisted_event = db.get(EconomicEvent, match.economic_event_id)
        assert persisted_event is not None and persisted_event.category_id == original_category_id


def test_ambiguous_match_is_review_only_and_gift_card_never_high(
    amazon_store: tuple[Settings, sessionmaker[Session]], tmp_path: Path
) -> None:
    _settings, factory = amazon_store
    with factory.begin() as db:
        _amazon_event(db, day=2)
        _amazon_event(db, day=3)
    path = tmp_path / "ambiguous.zip"
    path.write_bytes(
        _zip_bytes(
            [
                _order(1),
                _order(
                    2,
                    **{
                        "Order Date": "2026-09-01",
                        "Payment Method Type": "Gift Certificate + American Express",
                    },
                ),
            ]
        )
    )
    with factory() as db:
        analysis = analyze_amazon_export(path, db)

    assert analysis.summary.high_confidence_payment_matches == 0
    assert analysis.summary.medium_confidence_payment_matches == 2
    assert analysis.summary.gift_card_split_cases == 1


def test_category_suggestion_is_advisory_only(
    amazon_store: tuple[Settings, sessionmaker[Session]], tmp_path: Path
) -> None:
    _settings, factory = amazon_store
    with factory.begin() as db:
        _source, event = _amazon_event(db, day=2)
        category = Category(name="Manual category")
        db.add(category)
        db.flush()
        event.category_id = category.id
        event_id = event.id
        category_id = category.id
    path = tmp_path / "clothing.zip"
    path.write_bytes(_zip_bytes([_order(1, **{"Product Name": "Synthetic shirt"})]))
    with factory() as db:
        analysis = analyze_amazon_export(path, db)
        persisted = db.get(EconomicEvent, event_id)

    assert analysis.rows[0].category_suggestion == "Kleidung / Kleidung"
    assert persisted is not None and persisted.category_id == category_id


def test_unescaped_comma_in_product_name_does_not_shift_financial_columns(
    tmp_path: Path,
) -> None:
    row = _order(1, **{"Product Name": "Synthetic shirt, blue"})
    header = ",".join(ORDER_COLUMNS)
    malformed = ",".join(row[column] for column in ORDER_COLUMNS)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Your Amazon Orders/Order History.csv",
            f"{header}\n{malformed}\n".encode(),
        )
    path = tmp_path / "unescaped-title-comma.zip"
    path.write_bytes(output.getvalue())

    parsed = parse_amazon_export(path)

    assert len(parsed) == 1
    assert parsed[0].product_title == "Synthetic shirt, blue"
    assert parsed[0].amount == D("20.00")


def test_not_applicable_optional_date_is_preserved_without_failure(tmp_path: Path) -> None:
    returned = _return()
    returned["Request Date"] = "Not Applicable"
    path = tmp_path / "optional-date.zip"
    path.write_bytes(_zip_bytes([_order(1)], [returned]))

    parsed = parse_amazon_export(path)

    refund = next(row for row in parsed if row.record_type == "refund")
    assert refund.occurred_at is None


def test_failed_import_rolls_back_and_retries_atomically(
    amazon_store: tuple[Settings, sessionmaker[Session]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, factory = amazon_store
    batch = _stage(settings, factory, _zip_bytes([_order(1)]))
    real_import = amazon._import_rows

    def fail_after_write(
        db: Session,
        current: ImportBatch,
        _path: Path,
        *,
        scope_start: object,
    ) -> int:
        assert scope_start == settings.amazon_import_start_date
        db.add(
            RawImportRecord(
                import_batch_id=current.id,
                source_row_key="synthetic-failure",
                raw_payload_json={"safe": "synthetic"},
                raw_text="synthetic",
                raw_hash="f" * 64,
            )
        )
        db.flush()
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(amazon, "_import_rows", fail_after_write)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        import_amazon_batch(factory, batch.id, settings)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(RawImportRecord)) == 0
        assert db.get(ImportBatch, batch.id).status == "failed"

    monkeypatch.setattr(amazon, "_import_rows", real_import)
    import_amazon_batch(factory, batch.id, settings)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AmazonEnrichmentRecord)) == 1
        assert db.get(ImportBatch, batch.id).status == "imported"


def test_stage_preview_ui_is_enrichment_only(
    amazon_store: tuple[Settings, sessionmaker[Session]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, factory = amazon_store

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    monkeypatch.setattr("app.web.routes.get_settings", lambda: settings)
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            staged = client.post(
                "/import/stage",
                data={"source_type": "amazon"},
                files={"upload": ("orders.zip", _zip_bytes([_order(1)]), "application/zip")},
                follow_redirects=False,
            )
            assert staged.status_code == 303
            assert staged.headers["location"].endswith("/preview")
            preview = client.get(staged.headers["location"])
            assert preview.status_code == 200
            assert "Amazon-Enrichment prüfen" in preview.text
            assert "erzeugt keine neuen Ausgaben" in preview.text
    finally:
        app.dependency_overrides.clear()
