"""Browser action vocabulary. Never accept executable code from a caller."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .quality import page_diagnostics

# A click is the first action that can leave the page it was made on, and that is deliberate: the
# useful half of a challenge is pressing the control that clears it. The budget below is what stops
# one read from turning into an unbounded crawl.
ACTION_TIMEOUT_MS = 6_000
# How long to look for the element before deciding it is not there. Playwright's own default is
# thirty seconds, which is thirty seconds of a read spent discovering a typo.
PROBE_TIMEOUT_MS = 1_500
NAVIGATION_SETTLE_MS = 5_000
WAIT_DEFAULT_MS = 5_000
MAX_WAIT_MS = 10_000
# A challenge widget is attached by script after the load event, so the frame a caller was told to
# aim at often does not exist yet when the plan runs. Waiting briefly for it is the difference
# between an action that works and one that reports a frame it just missed.
FRAME_WAIT_MS = 2_500
FRAME_POLL_MS = 100
# Actions that need nothing but script in the top document, and actions that need a real document
# reference and real input events. The split matters because a cross-origin widget cannot be reached
# from the page's own JavaScript at all.
SCRIPT_KINDS = frozenset({"expand", "tab", "load_more", "scroll"})
PAGE_KINDS = frozenset({"click", "click_at", "wait_for"})
# Widget documents, matched against frame URLs. These identify the *widget's* document rather than
# any page that mentions a captcha: a publisher's own page can be called /captcha-demo and a marker
# loose enough to match it would point a click at the wrong document. `recaptcha` alone did exactly
# that on recaptcha-demo.appspot.com, so the reCAPTCHA markers name its frame routes.
_CHALLENGE_FRAME_RE = re.compile(
    r"challenges\.cloudflare\.com|/recaptcha/(api2|enterprise)/|hcaptcha\.com|funcaptcha\.com"
    r"|/gcaptcha4_load|/cdn-cgi/challenge-platform",
    re.I,
)
# The prompt of an interstitial is visible text in the top document. Only the head is read, because
# a challenge page is short by construction and a long page's boilerplate would only add noise.
_CHALLENGE_PROBE = "() => ((document.body && document.body.innerText) || '').slice(0, 3000)"
_IN_FORM = "el => Boolean(el.closest('form'))"
_READY_STATE = "() => document.readyState"
# Whether the page hosts a verification widget at all, judged from its markup. A widget's own
# document is attached by script after the load event, so the frame list taken at navigation time is
# often empty even on a page that will grow one. This tells the difference between that page and a
# page that never will, so only the first is waited on.
_WIDGET_MARKUP = (
    "() => /recaptcha|turnstile|hcaptcha|funcaptcha|geetest|challenge-platform/i.test("
    "document.documentElement ? document.documentElement.innerHTML : '')"
)


class ReadAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["expand", "tab", "load_more", "scroll", "click", "click_at", "wait_for"]
    selector: str | None = Field(
        default=None, max_length=300, description="CSS selector, evaluated in `frame` if given."
    )
    frame: str | None = Field(
        default=None,
        max_length=300,
        description=(
            "URL fragment identifying the iframe to act inside. Challenge controls live in another "
            "document, so their selector is unreachable without this; reported by the "
            "`challenge_frame:` warnings of a previous read."
        ),
    )
    x: int | None = Field(
        default=None, ge=0, le=8192, description="Viewport x for click_at, in screenshot pixels."
    )
    y: int | None = Field(
        default=None, ge=0, le=8192, description="Viewport y for click_at, in screenshot pixels."
    )
    text: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Visible text to wait for, matched case-insensitively as a substring. With "
            "duration_ms, that duration becomes the bound on the search."
        ),
    )
    duration_ms: int | None = Field(
        default=None, ge=100, le=MAX_WAIT_MS, description="How long to wait, capped."
    )

    @model_validator(mode="after")
    def validate_target(self) -> ReadAction:
        if self.kind == "scroll":
            taken = [
                self.selector,
                self.frame,
                self.x,
                self.y,
                self.text,
                self.duration_ms,
            ]
            if any(value is not None for value in taken):
                raise ValueError("Scroll takes no arguments")
        elif self.kind in SCRIPT_KINDS | {"click"}:
            if not self.selector:
                raise ValueError(f"{self.kind} requires a CSS selector")
            if self.x is not None or self.y is not None or self.text or self.duration_ms:
                raise ValueError(f"{self.kind} takes only selector and frame")
        elif self.kind == "click_at":
            if self.x is None or self.y is None:
                raise ValueError("click_at requires viewport coordinates x and y")
            if self.selector or self.frame or self.text or self.duration_ms:
                raise ValueError("click_at takes only x and y")
        else:
            if not self.text and not self.duration_ms:
                raise ValueError("wait_for needs text, duration_ms, or both")
            if self.selector or self.frame or self.x is not None or self.y is not None:
                raise ValueError("wait_for takes text or duration_ms only")
        return self


def plan_actions(actions: list[dict]) -> tuple[list[ReadAction], list[ReadAction]]:
    """Split a caller plan into what page script can do and what needs a real pointer.

    `click` deliberately lands on the Playwright side even though `el.click()` exists: a synthetic
    DOM click dispatches an untrusted event, which is precisely what a bot check is looking for.
    """
    validated = [ReadAction.model_validate(action) for action in actions]
    return (
        [action for action in validated if action.kind in SCRIPT_KINDS],
        [action for action in validated if action.kind in PAGE_KINDS],
    )


def action_script(actions: list[dict]) -> str:
    if len(actions) > 5:
        raise ValueError("At most five read-only actions are allowed")
    validated, _ = plan_actions(actions)
    validated = [action for action in validated if action.kind in SCRIPT_KINDS]
    return """return await (async () => {
      for (const action of ACTIONS) {
        if (action.kind === 'scroll') {
          window.scrollBy(0, Math.min(window.innerHeight, 1200));
        } else {
          const el = document.querySelector(action.selector);
          if (!el || el.closest('form')) throw new Error('Unsafe or missing read target');
          if (action.kind === 'expand') {
            const details = el.tagName === 'DETAILS' ? el : el.closest('details');
            if (!details) throw new Error('Expand requires a details element');
            details.open = true;
          } else if (action.kind === 'tab') {
            if (el.getAttribute('role') !== 'tab' || el.tagName === 'A')
              throw new Error('Tab requires a non-link element with role=tab');
            el.click();
          } else {
            if (el.tagName !== 'BUTTON' ||
                !/^(load|show|read) more(?:\\s|$)/i.test(el.textContent.trim()))
              throw new Error('Load-more requires a reading control');
            el.click();
          }
        }
        await new Promise(resolve => setTimeout(resolve, 400));
      }
      return {read_actions_completed: ACTIONS.length};
    })();""".replace("ACTIONS", json.dumps([action.model_dump() for action in validated]))


@dataclass
class ActionRun:
    """One read's action plan and what came of it.

    The plan travels here because a single Chromium serves concurrent reads and crawl4ai hands the
    navigation hook only its own arguments. See `_ACTION_RUN` in the reader.
    """

    plan: list[ReadAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    navigated_to: str | None = None
    challenge_wait_ms: int = 0
    challenge_cleared: bool | None = None
    challenge_frames: list[str] = field(default_factory=list)
    # Set when the markup hosts a verification widget but no widget document ever attached. Under
    # full network isolation that is usually the answer to "why is there nothing to click".
    challenge_widget_missing: bool = False
    # Set when the page would not answer the markup probe. Reporting "no widget here" on the
    # strength of a question that was never answered would be a guess, and a confident one.
    challenge_markup_unreadable: bool = False
    #: Whether this read will touch the page. A page that is about to be clicked, or drawn for a
    #: model to look at, gets its fonts: the reCAPTCHA checkbox is drawn from a webfont, and a read
    #: that blocks the font gets a broken-image glyph where the control should be.
    interactive: bool = False


async def run_page_actions(page: Any, run: ActionRun) -> None:
    """Carry out the actions that need a document reference and trusted input events.

    A failed click is a failure of the read: continuing would extract a page the caller never asked
    for. A `wait_for` that times out is information, not a failure, and is reported instead.
    """
    for action in run.plan:
        if action.kind == "wait_for":
            await _wait_for(page, action, run)
        elif action.kind == "click_at":
            await _click_at(page, action, run)
        else:
            await _click(page, action, run)
    if run.navigated_to:
        # A click that navigated leaves the document still arriving. Extraction has to see the page
        # the click chose, not the fragment that existed when the click returned.
        await _attempt(page.wait_for_load_state("domcontentloaded", timeout=NAVIGATION_SETTLE_MS))


async def wait_out_challenge(
    page: Any,
    grace_seconds: float,
    *,
    poll_ms: int = 400,
    clock=time.monotonic,
) -> tuple[int, bool]:
    """Give a self-clearing interstitial the seconds it needs, and no more than that.

    A managed Cloudflare check usually finishes by itself in two to six seconds, which is longer
    than a fixed post-load delay of half a second ever waits. Returns the time spent and whether the
    challenge signal is gone; a page that never looked challenged costs two evaluations. `clock` is
    injectable so the bound can be tested without spending it.
    """
    started = clock()
    budget = max(0.0, grace_seconds)
    while True:
        if not await _looks_challenged(page):
            return int((clock() - started) * 1000), True
        elapsed = clock() - started
        if elapsed >= budget:
            return int(elapsed * 1000), False
        await _attempt(page.wait_for_timeout(min(poll_ms, int((budget - elapsed) * 1000) or 1)))


async def wait_for_challenge_frame(page: Any, run: ActionRun, wait_ms: int) -> None:
    """Record the widget documents on the run, waiting briefly for one not attached yet.

    A caller can aim a click at a frame only if it was told the frame's name, and the widget's own
    document is usually attached a moment after the navigation. Two outcomes need reporting: the
    frames that exist, and a page that hosts a widget whose document never arrived. The second is
    what a caller under full network isolation needs to hear, because an absent widget document
    means there is nothing to click no matter which selector is tried.
    """
    run.challenge_frames = challenge_frames(page)
    if run.challenge_frames:
        return
    # Two cheap questions decide whether waiting could pay off. The markup says whether a widget is
    # expected; the load state says whether the page is still attaching scripts that could add one.
    # At `after_goto` the markup of a page that will grow a widget often mentions none yet, so
    # "still loading" alone is enough reason to wait. No answer at all is its own result: the reader
    # is blind here and says so instead of reporting absence.
    state = await _value(page.evaluate(_READY_STATE))
    markup = await _value(page.evaluate(_WIDGET_MARKUP))
    if state is None or markup is None:
        run.challenge_markup_unreadable = True
        return
    if not markup and state == "complete":
        return
    remaining_ms = max(0, wait_ms)
    polls = 0
    while remaining_ms > 0:
        if not await _attempt(page.wait_for_timeout(FRAME_POLL_MS)):
            break
        remaining_ms -= FRAME_POLL_MS
        run.challenge_frames = challenge_frames(page)
        if run.challenge_frames:
            return
        polls += 1
        if polls % 5 == 0:
            # Every half second, ask whether the wait can still pay off. A page that has finished
            # loading and still names no widget is not going to grow one, so an ordinary article
            # stops paying here instead of spending the whole bound.
            state = await _value(page.evaluate(_READY_STATE))
            markup = await _value(page.evaluate(_WIDGET_MARKUP))
            if state is None or markup is None:
                run.challenge_markup_unreadable = True
                return
            if state == "complete" and not markup:
                return
    # Nothing attached. Ask the markup once more, because a slow script may have named its widget
    # while we waited: only a page that names a widget can be said to be missing one.
    final = await _value(page.evaluate(_WIDGET_MARKUP))
    if final is None:
        run.challenge_markup_unreadable = True
    elif final:
        run.challenge_widget_missing = True


def challenge_frames(page: Any) -> list[str]:
    """Name the widget documents a caller can aim `frame` at.

    A challenge control lives in another document, so no selector from the page reaches it and the
    caller cannot guess the frame's URL fragment without being told. The query string is dropped:
    it carries the widget's own parameters, and the origin plus route is enough to aim at.
    """
    found: list[str] = []
    main = getattr(page, "main_frame", None)
    for frame in getattr(page, "frames", ()):
        if frame is main:
            # The top document is already addressable by leaving `frame` out; listing it as a widget
            # document invites the caller to aim a widget click at the page instead.
            continue
        url = str(getattr(frame, "url", "") or "")
        if not url or url.startswith("about:") or not _CHALLENGE_FRAME_RE.search(url):
            continue
        parts = urlsplit(url)
        name = f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")
        if name not in found:
            found.append(name)
    return found[:3]


async def _click(page: Any, action: ReadAction, run: ActionRun) -> None:
    frame, problem = await _resolve_frame(page, action.frame)
    if problem:
        run.failures.append(f"click {problem}")
        return
    target = frame.locator(str(action.selector)).first
    try:
        # Wait for the element by scrolling it into view first. A widget's document can exist before
        # the widget has drawn anything in it, and leading with a tiny existence check would call
        # that absence "no such element" while the page was still painting.
        await target.scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS)
        # A click inside a form is a write. The caller allowed navigation but not silence about it,
        # so the fact is reported rather than the click refused. If the question cannot be answered
        # the click still goes ahead: the frame check is context for the caller, not a gate.
        inside_form = bool(await _value(target.evaluate(_IN_FORM, timeout=PROBE_TIMEOUT_MS)))
        before = str(page.url)
        await target.click(timeout=ACTION_TIMEOUT_MS)
    except Exception as exc:  # Playwright raises on missing, hidden and intercepted targets
        run.failures.append(f"click {_reason(exc)}")
        return
    if inside_form:
        run.warnings.append("action_may_submit_form")
    _note_navigation(page, before, run)


async def _click_at(page: Any, action: ReadAction, run: ActionRun) -> None:
    try:
        before = str(page.url)
        # A stepped move rather than a jump: the point of coordinates is that a human pointer
        # travels, and a challenge grades the path as well as the press.
        await page.mouse.move(int(action.x or 0), int(action.y or 0), steps=24)
        await page.mouse.down()
        await page.mouse.up()
    except Exception as exc:
        run.failures.append(f"click_at {_reason(exc)}")
        return
    _note_navigation(page, before, run)


async def _wait_for(page: Any, action: ReadAction, run: ActionRun) -> None:
    if action.duration_ms and not action.text:
        await _attempt(page.wait_for_timeout(int(action.duration_ms)))
        return
    frame, problem = await _resolve_frame(page, action.frame)
    if problem:
        run.warnings.append(f"action_unavailable:wait_for {problem}")
        return
    timeout = int(action.duration_ms or WAIT_DEFAULT_MS)
    if not await _attempt(
        frame.get_by_text(str(action.text), exact=False).first.wait_for(
            state="visible", timeout=timeout
        )
    ):
        run.warnings.append(f"action_timeout:wait_for:{str(action.text)[:60]}")


async def _resolve_frame(page: Any, fragment: str | None) -> tuple[Any, str | None]:
    """Frames are re-resolved per action because a click may have replaced the document.

    A frame that is not there yet is waited for, briefly: the widget a caller was told to click is
    attached after the load event, and reporting "no frame matches" about a frame that appears two
    hundred milliseconds later would be an accurate description of nothing.
    """
    if not fragment:
        return page.main_frame, None
    deadline = FRAME_WAIT_MS
    while True:
        for frame in getattr(page, "frames", ()):
            if fragment in str(getattr(frame, "url", "")):
                return frame, None
        if deadline <= 0:
            break
        # A sleep that cannot happen means the page is gone. Continuing would spin the whole budget
        # and then report a frame list taken from a dead browser.
        if not await _attempt(page.wait_for_timeout(FRAME_POLL_MS)):
            break
        deadline -= FRAME_POLL_MS
    names = ", ".join(sorted({urlsplit(str(f.url)).netloc for f in getattr(page, "frames", ())}))
    return None, f"no frame matches {fragment!r} (frames: {names[:160]})"


def _note_navigation(page: Any, before: str, run: ActionRun) -> None:
    after = str(page.url)
    if after != before:
        run.navigated_to = after
        run.warnings.append(f"action_navigated:{after[:200]}")


async def _looks_challenged(page: Any) -> bool:
    try:
        title = await page.title()
        text = await page.evaluate(_CHALLENGE_PROBE)
    except Exception:  # a document we cannot inspect is not evidence of a challenge
        return False
    return "bot_challenge" in page_diagnostics(title or "", text or "")


async def _value(awaitable: Any) -> Any:
    """Await something that may fail and return what it said, or None."""
    try:
        return await awaitable
    except Exception:  # a detached document answers nothing
        return None


async def _attempt(awaitable: Any) -> bool:
    """Await one browser call and report whether it completed; steps own their own timeouts.

    The answer is "did it work", not "did it raise". Reading the inverse as success turned every
    "wait briefly and look again" loop here into a single pass that never looked again.
    """
    try:
        await awaitable
    except Exception:  # a detached or closing document
        return False
    return True


def _reason(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"


def discover_actions(html_text: str) -> list[dict[str, str]]:
    """Expose only controls that the action executor can actually operate."""
    from lxml import etree, html

    try:
        root = html.fromstring(html_text)
    except (ValueError, TypeError, etree.ParserError):
        return []
    actions = []
    for element in root.iter():
        if not isinstance(element.tag, str) or element.xpath("ancestor::form"):
            continue
        label = " ".join(element.itertext()).strip()[:120]
        if element.tag == "details":
            kind = "expand"
        elif element.get("role") == "tab" and element.tag != "a":
            kind = "tab"
        elif element.tag == "button" and re.match(r"^(load|show|read) more(?:\s|$)", label, re.I):
            kind = "load_more"
        else:
            continue
        # A full DOM path also works for controls without ids, without modifying the page.
        parts = []
        node = element
        while node is not None and isinstance(node.tag, str):
            index = 1 + sum(
                sibling.tag == node.tag for sibling in node.itersiblings(preceding=True)
            )
            parts.append(f"{node.tag}:nth-of-type({index})")
            node = node.getparent()
        selector = " > ".join(reversed(parts))
        if len(selector) <= 300:
            actions.append({"kind": kind, "selector": selector, "label": label})
        if len(actions) >= 30:
            break
    return actions
