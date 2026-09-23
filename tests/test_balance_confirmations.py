from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.migrations import upgrade_database
from app.db.models import (
    Account,
    BalanceConfirmation,
    Envelope,
    EnvelopeSnapshot,
    ImmutableRecordError,
    SourceTransaction,
)
from app.db.session import get_db
from app.main import app
from app.services.balance_confirmations import (
    account_balance_view,
    backfill_sparda_imported_balance,
    calculate_envelope_cash_total,
    create_balance_confirmation,
    vault_free_cash,
)
from app.services.dashboard import build_dashboard

D = Decimal


@pytest.fixture
def balance_store(tmp_path: Path) -> tuple[Settings, sessionmaker[Session]]:
    database_url = f"sqlite:///{(tmp_path / 'balances.db').as_posix()}"
    settings = Settings(
        private_database_url=database_url,
        demo_mode=False,
        private_data_dir=tmp_path / "private",
        backup_dir=tmp_path / "backups",
        log_dir=tmp_path / "logs",
    )
    upgrade_database(database_url)
    engine = create_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield settings, factory
    engine.dispose()


def _account(db: Session, name: str, account_type: str, **kwargs: object) -> Account:
    account = Account(
        name=name,
        account_type=account_type,
        balance=D("0"),
        balance_confirmed=False,
        **kwargs,
    )
    db.add(account)
    db.flush()
    return account


def _confirm(
    db: Session,
    account: Account,
    amount: str,
    when: datetime,
    *,
    status: str = "confirmed",
    source_type: str = "bank_statement",
    envelope_cash_total: str | None = None,
) -> BalanceConfirmation:
    return create_balance_confirmation(
        db,
        account=account,
        confirmed_at=when,
        entered_balance=D(amount),
        source_type=source_type,
        status=status,
        envelope_cash_total=(D(envelope_cash_total) if envelope_cash_total else None),
    )


