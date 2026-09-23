"""Add explicit envelope assignment decisions.

Revision ID: 20260923_0007
Revises: 20260923_0006
"""

import sqlalchemy as sa

from alembic import op

revision = "20260923_0007"
down_revision = "20260923_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "envelope_assignment_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_key", sa.String(length=80), nullable=False),
        sa.Column("source_transaction_id", sa.Integer(), nullable=True),
        sa.Column("economic_event_id", sa.Integer(), nullable=True),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("envelope_id", sa.Integer(), nullable=True),
        sa.Column("assignment_rule_id", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('assigned','no_envelope','later')",
            name="ck_envelope_assignment_decision",
        ),
        sa.CheckConstraint(
            "(decision = 'assigned' AND envelope_id IS NOT NULL) OR "
            "(decision != 'assigned' AND envelope_id IS NULL)",
            name="ck_envelope_assignment_target",
        ),
        sa.CheckConstraint(
            "source_transaction_id IS NOT NULL OR economic_event_id IS NOT NULL",
            name="ck_envelope_assignment_source",
        ),
        sa.ForeignKeyConstraint(["assignment_rule_id"], ["assignment_rules.id"]),
        sa.ForeignKeyConstraint(["economic_event_id"], ["economic_events.id"]),
        sa.ForeignKeyConstraint(["envelope_id"], ["envelopes.id"]),
        sa.ForeignKeyConstraint(["source_transaction_id"], ["source_transactions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_key"),
    )
    op.create_index(
        "ix_envelope_assignment_decisions_state",
        "envelope_assignment_decisions",
        ["decision"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_envelope_assignment_decisions_state",
        table_name="envelope_assignment_decisions",
    )
    op.drop_table("envelope_assignment_decisions")
