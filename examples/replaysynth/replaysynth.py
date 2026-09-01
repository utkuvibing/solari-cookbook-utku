"""rrweb NDJSON -> Playwright action script synthesizer.

Reads the rrweb event stream Solari produces for a recorded browser session
(NDJSON, one event per line, gzip already decompressed by the SDK) and turns
the *interactions* into a clean, readable Playwright (Python) script that
replays them.

The core trick: rrweb gives every node a globally-unique `id` and each
interaction (click / input) references that id. We reconstruct the DOM from
FullSnapshots *and* keep it current with IncrementalSnapshot mutations (adds,
removes, attribute changes), then synthesize a robust locator per interaction.
Coordinates in rrweb are unreliable (often 0,0), so we never trust them.

Locator strategy (in priority order):
  1. #id
  2. [data-testid] / [data-test] / [data-cy] / [data-qa] / [data-id]
  3. [name] / [placeholder] / [aria-label]
  4. associated <label>   -> page.get_by_label(...)
  5. button/a text        -> page.get_by_role("button"|"link", name=...)
  6. explicit [role]      -> page.get_by_role(role, name=...)
  7. visible text         -> page.get_by_text(...)
  8. structural path      -> page.locator("form > ...")

Run as a module:
    python -m replaysynth fixture.ndjson  -> writes replay.py to stdout
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# 1. DOM reconstruction + rrweb incremental mutations
# --------------------------------------------------------------------------- #

@dataclass
class Node:
    id: int
    type: int  # 0 doc, 1 doctype, 2 element, 3 text
    tag: str = ""
    attrs: dict[str, str] = field(default_factory=dict)
    text: str = ""
    parent: Optional["Node"] = None
    children: list["Node"] = field(default_factory=list)

    @property
    def is_element(self) -> bool:
        return self.type == 2

    @property
    def is_text(self) -> bool:
        return self.type == 3


class Dom:
    """A live DOM index, seeded from a FullSnapshot and kept current by mutations.

    ReplaySynth applies the subset of rrweb IncrementalSnapshot mutation events
    that affect element identity and selector synthesis:
      - adds: new nodes inserted into the tree
      - removes: nodes detached (removed from by_id so no stale selector is used)
      - attributes: id / class / data-* / aria-* changes
      - texts: textContent changes (used for visible-text selectors)

    It does NOT attempt to implement rrweb's full mutation surface (e.g.
    characterData in the middle of a text node, or moved nodes). If a mutation
    references a parentId we don't know, the subtree is skipped — the worst
    case is a missed interaction, not a wrong selector.
    """

    def __init__(self, full_snapshot: dict) -> None:
        self.by_id: dict[int, Node] = {}
        self.root = self._build(full_snapshot["node"])

    def _build(self, raw: dict) -> Node:
        ntype = raw.get("type", 0)
        if ntype == 0:  # Document
            node = Node(id=raw.get("id", -1), type=0, tag="")
        elif ntype == 1:  # DocumentType
            node = Node(id=raw.get("id", -1), type=1, tag="")
        elif ntype == 2:  # Element
            node = Node(
                id=raw.get("id", -1),
                type=2,
                tag=(raw.get("tagName") or "").lower(),
                attrs=dict(raw.get("attributes") or {}),
            )
        elif ntype == 3:  # Text
            node = Node(id=raw.get("id", -1), type=3, tag="", text=raw.get("textContent", ""))
        else:  # CDATA / comment — treat as opaque text
            node = Node(id=raw.get("id", -1), type=3, tag="", text=str(raw.get("textContent", "")))

        self.by_id[node.id] = node
        for child in raw.get("childNodes") or []:
            child_node = self._build(child)
            child_node.parent = node
            node.children.append(child_node)
        return node

    def get(self, node_id: int) -> Optional[Node]:
        return self.by_id.get(node_id)

    # ---- mutation application --------------------------------------------- #

    def apply_mutation(self, mutation: dict) -> None:
        """Apply one rrweb IncrementalSnapshot mutation payload.

        Supported keys (rrweb 2.x):
          texts:      [{id, value}]
          attributes: [{id, attributes: {k: v | None}}]
          removes:    [{parentId, id}]
          adds:       [{parentId, nextId, node}]
        """
        for t in mutation.get("texts") or []:
            node = self.by_id.get(t.get("id"))
            if node and node.is_text:
                node.text = t.get("value", node.text)

        for a in mutation.get("attributes") or []:
            node = self.by_id.get(a.get("id"))
            if node and node.is_element:
                for k, v in (a.get("attributes") or {}).items():
                    if v is None:
                        node.attrs.pop(k, None)
                    else:
                        node.attrs[k] = str(v)

        for r in mutation.get("removes") or []:
            node = self.by_id.get(r.get("id"))
            if node:
                self._detach(node)

        for a in mutation.get("adds") or []:
            parent = self.by_id.get(a.get("parentId"))
            if parent is None:
                # Parent unknown (maybe it was removed or is inside an iframe
                # we don't track). Skip the subtree rather than guess.
                continue
            raw = a.get("node")
            if not raw:
                continue
            node = self._build(raw)
            node.parent = parent
            next_id = a.get("nextId")
            if next_id is not None:
                for i, child in enumerate(parent.children):
                    if child.id == next_id:
                        parent.children.insert(i, node)
                        break
                else:
                    parent.children.append(node)
            else:
                parent.children.append(node)

    def _detach(self, node: Node) -> None:
        """Remove a node and its descendants from the index and its parent."""
        if node.parent is not None:
            try:
                node.parent.children.remove(node)
            except ValueError:
                pass
        # Recursively drop from the id index so no stale reference is used.
        for child in list(node.children):
            self._detach(child)
        self.by_id.pop(node.id, None)
        node.parent = None


# --------------------------------------------------------------------------- #
# 2. Locator model + selector synthesis
# --------------------------------------------------------------------------- #

@dataclass
class Locator:
    """A structured Playwright locator: how to find the element, not a string."""
    kind: str  # css | role | label | text
    value: str  # CSS selector, or ARIA role, or label/text content
    name: Optional[str] = None  # accessible name for role locators

    def emit(self) -> str:
        """Emit the exact Playwright Python expression.

        Role locators deliberately do NOT use exact=True: the accessible name
        is computed by the browser (whitespace-normalized, aria-label aware),
        and our name comes from rrweb's raw textContent which often carries
        leading/trailing whitespace. Playwright's default matching is
        normalized substring — the robust choice for replay.
        """
        if self.kind == "css":
            return f'page.locator("{_py_str(self.value)}")'
        if self.kind == "role":
            if self.name:
                return f'page.get_by_role("{_py_str(self.value)}", name="{_py_str(self.name)}")'
            return f'page.get_by_role("{_py_str(self.value)}")'
        if self.kind == "label":
            return f'page.get_by_label("{_py_str(self.value)}")'
        if self.kind == "text":
            return f'page.get_by_text("{_py_str(self.value)}")'
        raise ValueError(f"unknown locator kind: {self.kind}")


# HTML input types that act as buttons (not text fields).
_BUTTON_INPUT_TYPES = {"submit", "button", "reset", "image"}


def _safe_css(value: str) -> str:
    """Escape a value for use inside a CSS attribute selector value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _py_str(s: str) -> str:
    """Escape a string for embedding inside a double-quoted Python string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def _visible_text(node: Node) -> str:
    """Best-effort visible text for a node (for text= based selectors)."""
    if node.is_text:
        return node.text.strip()
    return "".join(c.text for c in node.children if c.is_text and c.text).strip()


def _all_text(node: Node) -> str:
    """Recursively collect visible text (used for accessible names)."""
    if node.is_text:
        return node.text
    return "".join(_all_text(c) for c in node.children)


def _label_for(node: Node) -> Optional[str]:
    """A label[for=X] whose `for` matches this control's id/name."""
    form = None
    p = node.parent
    while p:
        if p.is_element and p.tag == "form":
            form = p
            break
        p = p.parent
    if form is None or not node.attrs:
        return None
    target = node.attrs.get("id") or node.attrs.get("name")
    if not target:
        return None
    for cand in form.children:
        if cand.is_element and cand.tag == "label":
            for_ = cand.attrs.get("for")
            if for_ == target:
                txt = _visible_text(cand)
                return txt or None
    return None


