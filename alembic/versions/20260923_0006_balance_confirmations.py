"""Add append-only dated account balance confirmations.

Revision ID: 20260923_0006
Revises: 20260923_0005
"""

import sqlalchemy as sa

from alembic import op

revision = "20260923_0006"
down_revision = "20260923_0005"
branch_labels = None
depends_on = None


def _create_append_only_triggers() -> None:
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER prevent_balance_confirmations_{action.lower()}
            BEFORE {action} ON balance_confirmations
            BEGIN
                SELECT RAISE(ABORT, 'balance_confirmations is append-only');
            END
            """
        )


def upgrade() -> None:
    op.create_table(
        "balance_confirmations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), nullable=False),
        sa.Column("balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("source_reference", sa.String(length=255), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("envelope_cash_total", sa.Numeric(14, 2), nullable=True),
        sa.Column("calculated_envelope_total", sa.Numeric(14, 2), nullable=True),
        sa.Column("reconciliation_warning", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('manual_count','bank_statement','imported_balance',"
            "'card_statement','payment_provider')",
            name="ck_balance_confirmation_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('confirmed','provisional','unreconciled')",
            name="ck_balance_confirmation_status",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_balance_confirmation_confidence",
        ),
        sa.CheckConstraint(
            "envelope_cash_total IS NULL OR envelope_cash_total >= 0",
            name="ck_balance_confirmation_envelope_cash",
        ),
        sa.CheckConstraint(
            "calculated_envelope_total IS NULL OR calculated_envelope_total >= 0",
            name="ck_balance_confirmation_calculated_envelope",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_balance_confirmations_account_date",
        "balance_confirmations",
        ["account_id", "confirmed_at"],
    )
    _create_append_only_triggers()
    op.execute(
        """
        INSERT INTO balance_confirmations (
            account_id, confirmed_at, balance, currency, source_type,
            confidence, status, notes, created_at
        )
        SELECT
            id, created_at, balance, currency, 'manual_count',
            1, 'confirmed', 'Fiktiver Demo-Seed-Saldo', created_at
        FROM accounts
        WHERE name LIKE 'Demo-%'
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS prevent_balance_confirmations_delete")
    op.execute("DROP TRIGGER IF EXISTS prevent_balance_confirmations_update")
    op.drop_index("ix_balance_confirmations_account_date", table_name="balance_confirmations")
    op.drop_table("balance_confirmations")
