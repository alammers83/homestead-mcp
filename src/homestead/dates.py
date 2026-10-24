"""Day words the tools accept: 'today', 'tomorrow', 'yesterday', or YYYY-MM-DD."""

from __future__ import annotations

import calendar
from datetime import date, timedelta


def resolve_day(day: str, today: date) -> date:
    word = day.strip().lower()
    if word in {"today", ""}:
        return today
    if word == "tomorrow":
        return today + timedelta(days=1)
    if word == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(word)
    except ValueError as exc:
        raise ValueError(f"Use 'today', 'tomorrow', or YYYY-MM-DD (got {day!r})") from exc


def past_day(day: str, today: date) -> date:
    """A date something already happened on; the future is rejected."""
    d = resolve_day(day, today)
    if d > today:
        raise ValueError(f"{d.isoformat()} is in the future")
    return d


def add_months(d: date, months: int) -> date:
    month0 = d.month - 1 + months
    year, month = d.year + month0 // 12, month0 % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))
