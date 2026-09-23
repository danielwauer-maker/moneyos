from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    Account,
    EconomicEvent,
    Envelope,
    EnvelopeRulePeriod,
    EnvelopeSnapshot,
    EventSourceLink,
    SourceTransaction,
    SourceTransactionAccount,
)

D = Decimal
REQUIRED_ACCOUNT_KEYS = frozenset({"sparda", "amex", "paypal", "wallet", "vault", "c24"})


@dataclass(frozen=True)
class PrivateInitializationResult:
    accounts_created: int
    envelopes_created: int
    snapshots_created: int
    rules_created: int
    source_links_created: int
    events_backfilled: int
    transfer_targets_linked: int
    sparda_balance_confirmed: bool


def _load_master_data(settings: Settings) -> dict[str, Any]:
    path = settings.profile_private_data_dir / "master_data.json"
    if not path.is_file():
        raise FileNotFoundError(
            "Private master data file is missing; create it in the ignored "
            "private profile directory"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    accounts = payload.get("accounts")
    envelopes = payload.get("envelopes")
    if not isinstance(accounts, list) or not isinstance(envelopes, list):
        raise ValueError("Private master data requires accounts and envelopes lists")
    keys = {item.get("key") for item in accounts if isinstance(item, dict)}
    if keys != REQUIRED_ACCOUNT_KEYS:
        raise ValueError("Private master data account keys are incomplete or duplicated")
    return payload


def _get_or_create_accounts(
    db: Session, specs: list[dict[str, Any]]
) -> tuple[dict[str, Account], int]:
    accounts: dict[str, Account] = {}
    created = 0
    for spec in specs:
        key = str(spec["key"])
        name = str(spec["name"])
        account_type = str(spec["account_type"])
        active = bool(spec["is_active"])
        liability = bool(spec["is_liability"])
        account = db.scalar(select(Account).where(Account.name == name))
        if account is None:
            account = Account(
                name=name,
                account_type=account_type,
                balance=D("0"),
                balance_confirmed=False,
                is_active=active,
                is_liability=liability,
            )
            db.add(account)
            db.flush()
            created += 1
        elif (
            account.account_type != account_type
            or account.is_active != active
            or account.is_liability != liability
        ):
            raise ValueError(f"Account master data conflict for {name}")
        accounts[key] = account
    return accounts, created


def _add_rule(
    db: Session,
    envelope: Envelope,
    *,
    valid_from: date,
    valid_to: date | None,
    monthly_amount: Decimal | None,
    rule_type: str,
    target_amount: Decimal | None = None,
) -> bool:
    existing = db.scalar(
        select(EnvelopeRulePeriod).where(
            EnvelopeRulePeriod.envelope_id == envelope.id,
            EnvelopeRulePeriod.valid_from == valid_from,
            EnvelopeRulePeriod.rule_type == rule_type,
        )
    )
    expected = (valid_to, monthly_amount, target_amount)
    if existing is not None:
        actual = (existing.valid_to, existing.monthly_amount, existing.target_amount)
        if actual != expected:
            raise ValueError(f"Envelope rule conflict for {envelope.name}")
        return False
    db.add(
        EnvelopeRulePeriod(
            envelope_id=envelope.id,
            valid_from=valid_from,
            valid_to=valid_to,
            monthly_amount=monthly_amount,
            rule_type=rule_type,
            target_amount=target_amount,
            notes="Bestätigte private Beitragsregel",
        )
    )
    return True


def _initialize_envelopes(db: Session, specs: list[dict[str, Any]]) -> tuple[int, int, int]:
    envelopes_created = snapshots_created = rules_created = 0
    for order, spec in enumerate(specs):
        name = str(spec["name"])
        baseline = D(str(spec["baseline"]))
        rule_type = str(spec["target_rule_type"])
        target = D(str(spec["target_amount"])) if spec.get("target_amount") is not None else None
        envelope = db.scalar(select(Envelope).where(Envelope.name == name))
        if envelope is None:
            envelope = Envelope(
                name=name,
                target_rule_type=rule_type,
                target_amount=target,
                sort_order=order,
                notes="Privater physischer Umschlag",
            )
            db.add(envelope)
            db.flush()
            envelopes_created += 1
        snapshot = db.scalar(
            select(EnvelopeSnapshot).where(
                EnvelopeSnapshot.envelope_id == envelope.id,
                EnvelopeSnapshot.snapshot_date == date(2026, 4, 30),
            )
        )
        if snapshot is None:
            db.add(
                EnvelopeSnapshot(
                    envelope_id=envelope.id,
                    snapshot_date=date(2026, 4, 30),
                    physical_balance=baseline,
                    source="confirmed_private_baseline",
                    is_confirmed=True,
                )
            )
            snapshots_created += 1
        elif snapshot.physical_balance != baseline or not snapshot.is_confirmed:
            raise ValueError(f"Envelope baseline conflict for {name}")

        for rule in spec.get("rules", []):
            rules_created += _add_rule(
                db,
                envelope,
                valid_from=date.fromisoformat(str(rule["valid_from"])),
                valid_to=(
                    date.fromisoformat(str(rule["valid_to"])) if rule.get("valid_to") else None
                ),
                monthly_amount=(
                    D(str(rule["monthly_amount"]))
                    if rule.get("monthly_amount") is not None
                    else None
                ),
                rule_type=str(rule["rule_type"]),
                target_amount=(
                    D(str(rule["target_amount"])) if rule.get("target_amount") is not None else None
                ),
            )
    return envelopes_created, snapshots_created, rules_created


def _latest_sparda_balance(sources: list[SourceTransaction]) -> Decimal | None:
    with_balance = [source for source in sources if source.balance_after is not None]
    if not with_balance:
        return None
    latest_date = max(source.booked_at for source in with_balance)
    candidates = [source for source in with_balance if source.booked_at == latest_date]
    export_descending = with_balance[0].booked_at >= with_balance[-1].booked_at
    selected = (
        min(candidates, key=lambda row: row.id)
        if export_descending
        else max(candidates, key=lambda row: row.id)
    )
    return selected.balance_after


def _ensure_account_link(db: Session, source_id: int, account_id: int, role: str) -> bool:
    existing = db.scalar(
        select(SourceTransactionAccount).where(
            SourceTransactionAccount.source_transaction_id == source_id,
            SourceTransactionAccount.role == role,
        )
    )
    if existing is not None:
        if existing.account_id != account_id:
            raise ValueError("Source transaction account link conflict")
        return False
    db.add(
        SourceTransactionAccount(
            source_transaction_id=source_id,
            account_id=account_id,
            role=role,
        )
    )
    return True


def _backfill_sparda(db: Session, accounts: dict[str, Account]) -> tuple[int, int, int, bool]:
    sources = list(
        db.scalars(
            select(SourceTransaction)
            .where(SourceTransaction.source_system == "sparda")
            .order_by(SourceTransaction.id)
        )
    )
    sparda = accounts["sparda"]
    source_links_created = 0
    for source in sources:
        source_links_created += _ensure_account_link(db, source.id, sparda.id, "source")

    balance = _latest_sparda_balance(sources)
    if balance is not None:
        sparda.balance = balance
        sparda.balance_confirmed = True

    target_by_semantic = {
        "american_express_settlement": accounts["amex"],
        "paypal_funding_leg": accounts["paypal"],
    }
    target_by_type = {
        "cash_wallet": accounts["wallet"],
        "cash_vault": accounts["vault"],
    }
    events_backfilled = transfer_targets_linked = 0
    rows = db.execute(
        select(EconomicEvent, SourceTransaction)
        .join(EventSourceLink, EventSourceLink.economic_event_id == EconomicEvent.id)
        .join(SourceTransaction, SourceTransaction.id == EventSourceLink.source_transaction_id)
        .where(SourceTransaction.source_system == "sparda")
    ).all()
    for event, source in rows:
        changed = event.account_id != sparda.id
        event.account_id = sparda.id
        if event.event_type == "transfer":
            event.source_account_id = sparda.id
            metadata = source.metadata_json or {}
            target = target_by_semantic.get(metadata.get("sparda_semantic"))
            if target is None:
                target = target_by_type.get(metadata.get("target_account_type"))
            if target is not None:
                event.target_account_id = target.id
                transfer_targets_linked += _ensure_account_link(db, source.id, target.id, "target")
        events_backfilled += changed
    return source_links_created, events_backfilled, transfer_targets_linked, balance is not None


def initialize_private_profile(db: Session, settings: Settings) -> PrivateInitializationResult:
    if settings.demo_mode:
        raise ValueError("Private master data initialization requires the private profile")
    master_data = _load_master_data(settings)
    accounts, accounts_created = _get_or_create_accounts(db, master_data["accounts"])
    envelopes_created, snapshots_created, rules_created = _initialize_envelopes(
        db, master_data["envelopes"]
    )
    source_links, events_backfilled, transfer_targets, balance_confirmed = _backfill_sparda(
        db, accounts
    )
    return PrivateInitializationResult(
        accounts_created=accounts_created,
        envelopes_created=envelopes_created,
        snapshots_created=snapshots_created,
        rules_created=rules_created,
        source_links_created=source_links,
        events_backfilled=events_backfilled,
        transfer_targets_linked=transfer_targets,
        sparda_balance_confirmed=balance_confirmed,
    )
