from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from sqlalchemy.engine import make_url


@dataclass(frozen=True)
class RestoreResult:
    restored_from: Path
    safety_backup: Path


def sqlite_database_path(database_url: str) -> Path:
    url = make_url(database_url)
    if url.drivername != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("Local backup currently supports file-based SQLite only")
    return Path(url.database).resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_separate_backup_directory(database_path: Path, backup_dir: Path) -> None:
    resolved = backup_dir.resolve()
    if resolved == database_path.parent.resolve() or resolved.is_relative_to(
        database_path.parent.resolve()
    ):
        raise ValueError("Backup directory must be outside the active database directory")


def create_backup(
    database_url: str,
    backup_dir: Path,
    *,
    config_path: Path = Path(".env"),
    backup_type: str = "regular",
    now: datetime | None = None,
) -> Path:
    database_path = sqlite_database_path(database_url)
    if not database_path.is_file():
        raise FileNotFoundError("Active MoneyOS database does not exist")
    backup_dir.mkdir(parents=True, exist_ok=True)
    _assert_separate_backup_directory(database_path, backup_dir)
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S-%f")
    destination = backup_dir.resolve() / f"moneyos-{timestamp}-{backup_type}.zip"

    with tempfile.TemporaryDirectory(dir=backup_dir) as temp_dir_name:
        temp_db = Path(temp_dir_name) / "moneyos.db"
        with (
            closing(sqlite3.connect(database_path)) as source,
            closing(sqlite3.connect(temp_db)) as target,
        ):
            source.backup(target)
        db_checksum = _sha256(temp_db)
        manifest = {
            "format_version": 1,
            "created_at": (now or datetime.now(UTC)).isoformat(),
            "backup_type": backup_type,
            "database_sha256": db_checksum,
            "config_included": config_path.is_file(),
        }
        temp_archive = Path(temp_dir_name) / "backup.zip"
        with zipfile.ZipFile(temp_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(temp_db, "database/moneyos.db")
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            if config_path.is_file():
                archive.write(config_path, "config/.env")
        os.replace(temp_archive, destination)
    return destination


def _validated_archive_database(archive_path: Path, extract_dir: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        allowed = {"manifest.json", "database/moneyos.db", "config/.env"}
        names = set(archive.namelist())
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or name not in allowed:
                raise ValueError("Backup contains an unexpected or unsafe entry")
        if not {"manifest.json", "database/moneyos.db"}.issubset(names):
            raise ValueError("Backup is incomplete")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format_version") != 1:
            raise ValueError("Unsupported backup format")
        archive.extract("database/moneyos.db", extract_dir)
        extracted = extract_dir / "database" / "moneyos.db"
        if _sha256(extracted) != manifest.get("database_sha256"):
            raise ValueError("Backup database checksum mismatch")
        with closing(sqlite3.connect(extracted)) as connection:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("Backup database failed SQLite integrity validation")
        return extracted


def restore_backup(
    database_url: str,
    backup_dir: Path,
    archive_path: Path,
    *,
    confirm: bool,
    config_path: Path = Path(".env"),
    restore_config: bool = False,
) -> RestoreResult:
    if not confirm:
        raise ValueError("Restore requires explicit confirmation")
    database_path = sqlite_database_path(database_url)
    archive_path = archive_path.resolve()
    if not archive_path.is_file():
        raise FileNotFoundError("Backup archive does not exist")
    safety = create_backup(
        database_url,
        backup_dir,
        config_path=config_path,
        backup_type="safety",
    )
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=database_path.parent) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        extracted = _validated_archive_database(archive_path, temp_dir)
        replacement = database_path.parent / f".{database_path.name}.restore"
        replacement.write_bytes(extracted.read_bytes())
        os.replace(replacement, database_path)
        if restore_config:
            with zipfile.ZipFile(archive_path) as archive:
                if "config/.env" in archive.namelist():
                    config_temp = config_path.with_name(f".{config_path.name}.restore")
                    config_temp.write_bytes(archive.read("config/.env"))
                    os.replace(config_temp, config_path)
    return RestoreResult(restored_from=archive_path, safety_backup=safety)
