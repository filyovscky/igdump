"""Inclusive calendar-date filtering in the computer's local time zone."""
from __future__ import annotations

import argparse
import re
from datetime import UTC, date, datetime
from typing import Any


def parse_date(value: str) -> date:
    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
        raise argparse.ArgumentTypeError("Дата должна быть в формате ДД.ММ.ГГГГ, например 01.06.2024.")
    try:
        return datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError:
        raise argparse.ArgumentTypeError("Такой даты не существует. Используйте ДД.ММ.ГГГГ.") from None


def post_date(payload: dict[str, Any]) -> date | None:
    timestamp = payload.get("taken_at", payload.get("taken_at_timestamp"))
    if isinstance(timestamp, (int, float)):
        try:
            return datetime.fromtimestamp(timestamp, UTC).astimezone().date()
        except (ValueError, OverflowError, OSError):
            pass
    fallback = payload.get("_fallback_timestamp")
    if isinstance(fallback, str):
        try:
            return datetime.fromisoformat(fallback.replace("Z", "+00:00")).astimezone().date()
        except (ValueError, OverflowError):
            pass
    return None


def date_matches(value: date, after: date | None, before: date | None) -> bool:
    return (after is None or value >= after) and (before is None or value <= before)


def date_label(after: date | None, before: date | None) -> str:
    parts = []
    if after:
        parts.append(f"с {after:%d.%m.%Y}")
    if before:
        parts.append(f"по {before:%d.%m.%Y}")
    return " ".join(parts)
