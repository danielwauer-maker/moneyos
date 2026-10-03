from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ImportBatch, ImportConflict, RawImportRecord, SourceTransaction


class IdentityStatus(StrEnum):
    NEW = "new"
    DUPLICATE_WITHIN_FILE = "duplicate_within_file"
    EXISTING_EXACT = "existing_exact"
    EXISTING_CONFLICT = "existing_conflict"


@dataclass(frozen=True)
class IdentityResult:
    status: IdentityStatus
    existing_source_transaction_id: int | None = None
    differing_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class _KnownIdentity:
    content_hash: str
    source_transaction_id: int | None
    payload: dict[str, str]


LegacyNaturalKey = Callable[[dict[str, str]], str]


def differing_field_names(existing: dict[str, str], incoming: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            key
            for key in set(existing) | set(incoming)
            if (existing.get(key) or "").strip() != (incoming.get(key) or "").strip()
        )
    )


class SourceIdentityTracker:
    """Classify source rows without mutating existing source evidence."""

    def __init__(
        self,
        db: Session | None,
        source_system: str,
        legacy_natural_key: LegacyNaturalKey,
    ) -> None:
        self.exact: dict[str, int] = {}
        self.natural: dict[str, _KnownIdentity] = {}
        self.seen_exact: set[str] = set()
        self.seen_natural: dict[str, _KnownIdentity] = {}
        if db is None:
            return
        rows = db.execute(
            select(SourceTransaction, RawImportRecord)
            .outerjoin(RawImportRecord, RawImportRecord.id == SourceTransaction.raw_record_id)
            .where(SourceTransaction.source_system == source_system)
        )
        for source, raw in rows:
            self.exact[source.fingerprint] = source.id
            payload = {
                str(key): str(value or "")
                for key, value in ((raw.raw_payload_json or {}).items() if raw else [])
            }
            natural_key = source.source_natural_key
            if natural_key is None and payload:
                natural_key = legacy_natural_key(payload)
            if natural_key:
                self.natural.setdefault(
                    natural_key,
                    _KnownIdentity(source.fingerprint, source.id, payload),
                )

    def classify(
        self,
        *,
        natural_key: str,
        content_hash: str,
        payload: dict[str, str],
    ) -> IdentityResult:
        if content_hash in self.seen_exact:
            return IdentityResult(IdentityStatus.DUPLICATE_WITHIN_FILE)
        self.seen_exact.add(content_hash)

        existing_id = self.exact.get(content_hash)
        if existing_id is not None:
            return IdentityResult(IdentityStatus.EXISTING_EXACT, existing_id)

        known = self.seen_natural.get(natural_key) or self.natural.get(natural_key)
        if known is not None and known.content_hash != content_hash:
            return IdentityResult(
                IdentityStatus.EXISTING_CONFLICT,
                known.source_transaction_id,
                differing_field_names(known.payload, payload),
            )

        self.seen_natural[natural_key] = _KnownIdentity(content_hash, None, payload)
        return IdentityResult(IdentityStatus.NEW)


def add_source_conflict(
    db: Session,
    batch: ImportBatch,
    *,
    source_system: str,
    natural_key: str,
    incoming_content_hash: str,
    result: IdentityResult,
) -> None:
    exists = db.scalar(
        select(ImportConflict.id).where(
            ImportConflict.import_batch_id == batch.id,
            ImportConflict.source_system == source_system,
            ImportConflict.natural_key == natural_key,
            ImportConflict.incoming_content_hash == incoming_content_hash,
        )
    )
    if exists is not None:
        return
    db.add(
        ImportConflict(
            import_batch_id=batch.id,
            source_system=source_system,
            natural_key=natural_key,
            incoming_content_hash=incoming_content_hash,
            existing_source_transaction_id=result.existing_source_transaction_id,
            differing_fields_json=list(result.differing_fields),
            status="open",
        )
    )
