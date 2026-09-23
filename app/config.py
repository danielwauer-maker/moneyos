from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "MoneyOS"
    database_url: str = "sqlite:///./data/moneyos.db"
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
        if self.database_url.startswith("sqlite:///./"):
            Path(self.database_url.removeprefix("sqlite:///./")).parent.mkdir(
                parents=True, exist_ok=True
            )
        for directory in (
            self.staging_dir,
            self.quarantine_dir,
            self.backup_dir,
            self.log_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def staging_dir(self) -> Path:
        return self.private_data_dir / "imports" / "staging"

    @property
    def quarantine_dir(self) -> Path:
        return self.private_data_dir / "imports" / "quarantine"


@lru_cache
def get_settings() -> Settings:
    return Settings()
