from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import Settings


@dataclass(frozen=True)
class RetentionCandidate:
    category: str
    path: Path
    root: Path


def _old_files(root: Path, days: int, now: datetime) -> list[Path]:
    if not root.exists():
        return []
    cutoff = now - timedelta(days=days)
    return [
        path
        for path in root.iterdir()
        if path.is_file() and datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff
    ]


def plan_retention(settings: Settings, now: datetime | None = None) -> list[RetentionCandidate]:
    now = now or datetime.now(UTC)
    candidates: list[RetentionCandidate] = []
    categories = (
        ("staged", settings.staging_dir, settings.staged_import_retention_days),
        ("quarantine", settings.quarantine_dir, settings.quarantine_retention_days),
        ("log", settings.active_log_dir, settings.log_retention_days),
    )
    for category, root, days in categories:
        candidates.extend(
            RetentionCandidate(category, path, root) for path in _old_files(root, days, now)
        )

    backups = sorted(
        (path for path in settings.active_backup_dir.glob("moneyos-*.zip") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    protected_count = max(1, settings.minimum_backups_to_keep)
    eligible = backups[protected_count:]
    old_eligible = set(_old_files(settings.active_backup_dir, settings.backup_retention_days, now))
    candidates.extend(
        RetentionCandidate("backup", path, settings.active_backup_dir)
        for path in eligible
        if path in old_eligible
    )
    return candidates


def apply_retention(candidates: list[RetentionCandidate], *, confirm: bool) -> int:
    if not confirm:
        raise ValueError("Retention deletion requires explicit confirmation")
    removed = 0
    for candidate in candidates:
        path = candidate.path.resolve()
        if not path.is_relative_to(candidate.root.resolve()):
            raise ValueError("Retention candidate escapes its configured directory")
        if path.is_file():
            path.unlink()
            removed += 1
    return removed
