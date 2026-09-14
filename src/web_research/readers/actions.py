"""Small read-only browser action vocabulary. Never accept executable code from a caller."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReadAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["expand", "tab", "load_more", "scroll"]
    selector: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def validate_selector(self):
        if self.kind != "scroll" and not self.selector:
            raise ValueError("This action requires a CSS selector")
        if self.kind == "scroll" and self.selector:
            raise ValueError("Scroll does not accept a selector")
        return self


def action_script(actions: list[dict]) -> str:
    if len(actions) > 5:
        raise ValueError("At most five read-only actions are allowed")
    validated = [ReadAction.model_validate(action).model_dump() for action in actions]
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
    })();""".replace("ACTIONS", json.dumps(validated))


def discover_actions(html_text: str) -> list[dict[str, str]]:
    """Expose only controls that the action executor can actually operate."""
    import re

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
