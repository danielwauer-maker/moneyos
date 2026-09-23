"""Add explicit account links for private master data backfill.

Revision ID: 20260923_0005
Revises: 20260923_0004
"""

import sqlalchemy as sa

from alembic import op

revision = "20260923_0005"
down_revision = "20260923_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(
            sa.Column(
                "balance_confirmed", sa.Boolean(), server_default=sa.text("1"), nullable=False
            )
        )

    with op.batch_alter_table("economic_events") as batch:
        batch.add_column(sa.Column("source_account_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("target_account_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_economic_events_source_account", "accounts", ["source_account_id"], ["id"]
        )
        batch.create_foreign_key(
            "fk_economic_events_target_account", "accounts", ["target_account_id"], ["id"]
        )

    op.create_table(
        "source_transaction_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_transaction_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "role IN ('source', 'target')", name="ck_source_transaction_account_role"
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["source_transaction_id"], ["source_transactions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_transaction_id", "role"),
    )


def downgrade() -> None:
    op.drop_table("source_transaction_accounts")
    with op.batch_alter_table("economic_events") as batch:
        batch.drop_constraint("fk_economic_events_target_account", type_="foreignkey")
        batch.drop_constraint("fk_economic_events_source_account", type_="foreignkey")
        batch.drop_column("target_account_id")
        batch.drop_column("source_account_id")
    with op.batch_alter_table("accounts") as batch:
        batch.drop_column("balance_confirmed")
