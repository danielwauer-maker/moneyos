from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import get_settings
from app.db.session import SessionLocal
from app.importers.amazon import dry_run_amazon_file
from app.importers.amex import dry_run_amex_file
from app.importers.paypal import dry_run_paypal_file
from app.services.backup import create_backup, restore_backup
from app.services.balance_confirmations import backfill_sparda_imported_balance
from app.services.diagnostics import diagnostics_as_dicts
from app.services.import_staging import delete_staged_file
from app.services.private_profile import initialize_private_profile
from app.services.retention import apply_retention, plan_retention
from app.services.sparda_reclassification import (
    apply_sparda_reclassification,
    audit_sparda_transaction_details,
    plan_sparda_reclassification,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MoneyOS local operations")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("backup", help="Create a timestamped local backup")
    restore = commands.add_parser("restore", help="Restore a verified backup")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--confirm", action="store_true")
    restore.add_argument("--restore-config", action="store_true")
    commands.add_parser("diagnostics", help="Report local operational health")
    initialize = commands.add_parser(
        "init-private", help="Initialize private accounts/envelopes and backfill Sparda links"
    )
    initialize.add_argument("--confirm", action="store_true")
    balance_history = commands.add_parser(
        "init-balance-history", help="Backfill the imported Sparda balance confirmation"
    )
    balance_history.add_argument("--confirm", action="store_true")
    retention = commands.add_parser("retention", help="Plan or apply configured retention")
    retention.add_argument("--apply", action="store_true")
    retention.add_argument("--confirm", action="store_true")
    delete = commands.add_parser("delete-staged", help="Delete one retained import file")
    delete.add_argument("batch_id", type=int)
    delete.add_argument("--confirm", action="store_true")
    reclassify = commands.add_parser(
        "reclassify-sparda",
        help="Dry-run or apply safe Sparda merchant/category reclassification",
    )
    reclassify.add_argument("--apply", action="store_true")
    reclassify.add_argument("--confirm", action="store_true")
    commands.add_parser(
        "audit-sparda-details",
        help="Read-only audit of Sparda secondary details and category suggestions",
    )
    paypal_audit = commands.add_parser(
        "audit-paypal",
        help="Read-only structural and matching audit of a local PayPal CSV",
    )
    paypal_audit.add_argument("file", type=Path)
    amex_audit = commands.add_parser(
        "audit-amex",
        help="Read-only structural and settlement audit of a local Amex CSV",
    )
    amex_audit.add_argument("file", type=Path)
    amazon_audit = commands.add_parser(
        "audit-amazon",
        help="Read-only structural, duplicate and payment-match audit of an Amazon ZIP",
    )
    amazon_audit.add_argument("file", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()
    settings.ensure_local_directories()
    if args.command == "backup":
        print(create_backup(settings.active_database_url, settings.active_backup_dir))
    elif args.command == "restore":
        result = restore_backup(
            settings.active_database_url,
            settings.active_backup_dir,
            args.backup,
            confirm=args.confirm,
            restore_config=args.restore_config,
        )
        print(f"restored={result.restored_from}\nsafety_backup={result.safety_backup}")
    elif args.command == "diagnostics":
        with SessionLocal() as db:
            print(json.dumps(diagnostics_as_dicts(db, settings), indent=2))
    elif args.command == "init-private":
        if settings.demo_mode:
            raise ValueError("init-private requires MONEYOS_DEMO_MODE=false")
        if not args.confirm:
            raise ValueError("init-private requires --confirm")
        backup = create_backup(
            settings.active_database_url,
            settings.active_backup_dir,
            backup_type="safety",
        )
        with SessionLocal.begin() as db:
            result = initialize_private_profile(db, settings)
        print(f"safety_backup={backup}")
        print(json.dumps(result.__dict__, indent=2))
    elif args.command == "init-balance-history":
        if settings.demo_mode:
            raise ValueError("init-balance-history requires MONEYOS_DEMO_MODE=false")
        if not args.confirm:
            raise ValueError("init-balance-history requires --confirm")
        backup = create_backup(
            settings.active_database_url,
            settings.active_backup_dir,
            backup_type="safety",
        )
        with SessionLocal.begin() as db:
            created = backfill_sparda_imported_balance(db, settings)
        print(f"safety_backup={backup}")
        print(f"sparda_confirmation_created={created}")
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
    elif args.command == "reclassify-sparda":
        if settings.demo_mode:
            raise ValueError("reclassify-sparda requires MONEYOS_DEMO_MODE=false")
        if not args.apply:
            with SessionLocal() as db:
                report = plan_sparda_reclassification(db)
            print(json.dumps(report.as_dict(), indent=2, ensure_ascii=True))
        else:
            if not args.confirm:
                raise ValueError("reclassify-sparda --apply requires --confirm")
            backup = create_backup(
                settings.active_database_url,
                settings.active_backup_dir,
                backup_type="safety",
            )
            with SessionLocal.begin() as db:
                report = apply_sparda_reclassification(db)
            print(f"safety_backup={backup}")
            print(json.dumps(report.as_dict(), indent=2, ensure_ascii=True))
    elif args.command == "audit-sparda-details":
        if settings.demo_mode:
            raise ValueError("audit-sparda-details requires MONEYOS_DEMO_MODE=false")
        with SessionLocal() as db:
            report = audit_sparda_transaction_details(db)
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=True))
    elif args.command == "audit-paypal":
        if settings.demo_mode:
            raise ValueError("audit-paypal requires MONEYOS_DEMO_MODE=false")
        with SessionLocal() as db:
            report = dry_run_paypal_file(args.file, db)
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=True))
    elif args.command == "audit-amex":
        if settings.demo_mode:
            raise ValueError("audit-amex requires MONEYOS_DEMO_MODE=false")
        with SessionLocal() as db:
            report = dry_run_amex_file(args.file, db)
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=True))
    elif args.command == "audit-amazon":
        if settings.demo_mode:
            raise ValueError("audit-amazon requires MONEYOS_DEMO_MODE=false")
        with SessionLocal() as db:
            report = dry_run_amazon_file(args.file, db, settings.amazon_import_start_date)
        print(json.dumps(report, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
