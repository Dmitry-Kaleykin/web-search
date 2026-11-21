"""Real-input actions and the challenge wait, exercised against stand-ins for Playwright objects.

The point of these tests is not that methods were called. Each one pins a decision: which document a
click landed in, whether a failure was reported instead of papered over, whether a wait that failed
cost the caller the page, and how much time an ordinary page pays for the challenge check.
"""

from __future__ import annotations

import pytest

from web_research.readers.actions import (
    ACTION_TIMEOUT_MS,
    ActionRun,
    ReadAction,
    challenge_frames,
    plan_actions,
    run_page_actions,
    wait_for_challenge_frame,
    wait_out_challenge,
)


class FakeLocator:
    def __init__(
        self, frame, name: str, *, in_form=False, fails: bool = False, unreachables: bool = False
    ) -> None:
        self.unreachables = unreachables
        self.frame = frame
        self.name = name
        self.in_form = in_form
        self.fails = fails
        self.clicked = 0
        self.scrolled = 0

    async def evaluate(self, _script: str, *, timeout: int) -> bool:
        self.frame.probed.append(timeout)
        return self.in_form

    async def scroll_into_view_if_needed(self, *, timeout: int) -> None:
        if self.unreachables:
            raise TimeoutError(f"Locator.scroll_into_view_if_needed: Timeout {timeout}ms exceeded")
        self.scrolled += 1

    async def click(self, *, timeout: int) -> None:
        if self.fails:
            raise TimeoutError(f"locator.click: Timeout {timeout}ms exceeded\nhidden element")
        self.clicked += 1
        self.frame.clicked += 1
        self.frame.page.after_click(self.frame, self.name)

    @property
    def first(self):
        return self


class FakeText:
    def __init__(self, frame, visible: bool) -> None:
        self.frame = frame
        self.visible = visible

    @property
    def first(self):
        return self

    async def wait_for(self, *, state: str, timeout: int) -> None:
        self.frame.page.timeouts.append(timeout)
        if not self.visible:
            raise TimeoutError(f"waiting for {state} text, {timeout}ms exceeded")


class FakeFrame:
    def __init__(self, page, url: str, *, in_form=False, fails=False, text_visible=True) -> None:
        self.page = page
        self.url = url
        self.in_form = in_form
        self.fails = fails
        self.text_visible = text_visible
        self.locators: list[str] = []
        self.probed: list[int] = []
        self.unreachable = False
        self.clicked = 0

    def locator(self, selector: str) -> FakeLocator:
        self.locators.append(selector)
        kind = getattr(self, "locator_class", FakeLocator)
        return kind(
            self,
            selector,
            in_form=self.in_form,
            fails=self.fails,
            unreachables=self.unreachable,
        )

    def get_by_text(self, text: str, *, exact: bool) -> FakeText:
        self.text_asked = text
        return FakeText(self, self.text_visible)


class FakeMouse:
    def __init__(self) -> None:
        self.moves: list[tuple] = []
        self.pressed = 0

    async def move(self, x: int, y: int, *, steps: int) -> None:
        self.moves.append((x, y, steps))

    async def down(self) -> None:
        self.pressed += 1

    async def up(self) -> None:
        pass


