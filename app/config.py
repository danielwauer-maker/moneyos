from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    app_name: str = "MoneyOS"
    database_url: str = "sqlite:///./data/moneyos.db"
    private_database_url: str = "sqlite:///./data/private/profiles/private/moneyos.db"
    demo_mode: bool = True
    private_data_dir: Path = Path("data/private")
    backup_dir: Path = Path("backups")
    log_dir: Path = Path("logs")
    max_import_file_size_bytes: int = 25 * 1024 * 1024
    staged_import_retention_days: int = 90
    quarantine_retention_days: int = 365
    log_retention_days: int = 30
    backup_retention_days: int = 365
    minimum_backups_to_keep: int = 3

    model_config = SettingsConfigDict(env_file=".env", env_prefix="MONEYOS_", extra="ignore")

    def ensure_local_directories(self) -> None:
        database = make_url(self.active_database_url).database
        if self.active_database_url.startswith("sqlite") and database and database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        for directory in (
            self.staging_dir,
            self.quarantine_dir,
            self.active_backup_dir,
            self.active_log_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def profile_name(self) -> str:
        return "demo" if self.demo_mode else "private"

    @property
    def active_database_url(self) -> str:
        return self.database_url if self.demo_mode else self.private_database_url

    @property
    def profile_private_data_dir(self) -> Path:
        if self.demo_mode:
            return self.private_data_dir
        return self.private_data_dir / "profiles" / "private"

    @property
    def active_backup_dir(self) -> Path:
        return self.backup_dir if self.demo_mode else self.backup_dir / "private"

    @property
    def active_log_dir(self) -> Path:
        return self.log_dir if self.demo_mode else self.log_dir / "private"

    @property
    def staging_dir(self) -> Path:
        return self.profile_private_data_dir / "imports" / "staging"

    @property
    def quarantine_dir(self) -> Path:
        return self.profile_private_data_dir / "imports" / "quarantine"


@lru_cache
def get_settings() -> Settings:
    return Settings()
