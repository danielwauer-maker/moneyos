from __future__ import annotations

import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    ok: bool
    detail: str


def _directory_check(name: str, path: Path) -> DiagnosticCheck:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path):
            pass
        return DiagnosticCheck(name, True, "schreibbar")
    except OSError:
        return DiagnosticCheck(name, False, "nicht schreibbar")


def _migration_head() -> str:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head() or "unknown"


def run_diagnostics(db: Session, settings: Settings) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    try:
        db.execute(text("SELECT 1"))
        checks.append(DiagnosticCheck("database", True, "erreichbar"))
    except Exception:
        checks.append(DiagnosticCheck("database", False, "nicht erreichbar"))

    try:
        current = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        head = _migration_head()
        checks.append(DiagnosticCheck("schema", current == head, f"{current} / {head}"))
    except Exception:
        checks.append(DiagnosticCheck("schema", False, "Migrationsstand nicht lesbar"))

    checks.append(_directory_check("backup_directory", settings.backup_dir))
    checks.append(_directory_check("staging_directory", settings.staging_dir))
    try:
        free = shutil.disk_usage(settings.private_data_dir.resolve().anchor).free
        checks.append(DiagnosticCheck("free_disk_space", True, f"{free // (1024**3)} GiB frei"))
    except OSError:
        checks.append(DiagnosticCheck("free_disk_space", False, "nicht ermittelbar"))

    backups = sorted(settings.backup_dir.glob("moneyos-*.zip"), key=lambda p: p.stat().st_mtime)
    detail = backups[-1].name if backups else "noch kein erfolgreiches Backup"
    checks.append(DiagnosticCheck("last_successful_backup", bool(backups), detail))
    return checks


def diagnostics_as_dicts(db: Session, settings: Settings) -> list[dict[str, object]]:
    return [asdict(check) for check in run_diagnostics(db, settings)]