class FakePage:
    def __init__(
        self,
        url="https://site.example/form",
        frames=(),
        *,
        challenged_texts=(),
        click_fails=False,
        clock=None,
        hosts_widget=False,
    ) -> None:
        self.url = url
        self.hosts_widget = hosts_widget
        self.mouse = FakeMouse()
        self.settled = 0
        self.slept: list[int] = []
        self.evaluated = 0
        self.clock = clock
        self._titles = list(challenged_texts) or ["Ordinary article"]
        self.timeouts: list[int] = []
        self.main_frame = FakeFrame(self, url, fails=click_fails)
        for frame in frames:
            # A frame belongs to the page holding it, and the fake has to agree: a click reports the
            # navigation on the page it was made from, not on the page that built the frame.
            frame.page = self
        self.frames = [self.main_frame, *frames]

    def after_click(self, frame, _selector: str) -> None:
        if "leave" in frame.url:
            self.url = "https://other.example/landed"

    async def title(self) -> str:
        return self._titles.pop(0) if len(self._titles) > 1 else self._titles[0]

    ready_state = "complete"

    async def evaluate(self, _script: str):
        self.evaluated += 1
        if "innerHTML" in _script:
            # The markup probe: does this page host a verification widget at all?
            return self.hosts_widget
        if "readyState" in _script:
            return self.ready_state
        return "Just a moment…\nVerifying that you are human" if len(self._titles) > 1 else ""

    async def wait_for_timeout(self, ms: int) -> None:
        self.slept.append(ms)
        if self.clock is not None:
            # A real sleep spends wall time; the fake spends the virtual clock it was given, so a
            # bounded wait can be tested for its bound instead of for its duration.
            self.clock.now += ms / 1000

    async def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        self.settled += 1


def run_for(actions: list[dict], **kwargs) -> ActionRun:
    _, page_plan = plan_actions(actions)
    return ActionRun(plan=page_plan, **kwargs)


# --- where a click lands -----------------------------------------------------------------------


async def test_a_click_lands_in_the_frame_it_was_aimed_at():
    widget = FakeFrame(
        FakePage(), "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b"
    )
    page = FakePage(frames=[widget])
    run = run_for(
        [
            {
                "kind": "click",
                "selector": "input[type=checkbox]",
                "frame": "challenges.cloudflare.com",
            }
        ]
    )

    await run_page_actions(page, run)

    assert widget.locators == ["input[type=checkbox]"]
    assert page.main_frame.locators == []
    assert run.failures == []


async def test_a_frame_that_does_not_exist_fails_rather_than_clicking_the_wrong_document():
    page = FakePage()
    run = run_for([{"kind": "click", "selector": "input", "frame": "hcaptcha.com"}])

    await run_page_actions(page, run)

    assert run.failures and "no frame matches" in run.failures[0]
    assert page.main_frame.locators == []
    # The miss is admitted after the short wait for a late widget, not after a long one.
    assert len(page.slept) <= 30


async def test_a_click_that_navigates_is_reported_and_the_new_page_is_settled():
    leaving = FakeFrame(FakePage(), "https://site.example/leave-now")
    page = FakePage(frames=[leaving])
    run = run_for([{"kind": "click", "selector": "a", "frame": "leave-now"}])

    await run_page_actions(page, run)

    assert run.navigated_to == "https://other.example/landed"
    assert run.warnings == ["action_navigated:https://other.example/landed"]
    assert page.settled == 1


async def test_looking_for_the_target_does_not_inherit_playwrights_patience():
    page = FakePage()
    run = run_for([{"kind": "click", "selector": "#toggle"}])

    await run_page_actions(page, run)

    # A missing element should be admitted inside the action's own bound, not after Playwright's
    # default thirty seconds of waiting.
    assert page.main_frame.locators == ["#toggle"]
    assert page.main_frame.probed and max(page.main_frame.probed) <= ACTION_TIMEOUT_MS


async def test_a_click_inside_a_form_is_allowed_and_reported_as_a_write():
    form = FakeFrame(FakePage(), "https://site.example/form?widget", in_form=True)
    page = FakePage()
    page.main_frame = form
    page.frames = [form]
    run = run_for([{"kind": "click", "selector": "button[type=submit]"}])

    await run_page_actions(page, run)

    assert run.warnings == ["action_may_submit_form"]
    assert run.failures == []


async def test_a_click_that_cannot_reach_its_target_fails_the_read():
    page = FakePage(click_fails=True)
    run = run_for([{"kind": "click", "selector": "#gone"}])

    await run_page_actions(page, run)

    assert len(run.failures) == 1
    # The first line of a Playwright failure is the part that says what went wrong; the stack
    # trace beneath it describes their library, not the page.
    assert run.failures[0].startswith("click TimeoutError: locator.click: Timeout")


