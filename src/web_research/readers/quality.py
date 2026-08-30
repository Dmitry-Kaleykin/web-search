from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import ClassVar

from ..text import compact_text

_WORD_RE = re.compile(r"[^\W_]+(?:['\u2019-][^\W_]+)*", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")

# These are rendering/interstitial signals, not a general classification of page quality. Keep
# them narrow so a substantive article that merely discusses JavaScript or Cloudflare is not sent
# to Chromium.
_JAVASCRIPT_REQUIRED_RE = re.compile(
    r"^(?:please )?(?:enable|turn on) javascript[.! ]*$|"
    r"\bplease (?:enable|turn on) javascript\b|"
    r"\byou need to enable javascript to run this app\b|"
    r"\b(?:enable|turn on) javascript "
    r"(?:in your browser|to (?:access|continue|proceed|use|view))\b|"
    r"\bjavascript is required(?: to (?:access|continue|proceed|use|view))?\b|"
    r"\b(?:this|the) (?:app|application|page|site) requires javascript\b|"
    r"\bdoesn['\u2019]t work properly without javascript\b",
    re.IGNORECASE,
)
_LOADING_PLACEHOLDER_RE = re.compile(
    r"^(?:loading|loading[.]{1,3}|please wait|redirecting|initializing|"
    r"waiting for javascript)[.!… ]*$",
    re.IGNORECASE,
)
_BROWSER_CHECK_RE = re.compile(
    r"^(?:just a moment|checking your browser|verifying (?:that )?you are human|"
    r"performing security verification|please stand by)[.!… ]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class HTMLQualityAssessment:
    """Signals that the HTTP response probably needs JavaScript rendering."""

    browser_reasons: tuple[str, ...] = ()

    @property
    def browser_recommended(self) -> bool:
        return bool(self.browser_reasons)

    def warnings(self) -> list[str]:
        return [f"browser_recommended:{reason}" for reason in self.browser_reasons]


def assess_html_quality(html_text: str, extracted_content: str) -> HTMLQualityAssessment:
    """Assess render completeness from semantic signals rather than character counts.

    This deliberately answers only whether browser rendering is likely to recover more content.
    Relevance and evidentiary usefulness are handled later by the research pipeline.
    """

    probe = _HTMLQualityProbe()
    try:
        probe.feed(html_text)
        probe.close()
    except Exception:
        # A malformed document should still be judged from the text the extractor recovered.
        pass

    reasons = list(rendering_signals(extracted_content))
    noscript_signals = rendering_signals(probe.noscript_text)
    if "javascript_required" in noscript_signals and "javascript_required" not in reasons:
        reasons.append("javascript_required")

    if probe.app_root_seen and probe.script_count and not _has_meaningful_text(probe.app_root_text):
        reasons.append("empty_app_shell")
    elif probe.script_count and not _has_meaningful_text(extracted_content):
        reasons.append("script_only_page")
    elif not _has_meaningful_text(extracted_content):
        reasons.append("no_extracted_content")

    return HTMLQualityAssessment(tuple(dict.fromkeys(reasons)))


def rendering_signals(content: str) -> tuple[str, ...]:
    """Return strong text-only indications of an unrendered page or interstitial."""

    lines = [compact_text(line) for line in content.splitlines() if compact_text(line)]
    normalized = _SPACE_RE.sub(" ", content).strip()
    if not normalized:
        return ()

    reasons: list[str] = []
    if _JAVASCRIPT_REQUIRED_RE.search(normalized):
        reasons.append("javascript_required")

    # Loading and browser-check phrases are only decisive when they describe essentially the whole
    # extracted page. This avoids escalating articles that quote or discuss those phrases.
    placeholder_candidate = lines[-1] if len(lines) == 2 else normalized
    if _LOADING_PLACEHOLDER_RE.fullmatch(placeholder_candidate):
        reasons.append("loading_placeholder")
    if _BROWSER_CHECK_RE.fullmatch(placeholder_candidate):
        reasons.append("browser_check_interstitial")
    return tuple(reasons)


def has_meaningful_text(content: str) -> bool:
    """Whether extraction recovered language or numeric content, independent of its length."""

    return _has_meaningful_text(content)


def _has_meaningful_text(content: str) -> bool:
    return any(_WORD_RE.finditer(content))


class _HTMLQualityProbe(HTMLParser):
    APP_ROOT_IDS: ClassVar[set[str]] = {
        "__next",
        "__nuxt",
        "app",
        "app-root",
        "application",
        "root",
    }
    BLOCKED: ClassVar[set[str]] = {"script", "style", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.script_count = 0
        self.app_root_seen = False
        self._blocked_depth = 0
        self._noscript_depth = 0
        self._app_root_depth = 0
        self._stack: list[tuple[str, bool, bool, bool]] = []
        self._noscript_parts: list[str] = []
        self._app_root_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}
        starts_blocked = tag in self.BLOCKED
        starts_noscript = tag == "noscript"
        starts_app_root = _is_app_root(attributes)
        self._stack.append((tag, starts_blocked, starts_noscript, starts_app_root))

        if tag == "script":
            self.script_count += 1
        if starts_blocked:
            self._blocked_depth += 1
        if starts_noscript:
            self._noscript_depth += 1
        if starts_app_root:
            self.app_root_seen = True
            self._app_root_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] != tag:
                continue
            closing = self._stack[index:]
            del self._stack[index:]
            for _, blocked, noscript, app_root in reversed(closing):
                if blocked:
                    self._blocked_depth = max(0, self._blocked_depth - 1)
                if noscript:
                    self._noscript_depth = max(0, self._noscript_depth - 1)
                if app_root:
                    self._app_root_depth = max(0, self._app_root_depth - 1)
            return

    def handle_data(self, data: str) -> None:
        if self._noscript_depth:
            self._noscript_parts.append(data)
        if self._app_root_depth and not self._blocked_depth:
            self._app_root_parts.append(data)

    @property
    def noscript_text(self) -> str:
        return compact_text(" ".join(self._noscript_parts))

    @property
    def app_root_text(self) -> str:
        return compact_text(" ".join(self._app_root_parts))


def _is_app_root(attributes: dict[str, str]) -> bool:
    element_id = attributes.get("id", "").strip().lower()
    if element_id in _HTMLQualityProbe.APP_ROOT_IDS:
        return True
    return "data-reactroot" in attributes or "data-react-root" in attributes