def _display_text(node: Node) -> Optional[str]:
    """Text content of a button/link/a used for an accessible-name locator."""
    # Collapse whitespace like the browser's accessible-name computation does —
    # rrweb's raw textContent often carries leading/trailing whitespace/newlines.
    txt = " ".join(_all_text(node).split())
    if not txt:
        return None
    # Don't use long/complex text as a selector; keep role-ish phrases short.
    return txt if len(txt) <= 60 else txt.split()[0]


def synthesize(node: Node) -> Optional[Locator]:
    """Pick the most robust locator for a node, deterministic and readable."""
    if not node.is_element:
        return None

    # Tier 1: durable, unique id attribute.
    if node.attrs.get("id"):
        return Locator("css", f"#{_safe_css(node.attrs['id'])}")

    # Tier 2: common test-hook / component attributes.
    for attr in ("data-testid", "data-test", "data-cy", "data-qa", "data-id"):
        if node.attrs.get(attr):
            return Locator("css", f'[{attr}="{_safe_css(node.attrs[attr])}"]')

    # Tier 3: stable structural control attrs (name / placeholder / aria-label).
    for attr in ("name", "placeholder", "aria-label"):
        if node.attrs.get(attr) and node.attrs[attr] not in ("", "undefined"):
            return Locator("css", f'[{attr}="{_safe_css(node.attrs[attr])}"]')

    # Tier 4: accessible-name via associated <label>.
    label = _label_for(node)
    if label:
        return Locator("label", label)

    # Tier 5: buttons and links by accessible role + visible text.
    if node.tag in ("button", "a"):
        role = "button" if node.tag == "button" else "link"
        text = _display_text(node)
        if text:
            return Locator("role", role, name=text)
        return Locator("role", role)

    # Tier 5b: input[type=submit|button|reset|image] are buttons with a value label.
    if node.tag == "input" and node.attrs.get("type") in _BUTTON_INPUT_TYPES:
        value = node.attrs.get("value", "").strip()
        if value:
            return Locator("role", "button", name=value)
        return Locator("role", "button")

    # Tier 5c: explicit ARIA role.
    role = node.attrs.get("role")
    if role:
        return Locator("role", role, name=_display_text(node) or None)

    # Tier 6: visible text on text-carrying elements.
    text = _display_text(node)
    if text and node.tag in ("span", "summary", "li", "h1", "h2", "h3", "h4", "p", "td", "th", "div"):
        return Locator("text", text)

    # Tier 7: structural path — tag + nth-of-type within its parent.
    return Locator("css", structural_path(node))


