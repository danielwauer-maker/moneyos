from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import get_settings
from app.db.session import SessionLocal
from app.services.backup import create_backup, restore_backup
from app.services.diagnostics import diagnostics_as_dicts
from app.services.import_staging import delete_staged_file
from app.services.retention import apply_retention, plan_retention


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MoneyOS local operations")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("backup", help="Create a timestamped local backup")
    restore = commands.add_parser("restore", help="Restore a verified backup")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--confirm", action="store_true")
    restore.add_argument("--restore-config", action="store_true")
    commands.add_parser("diagnostics", help="Report local operational health")
    retention = commands.add_parser("retention", help="Plan or apply configured retention")
    retention.add_argument("--apply", action="store_true")
    retention.add_argument("--confirm", action="store_true")
    delete = commands.add_parser("delete-staged", help="Delete one retained import file")
    delete.add_argument("batch_id", type=int)
    delete.add_argument("--confirm", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()
    settings.ensure_local_directories()
    if args.command == "backup":
        print(create_backup(settings.database_url, settings.backup_dir))
    elif args.command == "restore":
        result = restore_backup(
            settings.database_url,
            settings.backup_dir,
            args.backup,
            confirm=args.confirm,
            restore_config=args.restore_config,
        )
        print(f"restored={result.restored_from}\nsafety_backup={result.safety_backup}")
    elif args.command == "diagnostics":
        with SessionLocal() as db:
            print(json.dumps(diagnostics_as_dicts(db, settings), indent=2))
    elif args.command == "retention":
        candidates = plan_retention(settings)
        if args.apply:
            print(f"removed={apply_retention(candidates, confirm=args.confirm)}")
        else:
            for candidate in candidates:
                print(f"{candidate.category}: {candidate.path}")
    elif args.command == "delete-staged":
        with SessionLocal() as db:
            delete_staged_file(db, args.batch_id, settings, confirm=args.confirm)
            print(f"deleted staged file for batch {args.batch_id}")


if __name__ == "__main__":
    main()
