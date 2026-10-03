from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Float, Numeric, create_engine, event, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.migrations import upgrade_database
from app.db.models import (
    Account,
    EconomicEvent,
    Envelope,
    EnvelopeRulePeriod,
    EventSourceLink,
    ImmutableRecordError,
    ImportBatch,
    RawImportRecord,
    SourceTransaction,
)
from app.web.templating import euro

D = Decimal


@pytest.fixture
def migrated_engine(tmp_path: object) -> Iterator[object]:
    database_path = tmp_path / "audit.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    upgrade_database(database_url)
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection: object, _record: object) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    yield engine
    engine.dispose()


def _source_graph(session: Session) -> tuple[RawImportRecord, SourceTransaction]:
    batch = ImportBatch(
        source_type="demo_bank",
        filename="fictional-input.txt",
        source_hash="batch-hash",
        status="imported",
        row_count=1,
        metadata_json={},
    )
    session.add(batch)
    session.flush()
    raw = RawImportRecord(
        import_batch_id=batch.id,
        source_row_key="row-1",
        raw_payload_json={"fictional": True},
        raw_text="fictional source row",
        raw_hash="raw-hash",
    )
    session.add(raw)
    session.flush()
    source = SourceTransaction(
        import_batch_id=batch.id,
        raw_record_id=raw.id,
        source_system="demo_bank",
        source_transaction_id="source-1",
        booked_at=datetime(2026, 1, 1, 12),
        description_raw="Fictional purchase",
        amount=D("12.34"),
        currency="EUR",
        status="booked",
        metadata_json={},
        fingerprint="source-fingerprint",
    )
    session.add(source)
    session.commit()
    return raw, source


def test_all_persisted_numeric_columns_avoid_binary_float() -> None:
    numeric_columns = [column for table in Base.metadata.tables.values() for column in table.c]
    assert not any(isinstance(column.type, Float) for column in numeric_columns)
    for column in numeric_columns:
        if isinstance(column.type, Numeric):
            assert column.type.asdecimal is True


def test_currency_formatting_never_converts_through_float() -> None:
    assert euro(D("999999999999.995")) == "1.000.000.000.000,00 €"


def test_migration_reaches_head_with_expected_schema(migrated_engine: object) -> None:
    schema = inspect(migrated_engine)
    assert "raw_import_records" in schema.get_table_names()
    assert "source_transactions" in schema.get_table_names()
    assert "economic_events" in schema.get_table_names()
    assert "envelope_assignment_decisions" in schema.get_table_names()
    assert "category_assignment_decisions" in schema.get_table_names()
    assert "amazon_enrichment_records" in schema.get_table_names()
    assert "amazon_payment_matches" in schema.get_table_names()
    assert "import_conflicts" in schema.get_table_names()
    with migrated_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261003_0011"
    assert {
        "uq_import_batches_imported_hash",
        "uq_import_batches_active_hash_source",
    }.issubset({index["name"] for index in schema.get_indexes("import_batches")})


def test_raw_and_source_records_are_append_only_in_orm_and_database(
    migrated_engine: object,
) -> None:
    with Session(migrated_engine) as session:
        raw, source = _source_graph(session)
        raw.raw_text = "changed"
        with pytest.raises(ImmutableRecordError):
            session.commit()
        session.rollback()

        source = session.get(SourceTransaction, source.id)
        session.delete(source)
        with pytest.raises(ImmutableRecordError):
            session.commit()
        session.rollback()

        with pytest.raises(IntegrityError):
            session.execute(
                text("UPDATE raw_import_records SET raw_text = 'bypass' WHERE id = :id"),
                {"id": raw.id},
            )
            session.commit()
        session.rollback()

        with pytest.raises(IntegrityError):
            session.execute(
                text("DELETE FROM source_transactions WHERE id = :id"), {"id": source.id}
            )
            session.commit()


def test_source_and_economic_events_are_separate_and_one_source_is_canonical_once(
    migrated_engine: object,
) -> None:
    with Session(migrated_engine) as session:
        _, source = _source_graph(session)
        account = Account(
            name="Fictional checking",
            account_type="checking",
            currency="EUR",
            balance=D("100.00"),
        )
        first = EconomicEvent(
            event_type="expense",
            occurred_at=datetime(2026, 1, 1, 12),
            description="Fictional economic event",
            amount=D("12.34"),
            account=account,
        )
        second = EconomicEvent(
            event_type="expense",
            occurred_at=datetime(2026, 1, 1, 12),
            description="Duplicate economic event",
            amount=D("12.34"),
            account=account,
        )
        session.add_all([first, second])
        session.flush()
        session.add(
            EventSourceLink(
                economic_event_id=first.id,
                source_transaction_id=source.id,
                link_type="canonical_source",
            )
        )
        session.commit()
        session.add(
            EventSourceLink(
                economic_event_id=second.id,
                source_transaction_id=source.id,
                link_type="canonical_source",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        assert SourceTransaction.__table__.name != EconomicEvent.__table__.name
        assert session.scalar(select(SourceTransaction.amount)) == D("12.34")
        assert session.scalar(
            select(EconomicEvent.amount).where(EconomicEvent.id == first.id)
        ) == D("12.34")


def test_historical_envelope_rules_are_append_only_and_non_overlapping(
    migrated_engine: object,
) -> None:
    with Session(migrated_engine) as session:
        envelope = Envelope(name="Fictional envelope", target_rule_type="monthly_contribution")
        session.add(envelope)
        session.flush()
        rule = EnvelopeRulePeriod(
            envelope_id=envelope.id,
            valid_from=date(2026, 1, 1),
            valid_to=date(2026, 5, 31),
            monthly_amount=D("25"),
            rule_type="monthly_contribution",
        )
        session.add(rule)
        session.commit()

        rule.monthly_amount = D("30")
        with pytest.raises(ImmutableRecordError):
            session.commit()
        session.rollback()

        session.add(
            EnvelopeRulePeriod(
                envelope_id=envelope.id,
                valid_from=date(2026, 5, 1),
                valid_to=None,
                monthly_amount=D("30"),
                rule_type="monthly_contribution",
            )
        )
        with pytest.raises((IntegrityError, ValueError)):
            session.commit()
