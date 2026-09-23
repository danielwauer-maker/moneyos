"""Create the frozen MoneyOS Phase-1 schema.

Revision ID: 20260923_0001
Revises: None
"""

import sqlalchemy as sa

from alembic import op

revision = "20260923_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("account_type", sa.String(30), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_liability", sa.Boolean(), nullable=False),
        sa.Column("parent_account_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["parent_account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["parent_id"], ["categories.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "envelopes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("target_rule_type", sa.String(40), nullable=False),
        sa.Column("target_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "target_amount IS NULL OR target_amount >= 0", name="ck_envelopes_target"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("imported_at", sa.DateTime(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_hash"),
    )
    op.create_table(
        "merchants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("canonical_name", sa.String(160), nullable=False),
        sa.Column("merchant_type", sa.String(60), nullable=True),
        sa.Column("aliases_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_name"),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(140), nullable=False),
        sa.Column("starts_at", sa.Date(), nullable=True),
        sa.Column("ends_at", sa.Date(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "economic_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("merchant_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("envelope_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("is_manual", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("amount >= 0", name="ck_economic_event_amount"),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_economic_event_confidence",
        ),
        sa.CheckConstraint(
            "event_type IN ('expense', 'income', 'transfer', 'refund')",
            name="ck_economic_event_type",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"]),
        sa.ForeignKeyConstraint(["envelope_id"], ["envelopes.id"]),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "envelope_rule_periods",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("envelope_id", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("monthly_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("rule_type", sa.String(40), nullable=False),
        sa.Column("target_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="ck_rule_period_dates"
        ),
        sa.CheckConstraint(
            "monthly_amount IS NULL OR monthly_amount >= 0", name="ck_rule_period_monthly"
        ),
        sa.CheckConstraint(
            "target_amount IS NULL OR target_amount >= 0", name="ck_rule_period_target"
        ),
        sa.ForeignKeyConstraint(["envelope_id"], ["envelopes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "envelope_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("envelope_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("physical_balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("is_confirmed", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint("physical_balance >= 0", name="ck_snapshot_physical_balance"),
        sa.ForeignKeyConstraint(["envelope_id"], ["envelopes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("envelope_id", "snapshot_date"),
    )
    op.create_table(
        "forecast_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("forecast_date", sa.Date(), nullable=False),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("direction", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("probability", sa.Numeric(5, 4), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint("amount >= 0", name="ck_forecast_amount"),
        sa.CheckConstraint(
            "probability IS NULL OR (probability >= 0 AND probability <= 1)",
            name="ck_forecast_probability",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "raw_import_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("import_batch_id", sa.Integer(), nullable=False),
        sa.Column("source_row_key", sa.String(255), nullable=False),
        sa.Column("raw_payload_json", sa.JSON(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("raw_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["import_batch_id"], ["import_batches.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("import_batch_id", "source_row_key"),
        sa.UniqueConstraint("raw_hash"),
    )
    op.create_table(
        "reconciliation_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("vault_total", sa.Numeric(14, 2), nullable=True),
        sa.Column("envelope_total", sa.Numeric(14, 2), nullable=True),
        sa.Column("free_vault_cash_before", sa.Numeric(14, 2), nullable=True),
        sa.Column("deposit_total", sa.Numeric(14, 2), nullable=True),
        sa.Column("withdraw_total", sa.Numeric(14, 2), nullable=True),
        sa.Column("transfer_money", sa.Numeric(14, 2), nullable=True),
        sa.Column("free_vault_cash_used", sa.Numeric(14, 2), nullable=True),
        sa.Column("bank_withdrawal_needed", sa.Numeric(14, 2), nullable=True),
        sa.Column("free_vault_cash_after", sa.Numeric(14, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "recurring_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(140), nullable=False),
        sa.Column("direction", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("frequency", sa.String(30), nullable=False),
        sa.Column("next_due_at", sa.Date(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("envelope_id", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("amount >= 0", name="ck_recurring_item_amount"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"]),
        sa.ForeignKeyConstraint(["envelope_id"], ["envelopes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "envelope_movements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("envelope_id", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("movement_type", sa.String(40), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("economic_event_id", sa.Integer(), nullable=True),
        sa.Column("source_account_id", sa.Integer(), nullable=True),
        sa.Column("target_account_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint("amount >= 0", name="ck_envelope_movement_amount"),
        sa.ForeignKeyConstraint(["economic_event_id"], ["economic_events.id"]),
        sa.ForeignKeyConstraint(["envelope_id"], ["envelopes.id"]),
        sa.ForeignKeyConstraint(["source_account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["target_account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "source_transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("import_batch_id", sa.Integer(), nullable=True),
        sa.Column("raw_record_id", sa.Integer(), nullable=True),
        sa.Column("source_system", sa.String(30), nullable=False),
        sa.Column("source_transaction_id", sa.String(255), nullable=False),
        sa.Column("related_source_transaction_id", sa.String(255), nullable=True),
        sa.Column("booked_at", sa.DateTime(), nullable=False),
        sa.Column("value_at", sa.DateTime(), nullable=True),
        sa.Column("merchant_raw", sa.String(255), nullable=True),
        sa.Column("description_raw", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("source_account_hint", sa.String(100), nullable=True),
        sa.Column("balance_after", sa.Numeric(14, 2), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["import_batch_id"], ["import_batches.id"]),
        sa.ForeignKeyConstraint(["raw_record_id"], ["raw_import_records.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint"),
        sa.UniqueConstraint("source_system", "source_transaction_id"),
    )
    op.create_table(
        "event_source_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("economic_event_id", sa.Integer(), nullable=False),
        sa.Column("source_transaction_id", sa.Integer(), nullable=False),
        sa.Column("link_type", sa.String(40), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "link_type IN ('canonical_source', 'funding_leg', 'settlement_leg', "
            "'enrichment', 'refund_origin', 'duplicate_source', 'authorization', "
            "'third_party_payment')",
            name="ck_event_source_link_type",
        ),
        sa.ForeignKeyConstraint(["economic_event_id"], ["economic_events.id"]),
        sa.ForeignKeyConstraint(["source_transaction_id"], ["source_transactions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("economic_event_id", "source_transaction_id", "link_type"),
    )
    op.create_index(
        "uq_event_source_links_canonical_source",
        "event_source_links",
        ["source_transaction_id"],
        unique=True,
        sqlite_where=sa.text("link_type = 'canonical_source'"),
    )
    op.create_table(
        "review_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("economic_event_id", sa.Integer(), nullable=True),
        sa.Column("source_transaction_id", sa.Integer(), nullable=True),
        sa.Column("review_type", sa.String(40), nullable=False),
        sa.Column("proposed_category_id", sa.Integer(), nullable=True),
        sa.Column("proposed_envelope_id", sa.Integer(), nullable=True),
        sa.Column("proposed_project_id", sa.Integer(), nullable=True),
        sa.Column("proposed_event_type", sa.String(20), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decision_notes", sa.Text(), nullable=True),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_review_confidence"),
        sa.ForeignKeyConstraint(["economic_event_id"], ["economic_events.id"]),
        sa.ForeignKeyConstraint(["proposed_category_id"], ["categories.id"]),
        sa.ForeignKeyConstraint(["proposed_envelope_id"], ["envelopes.id"]),
        sa.ForeignKeyConstraint(["proposed_project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["source_transaction_id"], ["source_transactions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "assignment_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_type", sa.String(40), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("condition_json", sa.JSON(), nullable=False),
        sa.Column("action_json", sa.JSON(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("created_from_review_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_from_review_id"], ["review_items.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("assignment_rules")
    op.drop_table("review_items")
    op.drop_index("uq_event_source_links_canonical_source", table_name="event_source_links")
    op.drop_table("event_source_links")
    op.drop_table("source_transactions")
    op.drop_table("envelope_movements")
    op.drop_table("recurring_items")
    op.drop_table("reconciliation_runs")
    op.drop_table("raw_import_records")
    op.drop_table("forecast_entries")
    op.drop_table("envelope_snapshots")
    op.drop_table("envelope_rule_periods")
    op.drop_table("economic_events")
    op.drop_table("projects")
    op.drop_table("merchants")
    op.drop_table("import_batches")
    op.drop_table("envelopes")
    op.drop_table("categories")
    op.drop_table("accounts")
