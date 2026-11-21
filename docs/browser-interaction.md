# Interacting with a rendered page

`read_url` normally only looks. Some pages hide their content behind a control, and some refuse to
answer until a challenge widget is satisfied. This page describes what a read may now do about that,
what it reports when it does, and what the isolation model still refuses.

A rendered read costs several seconds. A read that also interacts costs more, so nothing here runs
unless the caller asks for it or the page looks challenged.

## The action vocabulary

| `kind` | Where it runs | Fields | Notes |
| --- | --- | --- | --- |
| `expand`, `tab`, `load_more`, `scroll` | in-page script | `selector` | Read-only. These are the only kinds offered in `available_actions`. |
| `click` | Playwright, main frame or a named frame | `selector`, `frame` | Trusted input events. May navigate. |
| `click_at` | Playwright, viewport | `x`, `y` | Moves the pointer in steps before pressing, because a challenge may grade the travel. Coordinates come from a `visual` read. |
| `wait_for` | main frame or a named frame | `text`, `duration_ms` | Either field, or both; with both, the duration bounds the text search. |

Discovery stays read-only. The browser never offers a `click`, because a control that looks like a
tab may submit a form or start a purchase. The caller asks for a click explicitly and reads the
result.

A click inside a `<form>` is allowed and reported as `action_may_submit_form`; a click that changes
the document is reported as `action_navigated:<url>` and the read settles on the new page. An action
that did not happen is reported as `action_failed:<what failed>` and **the page is still returned**.
Failing the read instead would drop a rendered document onto the plain HTTP reader, which is the
outcome a browser escalation was meant to avoid.

## Challenge reporting

Strong challenge signals get a bounded wait before anything is decided. Cloudflare's own
interstitial passes in about two seconds; a managed check takes five to six. The wait is capped by
`WEB_SEARCH_BROWSER_CHALLENGE_GRACE_SECONDS` (default 8, `0` reports immediately).

| Warning | Meaning |
| --- | --- |
| `challenge_waited:<ms>`, `challenge_cleared`, `challenge_still_present` | The wait and its outcome. |
| `challenge_frame:<url>` | A widget's own document, named so a later action can aim at it with `frame`. |
| `challenge_widget_frame_absent` | The page named a widget but no widget document arrived. |
| `challenge_markup_unreadable` | The page would not answer the question, so absence is not claimed. |

A widget lives in its own document, usually on another origin, and a main-frame selector cannot see
into it. That is why `frame` exists and why these URLs are reported rather than guessed at. A
per-read discovery wait is 1.2 seconds, deliberately shorter than the time Google's script takes to
attach its iframe; a click or wait that was told to find a frame waits up to 2.5 seconds for it. If
the report comes back empty on a page you can see a widget on, aim at the stable name instead:
`recaptcha/api2` or `challenges.cloudflare.com` are substrings, not exact matches.

## Measured on 21 November 2026, in the isolated runtime

End to end, through the MCP tool, container and browser start included: an ordinary page renders in
about 5.2 seconds (`example.com`) and a heavy one in about 6.6 (`en.wikipedia.org`). Neither pays for
the challenge wait unless the page looks challenged: an article gives up the frame wait as soon as it
has finished loading without naming a widget.

* `recaptcha-demo.appspot.com/recaptcha-v2-checkbox.php` reports
  `challenge_frame:https://www.google.com/recaptcha/api2/anchor` and `…/bframe`. A click on
  `.recaptcha-checkbox-border` in `frame: "recaptcha/api2"` lands in three of three attempts. It
  landed in one of three before the click was reordered to wait for the element first, because a
  widget document can exist a moment before the widget draws anything inside it.
* Verification text (`Verification succeeded`) never appeared in four attempts, and the screenshot
  says why: the widget's own footnote reads "This site is exceeding reCAPTCHA Enterprise free
  quota." That is Google's demo key, not this reader's fault, and it means this target cannot
  demonstrate a pass in either direction. Treat the demo as a test of reaching and clicking a widget,
  which it does prove, and not of clearing one.
* `2captcha.com/demo/cloudflare-turnstile` now returns 200 with `challenge_waited:3` and
  `challenge_cleared`. Before the broker reply-buffer fix (pages over about 118 KB) this read failed
  outright, so the challenge path was never reached.

## What isolation still denies a challenge

The widget is a script written by a company that profits from telling it apart from a person, and it
runs inside a container that exists to leak as little as possible.

* reCAPTCHA's Web Worker requests bypass Playwright's route interception, then fail closed against
  `--network=none`. The worker cannot be observed or fulfilled, only missed.
* WebSockets are closed, service workers blocked, downloads refused. Fonts are skipped on reads that
  only want text; a read that will click, or draw a screenshot for a model to aim at, gets them.
  Media is always skipped.
* The rendered profile claims Windows Chrome while egress TLS is Apple SecureTransport via libcurl,
  with no GREASE and no ECH. A listener that compares layers sees one browser speaking two
  dialects. See [transport-fingerprints.md](transport-fingerprints.md).
* Every read leaves through one shared hosting address, which is a well-known address by
  construction. See [anonymity.md](anonymity.md).
* Credentials never enter the container, so content behind a login stays out of reach regardless of
  what the browser can click.
* Cloudflare documents automated browsers as unsupported for solving production challenges, and
  publishes `cf-mitigated: challenge` as the reliable signal. This reader uses the signal and reports
  the remaining block. It does not promise to solve a CAPTCHA.

## What would move it further

Reaching and clicking a widget is now available. Deciding *what* to click, and clearing checks that
need an image grid or an image captcha, is the remaining gap. The options, roughly in order of cost:

1. A solver service. Its API is a bounded `POST`, which the current policy already carries, but it
   adds an allowed destination and tells that service which pages were read.
2. A local model (OCR for text captchas, a small vision model for grids) inside the container, which
   keeps the disclosure at the cost of image and maintenance weight.
3. Making the rendered profile claim the platform the broker actually speaks from, which removes one
   layer mismatch without any new permission.
4. An exit address with a reputation of its own, which is about who is asking rather than what the
   browser can do.

None of these is required for the common case that motivated this work: a page whose real content is
one click away, in a frame, from the text the first read returned.
