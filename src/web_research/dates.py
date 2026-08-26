from __future__ import annotations

import json
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

DATE_KEYS = {
    "article:published_time",
    "date",
    "datecreated",
    "datepublished",
    "dc.date",
    "og:published_time",
    "publishdate",
    "pubdate",
}
PARTIAL_DATE_RE = re.compile(r"^\d{4}(?:-\d{2})?$")


def normalize_published_at(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    if not text:
        return None
    if PARTIAL_DATE_RE.fullmatch(text):
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.isoformat()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def published_at_from_html(html_text: str) -> tuple[str | None, str | None]:
    parser = _PublicationMetadataParser()
    try:
        parser.feed(html_text)
    except Exception:
        return None, None
    for value, source in parser.candidates:
        normalized = normalize_published_at(value)
        if normalized:
            return normalized, source
    return None, None


class _PublicationMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: list[tuple[str, str]] = []
        self._json_ld_depth = 0
        self._json_ld_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value for key, value in attrs}
        if tag.casefold() == "meta":
            key = str(
                attributes.get("property")
                or attributes.get("name")
                or attributes.get("itemprop")
                or ""
            ).casefold()
            content = attributes.get("content")
            if key in DATE_KEYS and content:
                self.candidates.append((content, f"html_meta:{key}"))
        elif tag.casefold() == "time" and attributes.get("datetime"):
            self.candidates.append((str(attributes["datetime"]), "html_time"))
        elif (
            tag.casefold() == "script"
            and str(attributes.get("type") or "").casefold() == "application/ld+json"
        ):
            self._json_ld_depth += 1
            self._json_ld_parts = []

    def handle_data(self, data: str) -> None:
        if self._json_ld_depth:
            self._json_ld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "script" or not self._json_ld_depth:
            return
        self._json_ld_depth -= 1
        try:
            payload = json.loads("".join(self._json_ld_parts))
        except (json.JSONDecodeError, TypeError):
            return
        for value in _find_json_values(payload, "datePublished"):
            self.candidates.append((str(value), "json_ld:datePublished"))


def _find_json_values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        if key in value:
            found.append(value[key])
        for child in value.values():
            found.extend(_find_json_values(child, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_json_values(child, key))
    return found