async def test_an_element_that_never_appears_is_admitted_at_the_actions_own_bound():
    """A widget document can exist before the widget has drawn anything inside it."""
    page = FakePage()
    page.main_frame.unreachable = True
    run = run_for([{"kind": "click", "selector": ".recaptcha-checkbox-border"}])

    await run_page_actions(page, run)

    waited = f"Locator.scroll_into_view_if_needed: Timeout {ACTION_TIMEOUT_MS}ms exceeded"
    assert run.failures == [f"click TimeoutError: {waited}"]
    assert page.main_frame.clicked == 0


async def test_a_form_check_that_cannot_be_answered_still_clicks():
    page = FakePage()

    class Unanswerable(FakeLocator):
        async def evaluate(self, _script: str, *, timeout: int) -> bool:
            raise RuntimeError("Execution context was destroyed")

    page.main_frame.locator_class = Unanswerable
    run = run_for([{"kind": "click", "selector": "#toggle"}])

    await run_page_actions(page, run)

    assert run.failures == []
    assert page.main_frame.clicked == 1
    assert run.warnings == []


async def test_the_pointer_travels_before_it_presses():
    page = FakePage()
    run = run_for([{"kind": "click_at", "x": 640, "y": 300}])

    await run_page_actions(page, run)

    assert page.mouse.moves == [(640, 300, 24)]
    assert page.mouse.pressed == 1


# --- waiting ----------------------------------------------------------------------------------


async def test_a_wait_that_times_out_is_reported_instead_of_costing_the_page():
    page = FakePage()
    page.main_frame.text_visible = False
    run = run_for([{"kind": "wait_for", "text": "Just a moment", "duration_ms": 500}])

    await run_page_actions(page, run)

    assert run.failures == []
    assert run.warnings == ["action_timeout:wait_for:Just a moment"]
    # With both given, the duration is the bound on the search rather than a separate pause.
    assert page.timeouts == [500]


async def test_waiting_for_a_duration_needs_no_target():
    page = FakePage()
    run = run_for([{"kind": "wait_for", "duration_ms": 700}])

    await run_page_actions(page, run)

    assert page.slept == [700]
    assert run.warnings == []


# --- the challenge grace ----------------------------------------------------------------------


async def test_an_ordinary_page_pays_one_inspection_and_no_waiting():
    page = FakePage()

    waited, cleared = await wait_out_challenge(page, 8.0)

    assert (cleared, waited) == (True, 0)
    assert page.evaluated == 1
    assert page.slept == []


async def test_a_self_clearing_interstitial_is_watched_until_it_passes():
    page = FakePage(challenged_texts=["Just a moment", "Just a moment", "Real article title"])

    waited, cleared = await wait_out_challenge(page, 8.0, poll_ms=40)

    assert cleared is True
    # `waited` measures the page's own clock, and this fake sleeps instantly, so what this test can
    # show honestly is that the loop watched the page clear instead of giving up on the first look.
    assert waited >= 0
    assert page.evaluated == 3
    assert page.slept and max(page.slept) <= 40


async def test_a_challenge_that_stays_put_costs_only_its_grace():
    class Stubborn(FakePage):
        async def title(self) -> str:
            return "Just a moment…"

        async def evaluate(self, _script: str) -> str:
            self.evaluated += 1
            return "Verifying that you are human"

    class Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    page = Stubborn(clock=clock)

    waited, cleared = await wait_out_challenge(page, 0.5, poll_ms=100, clock=clock)

    assert cleared is False
    assert waited == 500, (waited, page.slept)
    # The poll interval is the norm; the last step is whatever was left of the grace, which is why
    # the tail is short. Spending the bound and not exceeding it is the whole contract.
    assert page.slept[:4] == [100, 100, 100, 100]
    assert sum(page.slept) == pytest.approx(500, abs=2)


async def test_a_document_that_cannot_be_inspected_is_not_called_a_challenge():
    class Unreadable(FakePage):
        async def title(self) -> str:
            raise RuntimeError("Target page, context or browser has been closed")

    _waited, cleared = await wait_out_challenge(Unreadable(), 1.0)
    assert cleared is True


