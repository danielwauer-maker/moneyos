from pathlib import Path

from app.config import Settings


def test_demo_and_private_profiles_use_disjoint_storage(tmp_path: Path) -> None:
    common = {
        "database_url": f"sqlite:///{(tmp_path / 'demo.db').as_posix()}",
        "private_database_url": f"sqlite:///{(tmp_path / 'private' / 'moneyos.db').as_posix()}",
        "private_data_dir": tmp_path / "data" / "private",
        "backup_dir": tmp_path / "backups",
        "log_dir": tmp_path / "logs",
    }
    demo = Settings(demo_mode=True, **common)
    private = Settings(demo_mode=False, **common)

    assert demo.active_database_url != private.active_database_url
    assert demo.staging_dir != private.staging_dir
    assert demo.quarantine_dir != private.quarantine_dir
    assert demo.active_backup_dir != private.active_backup_dir
    assert demo.active_log_dir != private.active_log_dir
    assert private.profile_name == "private"
    assert (
        private.profile_private_data_dir == tmp_path / "data" / "private" / "profiles" / "private"
    )


def test_private_profile_creates_only_its_own_operational_directories(tmp_path: Path) -> None:
    private_database = tmp_path / "data" / "private" / "profiles" / "private" / "moneyos.db"
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'data' / 'moneyos.db').as_posix()}",
        private_database_url=f"sqlite:///{private_database.as_posix()}",
        demo_mode=False,
        private_data_dir=tmp_path / "data" / "private",
        backup_dir=tmp_path / "backups",
        log_dir=tmp_path / "logs",
    )

    settings.ensure_local_directories()

    assert private_database.parent.is_dir()
    assert settings.staging_dir.is_dir()
    assert settings.quarantine_dir.is_dir()
    assert settings.active_backup_dir.is_dir()
    assert settings.active_log_dir.is_dir()
    assert not (tmp_path / "data" / "moneyos.db").exists()
