"""Repair missing source identity guards in legacy local databases.

Revision ID: 20260923_0004
Revises: 20260923_0003
"""

import sqlalchemy as sa

from alembic import op

revision = "20260923_0004"
down_revision = "20260923_0003"
branch_labels = None
depends_on = None


def _has_unique(table: str, columns: set[str]) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        set(constraint["column_names"]) == columns
        for constraint in inspector.get_unique_constraints(table)
    )


def _create_source_append_only_triggers() -> None:
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS prevent_source_transactions_{action.lower()}
            BEFORE {action} ON source_transactions
            BEGIN
                SELECT RAISE(ABORT, 'source_transactions is append-only');
            END
            """
        )


def upgrade() -> None:
    source_identity = {"source_system", "source_transaction_id"}
    if not _has_unique("source_transactions", source_identity):
        op.execute("DROP TRIGGER IF EXISTS prevent_source_transactions_update")
        op.execute("DROP TRIGGER IF EXISTS prevent_source_transactions_delete")
        with op.batch_alter_table("source_transactions") as batch:
            batch.create_unique_constraint(
                "uq_source_transactions_source_identity",
                ["source_system", "source_transaction_id"],
            )
        _create_source_append_only_triggers()

    link_identity = {"economic_event_id", "source_transaction_id", "link_type"}
    if not _has_unique("event_source_links", link_identity):
        with op.batch_alter_table("event_source_links") as batch:
            batch.create_unique_constraint(
                "uq_event_source_links_event_source_type",
                ["economic_event_id", "source_transaction_id", "link_type"],
            )

    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("event_source_links")}
    if "uq_event_source_links_canonical_source" not in indexes:
        op.create_index(
            "uq_event_source_links_canonical_source",
            "event_source_links",
            ["source_transaction_id"],
            unique=True,
            sqlite_where=sa.text("link_type = 'canonical_source'"),
        )


def downgrade() -> None:
    # These guards have belonged to the canonical schema since revision 0001.
    # Removing a repaired guard would recreate the unsafe legacy drift.
    pass
