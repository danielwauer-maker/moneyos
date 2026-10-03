"""Add cross-batch identity and Amazon enrichment records.

Revision ID: 20261003_0010
Revises: 20260924_0009
"""

import sqlalchemy as sa

from alembic import op

revision = "20261003_0010"
down_revision = "20260924_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "source_transactions",
        sa.Column("source_natural_key", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "uq_source_transactions_natural_key",
        "source_transactions",
        ["source_system", "source_natural_key"],
        unique=True,
        sqlite_where=sa.text("source_natural_key IS NOT NULL"),
    )
    op.create_table(
        "amazon_enrichment_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("import_batch_id", sa.Integer(), nullable=False),
        sa.Column("raw_record_id", sa.Integer(), nullable=False),
        sa.Column("record_type", sa.String(length=30), nullable=False),
        sa.Column("natural_key", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("order_key", sa.String(length=64), nullable=True),
        sa.Column("item_key", sa.String(length=64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("product_title", sa.Text(), nullable=True),
        sa.Column("asin", sa.String(length=20), nullable=True),
        sa.Column("payment_method", sa.String(length=255), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "record_type IN ('order_item','digital_order_item','refund','return','replacement')",
            name="ck_amazon_enrichment_record_type",
        ),
        sa.ForeignKeyConstraint(["import_batch_id"], ["import_batches.id"]),
        sa.ForeignKeyConstraint(["raw_record_id"], ["raw_import_records.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("raw_record_id"),
        sa.UniqueConstraint("record_type", "natural_key"),
    )
    op.create_index(
        "ix_amazon_enrichment_records_order_key",
        "amazon_enrichment_records",
        ["order_key"],
    )
    op.create_index(
        "ix_amazon_enrichment_records_item_key",
        "amazon_enrichment_records",
        ["item_key"],
    )
    op.create_table(
        "amazon_payment_matches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("amazon_record_id", sa.Integer(), nullable=False),
        sa.Column("economic_event_id", sa.Integer(), nullable=False),
        sa.Column("match_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "match_type IN ('payment','refund')", name="ck_amazon_payment_match_type"
        ),
        sa.CheckConstraint("status IN ('linked','review')", name="ck_amazon_payment_match_status"),
        sa.ForeignKeyConstraint(["amazon_record_id"], ["amazon_enrichment_records.id"]),
        sa.ForeignKeyConstraint(["economic_event_id"], ["economic_events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("amazon_record_id", "economic_event_id", "match_type"),
    )
    op.create_table(
        "import_conflicts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("import_batch_id", sa.Integer(), nullable=False),
        sa.Column("source_system", sa.String(length=30), nullable=False),
        sa.Column("natural_key", sa.String(length=64), nullable=False),
        sa.Column("incoming_content_hash", sa.String(length=64), nullable=False),
        sa.Column("existing_source_transaction_id", sa.Integer(), nullable=True),
        sa.Column("existing_amazon_record_id", sa.Integer(), nullable=True),
        sa.Column("differing_fields_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('open','resolved','ignored')", name="ck_import_conflict_status"
        ),
        sa.ForeignKeyConstraint(["existing_amazon_record_id"], ["amazon_enrichment_records.id"]),
        sa.ForeignKeyConstraint(["existing_source_transaction_id"], ["source_transactions.id"]),
        sa.ForeignKeyConstraint(["import_batch_id"], ["import_batches.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "import_batch_id", "source_system", "natural_key", "incoming_content_hash"
        ),
    )
    for table in ("amazon_enrichment_records",):
        op.execute(
            f"""
            CREATE TRIGGER prevent_{table}_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} is append-only');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER prevent_{table}_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} is append-only');
            END
            """
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS prevent_amazon_enrichment_records_delete")
    op.execute("DROP TRIGGER IF EXISTS prevent_amazon_enrichment_records_update")
    op.drop_table("import_conflicts")
    op.drop_table("amazon_payment_matches")
    op.drop_index("ix_amazon_enrichment_records_item_key", table_name="amazon_enrichment_records")
    op.drop_index("ix_amazon_enrichment_records_order_key", table_name="amazon_enrichment_records")
    op.drop_table("amazon_enrichment_records")
    op.drop_index("uq_source_transactions_natural_key", table_name="source_transactions")
    op.drop_column("source_transactions", "source_natural_key")
