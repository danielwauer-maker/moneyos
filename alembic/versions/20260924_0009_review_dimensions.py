"""Add independent project and economic-type review decisions.

Revision ID: 20260924_0009
Revises: 20260924_0008
"""

import sqlalchemy as sa

from alembic import op

revision = "20260924_0009"
down_revision = "20260924_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_assignment_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_key", sa.String(length=80), nullable=False),
        sa.Column("source_transaction_id", sa.Integer(), nullable=True),
        sa.Column("economic_event_id", sa.Integer(), nullable=True),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("assignment_rule_id", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('assigned','no_project','later')", name="ck_project_assignment_decision"
        ),
        sa.CheckConstraint(
            "(decision = 'assigned' AND project_id IS NOT NULL) OR "
            "(decision != 'assigned' AND project_id IS NULL)",
            name="ck_project_assignment_target",
        ),
        sa.CheckConstraint(
            "source_transaction_id IS NOT NULL OR economic_event_id IS NOT NULL",
            name="ck_project_assignment_source",
        ),
        sa.ForeignKeyConstraint(["assignment_rule_id"], ["assignment_rules.id"]),
        sa.ForeignKeyConstraint(["economic_event_id"], ["economic_events.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["source_transaction_id"], ["source_transactions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_key"),
    )
    op.create_table(
        "economic_type_assignment_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_key", sa.String(length=80), nullable=False),
        sa.Column("source_transaction_id", sa.Integer(), nullable=True),
        sa.Column("economic_event_id", sa.Integer(), nullable=True),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('expense','income','transfer','refund','later')",
            name="ck_type_assignment_decision",
        ),
        sa.CheckConstraint(
            "source_transaction_id IS NOT NULL OR economic_event_id IS NOT NULL",
            name="ck_type_assignment_source",
        ),
        sa.ForeignKeyConstraint(["economic_event_id"], ["economic_events.id"]),
        sa.ForeignKeyConstraint(["source_transaction_id"], ["source_transactions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_key"),
    )


def downgrade() -> None:
    op.drop_table("economic_type_assignment_decisions")
    op.drop_table("project_assignment_decisions")
