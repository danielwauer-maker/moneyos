from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
IBAN_RE = re.compile(r"(?i)\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b")
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
SENSITIVE_KEYS = frozenset(
    {
        "iban",
        "card",
        "card_identifier",
        "account_identifier",
        "email",
        "description",
        "description_raw",
        "raw_text",
        "raw_payload",
        "raw_payload_json",
    }
)
SENSITIVE_LABEL_RE = re.compile(
    r"(?i)\b(?:iban|card|card_identifier|account_identifier|email|description|"
    r"description_raw|raw_text|raw_payload|raw_payload_json)\b"
)


def redact_text(value: object) -> str:
    text = str(value)
    text = EMAIL_RE.sub(REDACTED, text)
    text = IBAN_RE.sub(REDACTED, text)
    return CARD_RE.sub(REDACTED, text)


def redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key.lower() in SENSITIVE_KEYS:
            result[key] = REDACTED
        elif isinstance(item, Mapping):
            result[key] = redact_mapping(item)
        elif isinstance(item, str):
            result[key] = redact_text(item)
        else:
            result[key] = item
    return result


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        sensitive_context = bool(SENSITIVE_LABEL_RE.search(str(record.msg)))
        record.msg = redact_text(record.msg)
        if isinstance(record.args, Mapping):
            record.args = redact_mapping(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(
                REDACTED if sensitive_context else redact_text(item) for item in record.args
            )
        return True


def install_redaction_filters() -> None:
    """Attach redaction to handlers that can emit MoneyOS process logs."""
    for logger_name in (None, "uvicorn", "uvicorn.error", "uvicorn.access", "alembic"):
        logger = logging.getLogger(logger_name)
        for handler in logger.handlers:
            if not any(isinstance(item, SensitiveDataFilter) for item in handler.filters):
                handler.addFilter(SensitiveDataFilter())