async def test_challenge_widgets_are_named_without_their_query_strings():
    page = FakePage(
        frames=[
            FakeFrame(
                None,
                "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile?hl=en",
            ),
            FakeFrame(None, "https://www.google.com/recaptcha/api2/anchor?k=6Le-token"),
            FakeFrame(None, "about:blank"),
            FakeFrame(None, "https://site.example/side-panel"),
        ]
    )

    assert challenge_frames(page) == [
        "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile",
        "https://www.google.com/recaptcha/api2/anchor",
    ]


async def test_the_publishers_own_page_is_not_reported_as_a_widget_document():
    """Measured on recaptcha-demo.appspot.com: a page *about* a captcha is not the captcha."""
    page = FakePage(url="https://recaptcha-demo.appspot.com/recaptcha-v2-checkbox.php")

    assert challenge_frames(page) == []


async def test_a_widget_frame_that_arrives_late_is_still_clicked():
    """The widget is attached by script after the load event, so the frame may not exist yet."""

    class LateWidget(FakePage):
        def __init__(self) -> None:
            super().__init__()
            self.widget = FakeFrame(
                self, "https://www.google.com/recaptcha/api2/bframe?k=6Le-token"
            )

        async def wait_for_timeout(self, ms: int) -> None:
            self.slept.append(ms)
            if len(self.slept) == 2:
                self.frames.append(self.widget)

    page = LateWidget()
    run = run_for(
        [{"kind": "click", "selector": "input[type=checkbox]", "frame": "recaptcha/api2"}]
    )

    await run_page_actions(page, run)

    assert page.widget.locators == ["input[type=checkbox]"]
    assert run.failures == []


async def test_a_finished_page_without_a_widget_does_not_pay_the_frame_wait():
    page = FakePage()
    page.ready_state = "complete"
    run = ActionRun(plan=[])

    await wait_for_challenge_frame(page, run, 2_500)

    assert run.challenge_frames == []
    # A page with no widget must not be reported as a page whose widget failed to load, and must
    # not spend the wait looking for one.
    assert run.challenge_widget_missing is False
    assert page.slept == []


async def test_a_widget_that_attaches_its_document_late_is_still_reported():
    """The widget document is created by script after the load event, so the list starts empty."""

    class LateWidget(FakePage):
        def __init__(self) -> None:
            super().__init__(hosts_widget=True)
            self.widget = FakeFrame(
                self, "https://www.google.com/recaptcha/api2/bframe?k=6Le-token"
            )

        async def wait_for_timeout(self, ms: int) -> None:
            self.slept.append(ms)
            if len(self.slept) == 3:
                self.frames.append(self.widget)

    page = LateWidget()
    run = ActionRun(plan=[])

    await wait_for_challenge_frame(page, run, 2_500)

    assert run.challenge_frames == ["https://www.google.com/recaptcha/api2/bframe"]
    assert run.challenge_widget_missing is False
    assert len(page.slept) == 3


async def test_a_page_still_loading_is_given_the_wait_even_with_no_widget_in_markup_yet():
    """Measured: at navigation time the widget script has not written its markup either."""

    class StillLoading(FakePage):
        def __init__(self) -> None:
            super().__init__()
            self.ready_state = "interactive"
            self.widget = FakeFrame(
                self, "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b"
            )

        async def wait_for_timeout(self, ms: int) -> None:
            self.slept.append(ms)
            if len(self.slept) == 2:
                self.frames.append(self.widget)

    page = StillLoading()
    run = ActionRun(plan=[])

    await wait_for_challenge_frame(page, run, 1_000)

    assert run.challenge_frames == [
        "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b"
    ]
    assert len(page.slept) == 2


