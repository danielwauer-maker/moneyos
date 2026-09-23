from pathlib import Path

from alembic.config import Config

from alembic import command
from app.config import get_settings


def upgrade_database(database_url: str | None = None) -> None:
    """Upgrade the configured database to the repository's migration head."""
    repository_root = Path(__file__).resolve().parents[2]
    config = Config(str(repository_root / "alembic.ini"))
    config.set_main_option("script_location", str(repository_root / "alembic"))
    config.attributes["database_url"] = database_url or get_settings().database_url
    command.upgrade(config, "head")
