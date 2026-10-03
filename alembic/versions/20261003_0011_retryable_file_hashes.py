"""Allow retry batches while retaining successful file-hash protection.

Revision ID: 20261003_0011
Revises: 20261003_0010
"""

import sqlalchemy as sa

from alembic import op

revision = "20261003_0011"
down_revision = "20261003_0010"
branch_labels = None
depends_on = None

NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
ALLOWED_STATUSES = "'pending','uploaded','validating','valid','quarantined','imported','failed'"


def _create_status_triggers() -> None:
    for action in ("INSERT", "UPDATE"):
        op.execute(
            f"""
            CREATE TRIGGER validate_import_batch_status_{action.lower()}
            BEFORE {action} ON import_batches
            WHEN NEW.status NOT IN ({ALLOWED_STATUSES})
            BEGIN
                SELECT RAISE(ABORT, 'invalid import batch status');
            END
            """
        )


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS validate_import_batch_status_update")
    op.execute("DROP TRIGGER IF EXISTS validate_import_batch_status_insert")
    with op.batch_alter_table(
        "import_batches",
        naming_convention=NAMING_CONVENTION,
        recreate="always",
    ) as batch:
        batch.drop_constraint("uq_import_batches_source_hash", type_="unique")
    op.create_index(
        "uq_import_batches_imported_hash",
        "import_batches",
        ["source_hash"],
        unique=True,
        sqlite_where=sa.text("status = 'imported'"),
    )
    op.create_index(
        "uq_import_batches_active_hash_source",
        "import_batches",
        ["source_hash", "source_type"],
        unique=True,
        sqlite_where=sa.text("status IN ('pending','uploaded','validating','valid')"),
    )
    _create_status_triggers()


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS validate_import_batch_status_update")
    op.execute("DROP TRIGGER IF EXISTS validate_import_batch_status_insert")
    op.drop_index("uq_import_batches_active_hash_source", table_name="import_batches")
    op.drop_index("uq_import_batches_imported_hash", table_name="import_batches")
    with op.batch_alter_table(
        "import_batches",
        naming_convention=NAMING_CONVENTION,
        recreate="always",
    ) as batch:
        batch.create_unique_constraint("uq_import_batches_source_hash", ["source_hash"])
    _create_status_triggers()
