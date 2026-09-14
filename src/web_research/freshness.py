"""Deterministic publication windows and explicit cache policy for time-sensitive requests."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True, slots=True)
class DateWindow:
    start: date
    end: date
    inferred: bool = False

    def contains(self, published_at: str | None) -> bool:
        if not published_at:
            return False
        # A partial date is only sufficient if its entire possible interval fits the request.
        try:
            value = published_at[:10]
            if re.fullmatch(r"\d{4}", value):
                first, last = date(int(value), 1, 1), date(int(value), 12, 31)
            elif re.fullmatch(r"\d{4}-\d{2}", value):
                year, month = map(int, value.split("-"))
                first, last = (
                    date(year, month, 1),
                    date(year, month, calendar.monthrange(year, month)[1]),
                )
            else:
                first = last = date.fromisoformat(value)
        except ValueError:
            return False
        return self.start <= first <= last <= self.end


def publication_window(text: str, *, as_of: date | None = None) -> DateWindow | None:
    today = as_of or date.today()
    value = text.casefold()
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", value)
    if dates:
        parsed = [date.fromisoformat(item) for item in dates]
        if len(parsed) >= 2:
            if parsed[0] > parsed[1]:
                raise ValueError("Freshness start date must not follow end date")
            return DateWindow(parsed[0], min(parsed[1], today))
        if re.search(r"\b(since|after|from)\b", value):
            return DateWindow(parsed[0], today)
        if re.search(r"\b(before|until|through)\b", value):
            return DateWindow(date.min, min(parsed[0], today))
        return DateWindow(parsed[0], min(parsed[0], today))
    recent = re.search(
        r"\b(?:last|past)\s+(\d+|one|two|seven|thirty)\s+(day|week|month|year)s?\b", value
    )
    if recent:
        number, unit = recent.groups()
        n = (
            int(number)
            if number.isdigit()
            else {"one": 1, "two": 2, "seven": 7, "thirty": 30}[number]
        )
        days = min(n * {"day": 1, "week": 7, "month": 30, "year": 365}[unit], 36500)
        return DateWindow(today - timedelta(days=max(0, days - 1)), today)
    if "yesterday" in value:
        yesterday = today - timedelta(days=1)
        return DateWindow(yesterday, yesterday)
    if "today" in value:
        return DateWindow(today, today)
    for period, days in (("week", 7), ("month", 30), ("year", 365)):
        if re.search(rf"\b(?:last|past|this) {period}\b", value):
            return DateWindow(today - timedelta(days=days - 1), today)
    years = re.findall(r"\b(?:19|20)\d{2}\b", value)
    if years:
        start = date(int(years[0]), 1, 1)
        end = (
            today
            if re.search(r"\b(since|after)\b", value)
            else min(date(int(years[-1]), 12, 31), today)
        )
        return DateWindow(start, end)
    if re.search(r"\b(latest|recent|recently|current|currently|newest|up.to.date)\b", value):
        return DateWindow(today - timedelta(days=29), today, inferred=True)
    return None
