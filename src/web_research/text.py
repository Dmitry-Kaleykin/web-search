from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import ClassVar
from urllib.parse import urljoin

TOKEN_RE = re.compile(r"[\w'-]{2,}", re.UNICODE)
SPACE_RE = re.compile(r"\s+")


def tokens(text: str) -> set[str]:
    return {match.group(0).lower() for match in TOKEN_RE.finditer(text)}


def lexical_similarity(left: str, right: str) -> float:
    left_tokens = tokens(left)
    right_tokens = tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def compact_text(text: str) -> str:
    return SPACE_RE.sub(" ", text).strip()


def best_excerpt(content: str, target: str, *, limit: int = 700) -> str:
    paragraphs = [compact_text(item) for item in re.split(r"\n\s*\n|(?<=[.!?])\s+", content)]
    paragraphs = [item for item in paragraphs if item]
    if not paragraphs:
        return compact_text(content)[:limit]
    best = max(paragraphs, key=lambda item: lexical_similarity(item, target))
    return best[:limit]


class BasicHTMLExtractor(HTMLParser):
    """Small dependency-free fallback; Trafilatura is the production extractor."""

    BLOCKED: ClassVar[set[str]] = {"script", "style", "noscript", "svg"}

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.blocked_depth = 0
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self.title_parts: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.BLOCKED:
            self.blocked_depth += 1
        if tag == "title":
            self.in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                absolute = urljoin(self.base_url, href)
                if absolute.startswith(("http://", "https://")):
                    self.links.append(absolute)
        if tag in {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.BLOCKED and self.blocked_depth:
            self.blocked_depth -= 1
        if tag == "title":
            self.in_title = False
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.blocked_depth:
            return
        value = html.unescape(data)
        self.text_parts.append(value)
        if self.in_title:
            self.title_parts.append(value)

    @property
    def content(self) -> str:
        lines = [compact_text(item) for item in "".join(self.text_parts).splitlines()]
        return "\n\n".join(item for item in lines if item)

    @property
    def title(self) -> str:
        return compact_text(" ".join(self.title_parts))


def extract_html_fallback(html_text: str, base_url: str) -> tuple[str, str, list[str]]:
    parser = BasicHTMLExtractor(base_url)
    parser.feed(html_text)
    return parser.title, parser.content, list(dict.fromkeys(parser.links))
