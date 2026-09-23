"""Add database-level append-only and historical-period guards.

Revision ID: 20260923_0002
Revises: 20260923_0001
"""

from alembic import op

revision = "20260923_0002"
down_revision = "20260923_0001"
branch_labels = None
depends_on = None


IMMUTABLE_TABLES = (
    "raw_import_records",
    "source_transactions",
    "envelope_rule_periods",
)


def upgrade() -> None:
    for table in IMMUTABLE_TABLES:
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

    op.execute(
        """
        CREATE TRIGGER prevent_envelope_rule_period_overlap
        BEFORE INSERT ON envelope_rule_periods
        WHEN EXISTS (
            SELECT 1
            FROM envelope_rule_periods existing
            WHERE existing.envelope_id = NEW.envelope_id
              AND existing.rule_type = NEW.rule_type
              AND existing.valid_from <= COALESCE(NEW.valid_to, '9999-12-31')
              AND COALESCE(existing.valid_to, '9999-12-31') >= NEW.valid_from
        )
        BEGIN
            SELECT RAISE(ABORT, 'envelope rule periods must not overlap');
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS prevent_envelope_rule_period_overlap")
    for table in reversed(IMMUTABLE_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS prevent_{table}_delete")
        op.execute(f"DROP TRIGGER IF EXISTS prevent_{table}_update")