async def test_an_article_gives_up_the_wait_as_soon_as_it_has_finished_loading():
    """The wait is for a widget that never comes; an ordinary page should not pay for all of it."""

    class QuickLoad(FakePage):
        def __init__(self) -> None:
            super().__init__()
            self.ready_state = "interactive"

        async def wait_for_timeout(self, ms: int) -> None:
            self.slept.append(ms)
            # Finished loading half a second in, without ever naming a widget.
            if len(self.slept) == 5:
                self.ready_state = "complete"

    page = QuickLoad()
    run = ActionRun(plan=[])

    await wait_for_challenge_frame(page, run, 4_000)

    assert run.challenge_frames == []
    assert run.challenge_widget_missing is False
    assert len(page.slept) == 5, page.slept


async def test_a_page_that_will_not_answer_the_probe_is_reported_as_unreadable():
    """Absence and silence are different results, and only one of them is a fact about the page."""

    class Unanswerable(FakePage):
        async def evaluate(self, script: str):
            if "readyState" in script or "innerHTML" in script:
                raise RuntimeError(
                    "Execution context was destroyed, most likely because of navigation"
                )
            return await super().evaluate(script)

    page = Unanswerable()
    run = ActionRun(plan=[])

    await wait_for_challenge_frame(page, run, 600)

    assert run.challenge_markup_unreadable is True
    assert run.challenge_widget_missing is False
    assert page.slept == []


async def test_a_widget_named_only_after_the_wait_is_still_reported_as_absent():
    """Measured on recaptcha-demo.appspot.com: api.js names its widget about two seconds in."""

    class SlowScript(FakePage):
        def __init__(self) -> None:
            super().__init__()
            self.ready_state = "interactive"
            self.asks = 0

        async def evaluate(self, script: str):
            if "innerHTML" in script:
                self.asks += 1
                # The first look sees an empty body; the script has not run yet.
                return self.asks > 1
            return await super().evaluate(script)

    page = SlowScript()
    run = ActionRun(plan=[])

    await wait_for_challenge_frame(page, run, 400)

    assert run.challenge_frames == []
    assert run.challenge_widget_missing is True
    assert page.asks == 2


async def test_a_widget_whose_document_never_arrives_is_reported_as_absent():
    """The signal that separates "aim elsewhere" from "there is nothing here to aim at"."""

    class HostsWidget(FakePage):
        def __init__(self) -> None:
            super().__init__(hosts_widget=True)

    page = HostsWidget()
    run = ActionRun(plan=[])

    await wait_for_challenge_frame(page, run, 600)

    assert run.challenge_frames == []
    assert run.challenge_widget_missing is True


async def test_a_frame_that_never_arrives_says_what_is_actually_present():
    page = FakePage()
    run = run_for([{"kind": "click", "selector": "input", "frame": "hcaptcha.com"}])

    await run_page_actions(page, run)

    assert len(run.failures) == 1
    assert run.failures[0].startswith("click no frame matches 'hcaptcha.com' (frames: ")
    assert "site.example" in run.failures[0]


# --- the vocabulary ---------------------------------------------------------------------------


def test_a_mixed_plan_splits_by_what_needs_a_real_pointer():
    script, page_plan = plan_actions(
        [{"kind": "expand", "selector": "details"}, {"kind": "click", "selector": "a"}]
    )
    assert [action.kind for action in script] == ["expand"]
    assert [action.kind for action in page_plan] == ["click"]


@pytest.mark.parametrize(
    ("action", "problem"),
    [
        ({"kind": "click_at", "x": 10}, "x and y"),
        ({"kind": "click_at", "x": 1, "y": 2, "selector": "a"}, "only x and y"),
        ({"kind": "wait_for"}, "needs text"),
        ({"kind": "wait_for", "text": "a", "x": 1}, "takes text or duration_ms only"),
        ({"kind": "click"}, "requires a CSS selector"),
        ({"kind": "click_at", "x": 1, "y": 9000}, None),
        ({"kind": "wait_for", "duration_ms": 60_000}, None),
        ({"kind": "scroll", "selector": "body"}, "no arguments"),
    ],
)
def test_the_vocabulary_refuses_targets_it_cannot_honour(action, problem):
    if problem is None:
        with pytest.raises(ValueError):
            ReadAction(**action)
    else:
        with pytest.raises(ValueError, match=problem):
            ReadAction(**action)
