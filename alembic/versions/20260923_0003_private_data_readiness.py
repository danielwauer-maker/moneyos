"""Add private-import lifecycle metadata and backup history.

Revision ID: 20260923_0003
Revises: 20260923_0002
"""

import sqlalchemy as sa

from alembic import op

revision = "20260923_0003"
down_revision = "20260923_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("import_batches") as batch:
        batch.add_column(sa.Column("stored_filename", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("size_bytes", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("detected_mime", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("validation_json", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))

    op.create_table(
        "backup_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("backup_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("filename"),
    )

    allowed = "'pending','uploaded','validating','valid','quarantined','imported','failed'"
    for action in ("INSERT", "UPDATE"):
        op.execute(
            f"""
            CREATE TRIGGER validate_import_batch_status_{action.lower()}
            BEFORE {action} ON import_batches
            WHEN NEW.status NOT IN ({allowed})
            BEGIN
                SELECT RAISE(ABORT, 'invalid import batch status');
            END
            """
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS validate_import_batch_status_update")
    op.execute("DROP TRIGGER IF EXISTS validate_import_batch_status_insert")
    op.drop_table("backup_records")
    with op.batch_alter_table("import_batches") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("validation_json")
        batch.drop_column("detected_mime")
        batch.drop_column("size_bytes")
        batch.drop_column("stored_filename")
