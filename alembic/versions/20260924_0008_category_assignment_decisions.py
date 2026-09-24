"""Add independent category assignment decisions.

Revision ID: 20260924_0008
Revises: 20260923_0007
"""

import sqlalchemy as sa

from alembic import op

revision = "20260924_0008"
down_revision = "20260923_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "category_assignment_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_key", sa.String(length=80), nullable=False),
        sa.Column("source_transaction_id", sa.Integer(), nullable=True),
        sa.Column("economic_event_id", sa.Integer(), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("assignment_rule_id", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "source_transaction_id IS NOT NULL OR economic_event_id IS NOT NULL",
            name="ck_category_assignment_source",
        ),
        sa.ForeignKeyConstraint(["assignment_rule_id"], ["assignment_rules.id"]),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"]),
        sa.ForeignKeyConstraint(["economic_event_id"], ["economic_events.id"]),
        sa.ForeignKeyConstraint(["source_transaction_id"], ["source_transactions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_key"),
    )


def downgrade() -> None:
    op.drop_table("category_assignment_decisions")
