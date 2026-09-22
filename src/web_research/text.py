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


class BasicHTMLExtractor(HTMLParser):
    """Small text fallback; Trafilatura is the production extractor."""

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
    parser.feed(absolute_html_links(html_text, base_url))
    return parser.title, parser.content, list(dict.fromkeys(parser.links))


def absolute_html_links(html_text: str, page_url: str) -> str:
    """Resolve links before extractors can replace the document base with its origin.

    Uses the final response URL and honors HTML base href. This only transforms markup;
    following any returned link still passes through the reader's URL safety checks.
    """
    from lxml import etree
    from lxml import html as lxml_html

    try:
        tree = lxml_html.fromstring(html_text)
        bases = tree.xpath("//base[@href]")
        effective_base = urljoin(page_url, bases[0].get("href")) if bases else page_url
        for base in bases:
            base.drop_tree()
        tree.make_links_absolute(effective_base, resolve_base_href=False, handle_failures="ignore")
        return lxml_html.tostring(tree, encoding="unicode")
    except (ValueError, etree.ParserError):
        return html_text