def structural_path(node: Node) -> str:
    """Fallback: unique-ish path of tag names + nth-of-type.

    Does NOT climb to an ancestor's id — that would target the ancestor (e.g. a
    form) instead of the clicked element. The path is purely the element's own
    tag/position within its parent.
    """
    chain: list[str] = []
    cur: Optional[Node] = node
    while cur is not None and cur.is_element and cur.parent is not None:
        parent_children = [c for c in cur.parent.children if c.is_element and c.tag == cur.tag]
        idx = parent_children.index(cur) + 1 if parent_children else 1
        chain.append(f"{cur.tag}:nth-of-type({idx})")
        cur = cur.parent
    chain.reverse()
    return " > ".join(chain) if chain else (cur.tag if cur else "")


# --------------------------------------------------------------------------- #
# 3. Interaction extraction
# --------------------------------------------------------------------------- #

@dataclass
class Action:
    kind: str  # click | fill | check | uncheck
    locator: Locator
    value: str = ""
    comment: str = ""
    field: str = ""  # field name for secrets resolution (password/redacted fills)


def _is_incremental_prefix(prev: str, curr: str) -> bool:
    """True if curr looks like an incremental typing step toward prev.

    e.g. prev="u", curr="ut" -> True (we're still typing the same field)
         prev="utku", curr="utku" -> True (no-op, same value)
         prev="hello", curr="world" -> False (a real change, not a prefix)
    """
    if prev == curr:
        return True
    if not prev or not curr:
        return False
    return curr.startswith(prev) or prev.startswith(curr)