def test_balance_confirmations_are_append_only_and_newest_confirmed_wins(
    balance_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    _, factory = balance_store
    with factory.begin() as db:
        account = _account(db, "Synthetisches Giro", "checking")
        older = _confirm(db, account, "100", datetime(2026, 1, 1, 10))
        _confirm(db, account, "999", datetime(2026, 3, 1, 10), status="provisional")
        newest = _confirm(db, account, "125", datetime(2026, 2, 1, 10))
        db.flush()
        account_id = account.id
        older_id = older.id
        newest_id = newest.id

    with factory() as db:
        view = account_balance_view(db, db.get(Account, account_id))
        assert view.current.id == newest_id
        assert view.current.balance == D("125.00")
        row = db.get(BalanceConfirmation, older_id)
        row.notes = "mutation"
        with pytest.raises(ImmutableRecordError):
            db.flush()
        db.rollback()
        row = db.get(BalanceConfirmation, older_id)
        db.delete(row)
        with pytest.raises(ImmutableRecordError):
            db.flush()

    with factory() as db, pytest.raises(IntegrityError):
        db.execute(
            text("UPDATE balance_confirmations SET notes='forbidden' WHERE id=:id"),
            {"id": older_id},
        )
        db.commit()


def test_unknown_and_inactive_balances_are_excluded_and_amex_is_a_liability(
    balance_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    _, factory = balance_store
    with factory.begin() as db:
        checking = _account(db, "Synthetisches Giro", "checking")
        amex = _account(db, "Synthetische Karte", "credit_card", is_liability=True)
        _account(db, "Unbestätigtes PayPal", "paypal")
        c24 = _account(db, "Historisches Konto", "checking", is_active=False)
        _confirm(db, checking, "1000", datetime(2026, 4, 1, 12))
        card = _confirm(
            db,
            amex,
            "200",
            datetime(2026, 4, 1, 12),
            source_type="card_statement",
        )
        _confirm(db, c24, "9000", datetime(2026, 4, 1, 12))
        db.flush()
        assert card.balance == D("-200.00")

    with factory() as db:
        dashboard = build_dashboard(db)
        assert dashboard["wealth"] == D("800.00")
        assert dashboard["card_liabilities"] == D("200.00")
        assert dashboard["unreconciled_active_count"] == 1
        assert dashboard["wealth_has_unknown"]


def test_wallet_manual_count_and_vault_cash_reconciliation_without_double_count(
    balance_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    _, factory = balance_store
    with factory.begin() as db:
        wallet = _account(db, "Test-Portemonnaie", "cash_wallet")
        vault = _account(db, "Test-Tresor", "cash_vault")
        first = Envelope(name="Test-Umschlag A", sort_order=1)
        second = Envelope(name="Test-Umschlag B", sort_order=2)
        db.add_all([first, second])
        db.flush()
        db.add_all(
            [
                EnvelopeSnapshot(
                    envelope_id=first.id,
                    snapshot_date=date(2026, 4, 30),
                    physical_balance=D("120"),
                    source="synthetic",
                    is_confirmed=True,
                ),
                EnvelopeSnapshot(
                    envelope_id=second.id,
                    snapshot_date=date(2026, 4, 30),
                    physical_balance=D("80"),
                    source="synthetic",
                    is_confirmed=True,
                ),
            ]
        )
        db.flush()
        wallet_row = _confirm(
            db,
            wallet,
            "45",
            datetime(2026, 5, 1, 12),
            source_type="manual_count",
        )
        vault_row = _confirm(
            db,
            vault,
            "500",
            datetime(2026, 5, 1, 12),
            source_type="manual_count",
            envelope_cash_total="200",
        )
        db.flush()
        assert wallet_row.source_type == "manual_count"
        assert calculate_envelope_cash_total(db) == D("200.00")
        assert vault_free_cash(vault_row) == D("300.00")
        assert vault_row.reconciliation_warning is None

    with factory() as db:
        dashboard = build_dashboard(db)
        assert dashboard["reserved"] == D("200.00")
        assert dashboard["wealth"] == D("545.00")
        assert dashboard["free_vault"] == D("300.00")


def test_vault_rejects_envelopes_above_total_and_warns_on_mismatch(
    balance_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    _, factory = balance_store
    with factory.begin() as db:
        vault = _account(db, "Test-Tresor", "cash_vault")
        envelope = Envelope(name="Test-Umschlag", sort_order=1)
        db.add(envelope)
        db.flush()
        db.add(
            EnvelopeSnapshot(
                envelope_id=envelope.id,
                snapshot_date=date(2026, 4, 30),
                physical_balance=D("100"),
                source="synthetic",
                is_confirmed=True,
            )
        )
        db.flush()
        with pytest.raises(ValueError, match="nicht überschreiten"):
            _confirm(
                db,
                vault,
                "50",
                datetime(2026, 5, 1, 12),
                source_type="manual_count",
                envelope_cash_total="75",
            )
        warning = _confirm(
            db,
            vault,
            "300",
            datetime(2026, 5, 1, 12),
            source_type="manual_count",
            envelope_cash_total="90",
        )
        assert warning.calculated_envelope_total == D("100.00")
        assert warning.reconciliation_warning == "envelope_total_mismatch"


def test_sparda_imported_balance_backfill_is_dated_and_idempotent(
    balance_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = balance_store
    with factory.begin() as db:
        account = _account(db, "Sparda Girokonto", "checking")
        for index, (when, balance) in enumerate(
            [(datetime(2026, 5, 1, 12), "700"), (datetime(2026, 5, 2, 12), "725")]
        ):
            db.add(
                SourceTransaction(
                    source_system="sparda",
                    source_transaction_id=f"synthetic-{index}",
                    booked_at=when,
                    value_at=when,
                    description_raw="Synthetisch",
                    amount=D("25"),
                    currency="EUR",
                    balance_after=D(balance),
                    metadata_json={},
                    fingerprint=(str(index) * 64),
                )
            )
        db.flush()
        account_id = account.id

    with factory.begin() as db:
        assert backfill_sparda_imported_balance(db, settings)
    with factory.begin() as db:
        assert not backfill_sparda_imported_balance(db, settings)

    with factory() as db:
        view = account_balance_view(db, db.get(Account, account_id))
        assert view.current.balance == D("725.00")
        assert view.current.confirmed_at == datetime(2026, 5, 2, 12)
        assert view.current.source_type == "imported_balance"
        assert db.scalar(select(func.count()).select_from(BalanceConfirmation)) == 1


def test_private_account_confirmation_ui_handles_wallet_and_vault(
    balance_store: tuple[Settings, sessionmaker[Session]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, factory = balance_store
    with factory.begin() as db:
        wallet = _account(db, "Portemonnaie", "cash_wallet")
        vault = _account(db, "Tresor", "cash_vault")
        envelope = Envelope(name="Synthetischer Umschlag", sort_order=1)
        db.add(envelope)
        db.flush()
        db.add(
            EnvelopeSnapshot(
                envelope_id=envelope.id,
                snapshot_date=date(2026, 4, 30),
                physical_balance=D("100"),
                source="synthetic",
                is_confirmed=True,
            )
        )
        wallet_id = wallet.id
        vault_id = vault.id

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    monkeypatch.setattr("app.web.routes.get_settings", lambda: settings)
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            accounts_page = client.get("/accounts")
            assert accounts_page.status_code == 200
            assert "Nicht abgestimmt" in accounts_page.text
            assert "Saldo bestätigen" in accounts_page.text

            wallet_form = client.get(f"/accounts/{wallet_id}/confirm")
            assert wallet_form.status_code == 200
            assert "Portemonnaie zählen" in wallet_form.text
            wallet_saved = client.post(
                f"/accounts/{wallet_id}/confirm",
                data={
                    "confirmed_at": "2026-05-01T12:00",
                    "balance": "45,00",
                    "source_type": "manual_count",
                    "status": "confirmed",
                },
                follow_redirects=False,
            )
            assert wallet_saved.status_code == 303

            invalid_vault = client.post(
                f"/accounts/{vault_id}/confirm",
                data={
                    "confirmed_at": "2026-05-01T12:00",
                    "balance": "50,00",
                    "envelope_cash_total": "75,00",
                    "source_type": "manual_count",
                    "status": "confirmed",
                },
            )
            assert invalid_vault.status_code == 422
            assert "nicht überschreiten" in invalid_vault.text

            warning_vault = client.post(
                f"/accounts/{vault_id}/confirm",
                data={
                    "confirmed_at": "2026-05-01T12:00",
                    "balance": "300,00",
                    "envelope_cash_total": "90,00",
                    "source_type": "manual_count",
                    "status": "confirmed",
                },
                follow_redirects=False,
            )
            assert warning_vault.status_code == 303
            history = client.get(warning_vault.headers["location"])
            assert "Umschlagabweichung" in history.text
            assert "freies Tresorgeld" in history.text
    finally:
        app.dependency_overrides.clear()