def extract_actions(events: list[dict]) -> list[Action]:
    """Turn an rrweb stream into a minimal, read-only action list.

    Handles multiple FullSnapshots (page navigation renumbers node ids) and
    IncrementalSnapshot mutations (dynamic DOM changes after the snapshot).
    Each interaction is resolved against the DOM state at that point in the
    event stream.
    """
    acts: list[Action] = []
    dom: Optional[Dom] = None
    # Track the last fill per field name (not per node id) so we can collapse
    # incremental typing into one final fill. A click/navigation resets the
    # "same logical interaction" window.
    last_fill: dict[str, Action] = {}  # field_name -> the Action we emitted

    for ev in events:
        etype = ev.get("type")

        if etype == 2:  # FullSnapshot — new id namespace.
            dom = Dom(ev["data"])
            last_fill.clear()
            continue

        if etype != 3:  # IncrementalSnapshot
            continue

        data = ev.get("data", {})
        source = data.get("source")

        if source == 0 and dom is not None:  # Mutation
            dom.apply_mutation(data)
            continue

        if source == 2:  # MouseInteraction
            mtype = data.get("type")
            # 2 = Click. rrweb reports the DEEPEST hit-test element (often an icon
            # or inner span); walk up to a real interactive ancestor (button/a).
            if mtype == 2:
                node = _resolve(dom, data.get("id", -1))
                node = _clickable_ancestor(node)
                if node is None:
                    continue
                loc = synthesize(node)
                if loc:
                    # A click ends any incremental-typing sequence.
                    last_fill.clear()
                    acts.append(Action(kind="click", locator=loc,
                                       comment=_visible_text(node)[:40]))
            # ignore mousemove/down/up/over — noise

        elif source == 5:  # Input
            node = _resolve(dom, data.get("id", -1))
            if node is None or not node.is_element:
                continue
            value = data.get("text", "")
            is_checked = data.get("isChecked", False)
            attr_type = (node.attrs.get("type") or "").lower()
            field_name = node.attrs.get("name") or node.attrs.get("id") or ""

            if attr_type in ("checkbox", "radio"):
                kind = "check" if is_checked else "uncheck"
                loc = synthesize(node)
                if loc:
                    last_fill.clear()
                    acts.append(Action(kind=kind, locator=loc,
                                       comment=field_name[:30]))
                continue

            # Text-like input. rrweb masks password fields to a run of '*'.
            is_password = attr_type == "password" or (value and set(value) <= {"*"})
            emit_value = "[REDACTED]" if is_password else value
            loc = synthesize(node)
            if not loc:
                continue

            prev = last_fill.get(field_name)
            if prev is not None:
                if is_password and prev.value == "[REDACTED]":
                    # Same password field, still typing — all values are '*'
                    # runs, so keep the single redacted action.
                    continue
                if _is_incremental_prefix(prev.value, value):
                    # Same field, still typing — replace the previous action's
                    # value instead of emitting a new one.
                    prev.value = emit_value
                    continue

            act = Action(kind="fill", locator=loc, value=emit_value,
                         comment=field_name[:30], field=field_name)
            acts.append(act)
            last_fill[field_name] = act

        elif source == 3:  # Scroll — ignore
            continue

    return acts


def _resolve(dom: Optional[Dom], node_id: int) -> Optional[Node]:
    if dom is not None:
        return dom.get(node_id)
    return None


def _clickable_ancestor(node: Optional[Node]) -> Optional[Node]:
    """Walk up from a hit-tested leaf to the nearest interactive element.

    rrweb records the DEEPEST element under the pointer — often an <i> icon or a
    <span> inside a <button>. Clicking that leaf directly is valid but produces
    brittle selectors; climb to the enclosing button/a/input[type=submit|button]
    instead.
    """
    if node is None:
        return None
    cur: Optional[Node] = node
    while cur is not None:
        if cur.is_element:
            if cur.tag in ("button", "a", "select", "textarea"):
                return cur
            if cur.tag == "input" and (cur.attrs.get("type") or "").lower() in _BUTTON_INPUT_TYPES:
                return cur
        cur = cur.parent
    return node  # nothing better; use the leaf


# --------------------------------------------------------------------------- #
# 4. Code generation
# --------------------------------------------------------------------------- #

def actions_body(acts: list[Action]) -> list[str]:
    """The core action statements, as a list of code lines (no wrapper)."""
    lines: list[str] = []
    for act in acts:
        loc = act.locator.emit()
        if act.kind == "click":
            if act.comment:
                lines.append(f"        # click {act.comment}")
            lines.append(f"        await {loc}.click()")
        elif act.kind == "fill":
            if act.value == "[REDACTED]":
                fill_expr = f'secrets.get("{_py_str(act.field)}", "[REDACTED]")'
            else:
                fill_expr = f'"{_py_str(act.value)}"'
            lines.append(f"        # fill {act.comment or 'input'}")
            lines.append(f"        await {loc}.fill({fill_expr})")
        elif act.kind == "check":
            lines.append(f"        # check {act.comment or 'checkbox'}")
            lines.append(f"        await {loc}.check()")
        elif act.kind == "uncheck":
            lines.append(f"        # uncheck {act.comment or 'checkbox'}")
            lines.append(f"        await {loc}.uncheck()")
        lines.append("")
    return lines


def to_python(acts: list[Action], start_url: str, *, standalone: bool = True) -> str:
    """Generate a Playwright script.

    `standalone=True` emits a self-contained `main()` that launches its own
    browser. `standalone=False` emits only a `replay_on(page)` coroutine that
    replays on a page handed to it (e.g. a Solari cloud-browser page), so a
    driver can attach a profile and re-run the flow.
    """
    body = actions_body(acts)

    if not standalone:
        # Standalone main() uses 8-space body indent; replay_on uses 4-space.
        # Re-indent the 8-space body lines down to 4 space so they sit at the
        # function-body level (NOT inside the `if secrets is None` block).
        reduced = []
        for ln in body:
            if ln.startswith("        "):
                reduced.append("    " + ln[8:])
            elif ln.strip():
                reduced.append(ln)
            else:
                reduced.append(ln)
        lines = ['"""Auto-generated by replaysynth — replay on a supplied page."""']
        lines.append("")
        lines.append("async def replay_on(page, secrets=None):")
        lines.append("    # secrets: Optional dict of {field_name: value}. rrweb masks")
        lines.append("    # password fields, so supply the real value at replay time.")
        lines.append("    if secrets is None:")
        lines.append("        secrets = {}")
        lines.extend(reduced)
        lines.append("    return page")
        lines.append("")
        return "\n".join(lines)

    lines: list[str] = []
    lines.append('"""Auto-generated by replaysynth — replay a recorded Solari session."""')
    lines.append("")
    lines.append("import asyncio")
    lines.append("from playwright.async_api import async_playwright")
    lines.append("")
    lines.append("")
    lines.append("async def main():")
    lines.append("    async with async_playwright() as p:")
    lines.append("        browser = await p.chromium.launch()")
    lines.append("        page = await browser.new_page()")
    lines.append("")
    lines.append(f'        await page.goto("{_py_str(start_url)}")')
    lines.append("")
    lines.extend(body)
    lines.append("        await browser.close()")
    lines.append("")
    lines.append("")
    lines.append("if __name__ == '__main__':")
    lines.append("    asyncio.run(main())")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Synthesize a Playwright script from rrweb NDJSON.")
    ap.add_argument("input", help="path to .ndjson (gzip-readable: pass already-decompressed, or a .gz)")
    ap.add_argument("--out", help="write to file instead of stdout")
    ap.add_argument("--url", help="override start URL (default: from Meta event)")
    ap.add_argument("--standalone", action="store_true", default=True,
                    help="emit a self-contained main() (default); pass --replay-only for a replay_on(page)")
    ap.add_argument("--replay-only", action="store_true",
                    help="emit only a replay_on(page) coroutine for a driver to run on its own page")
    args = ap.parse_args()

    events: list[dict] = []
    path = args.input
    if path.endswith(".gz"):
        import gzip
        raw = gzip.open(path, "rt", encoding="utf-8")
    else:
        raw = open(path, "r", encoding="utf-8")
    with raw as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    # Find the FullSnapshot (type 2) and the start URL.
    full = None
    start_url = args.url or ""
    for ev in events:
        if ev.get("type") == 2 and full is None:
            full = ev
        if ev.get("type") == 4 and not start_url:
            start_url = ev.get("data", {}).get("href", "")
    if full is None:
        print("ERROR: no FullSnapshot (type=2) found", file=sys.stderr)
        return 1
    if not start_url:
        start_url = "about:blank"

    acts = extract_actions(events)
    code = to_python(acts, start_url, standalone=not args.replay_only)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(code)
        print(f"wrote {len(code)} bytes, {len(acts)} actions -> {args.out}")
    else:
        print(code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
