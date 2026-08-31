"""rrweb NDJSON -> Playwright action script synthesizer.

Reads the rrweb event stream Solari produces for a recorded browser session
(NDJSON, one event per line, gzip already decompressed by the SDK) and turns
the *interactions* into a clean, readable Playwright (Python) script that
replays them.

The core trick: rrweb gives every node a globally-unique `id` and each
interaction (click / input) references that id. We reconstruct the DOM from
the FullSnapshot, index nodes by id, and synthesize a robust CSS selector per
interaction by walking a fallback chain (id -> data-testid -> name/type ->
aria-label -> visible text/role -> structural path). Coordinates in rrweb are
unreliable (often 0,0), so we never trust them.

Run as a module:
    python -m replaysynth fixture.ndjson  -> writes replay.py to stdout
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# 1. DOM reconstruction from the FullSnapshot
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


# --------------------------------------------------------------------------- #
# 2. Selector synthesis (the whole point)
# --------------------------------------------------------------------------- #

def _safe_css(value: str) -> str:
    """Escape a value for use inside a CSS attribute selector value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _visible_text(node: Node) -> str:
    """Best-effort visible text for a node (for text= based selectors)."""
    if node.is_text:
        return node.text.strip()
    return "".join(c.text for c in node.children if c.is_text and c.text).strip()


def _argmax_int(attrs: dict[str, str]) -> Optional[str]:
    """Longest single-space token as a proxy for a stable-looking selector attr."""
    for key in ("data-cy", "data-test", "data-testid"):
        if attrs.get(key):
            candidates = attrs[key].split()
            if candidates:
                return max(candidates, key=len)
    return None


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
    """Text content of a button/link/a used for a text= selector."""
    txt = _visible_text(node)
    if not txt:
        return None
    # Don't use long/complex text as a selector; keep role-ish phrases short.
    return txt if len(txt) <= 40 else txt.split()[0]


def synthesize(node: Node) -> Optional[str]:
    """Pick the most robust CSS selector for a node, deterministic and readable."""
    if not node.is_element:
        return None

    # Tier 1: durable, unique id attribute.
    if node.attrs.get("id"):
        return f"#{_safe_css(node.attrs['id'])}"

    # Tier 2: common test-hook / component attributes.
    for attr in ("data-testid", "data-test", "data-cy", "data-qa", "data-id"):
        if node.attrs.get(attr):
            return f'[{attr}="{_safe_css(node.attrs[attr])}"]'

    # Tier 3: stable structural control attrs (name / type / placeholder).
    for attr in ("name", "placeholder", "aria-label", "data-name", "data-field"):
        if node.attrs.get(attr) and node.attrs[attr] not in ("", "undefined"):
            return f'[{attr}="{_safe_css(node.attrs[attr])}"]'

    # Tier 4: accessible-name via associated <label>.
    label = _label_for(node)
    if label:
        return f'get_by_label("{_py_str(label)}")'  # placeholder, resolved later

    # Tier 5: visible text for role-ish elements (button, a, span with text).
    text = _display_text(node)
    if node.tag in ("button", "a"):
        # Buttons/links: prefer accessible-name (visible text OR role). If the
        # element has no visible text (e.g. a <button> containing only an icon),
        # fall back to an exact role locator — the most robust option.
        role = "button" if node.tag == "button" else "link"
        if text:
            return f'get_by_role("{role}", name="{_py_str(text)}", exact=True)'
        # No text: default to the first matching role; usually unique enough.
        return f'get_by_role("{role}")'
    if text and node.tag in ("span", "summary", "li", "h1", "h2", "h3", "p", "td"):
        return f'text="{_py_str(text)}"'

    # Tier 6: role-based (only for explicit roles we care about).
    role = node.attrs.get("role")
    if role:
        return f'get_by_role("{_safe_css(role)}", name="{_py_str(_display_text(node) or "")}")'

    # Tier 7: structural path — tag + nth-of-type within its parent.
    return structural_path(node)


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


def _py_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _safe_py_quote(s: str) -> str:
    """Quote a string for embedding inside a Playwright CSS selector value."""
    # Playwright CSS :has-text() wants the raw text; escape CSS-wise (no quotes needed
    # for the arg when we wrap in double quotes at the py level — but colons/commas break it).
    return s.replace("\\", "\\\\").replace('"', '\\"')


# --------------------------------------------------------------------------- #
# 3. Interaction extraction
# --------------------------------------------------------------------------- #

@dataclass
class Action:
    kind: str  # click | fill | check | uncheck | goto
    selector: str = ""
    value: str = ""
    comment: str = ""
    url: str = ""  # for goto
    field: str = ""  # field name for secrets resolution (password/redacted fills)


def extract_actions(events: list[dict], doms: list, start_url: str) -> list[Action]:
    """Turn an rrweb stream into a minimal, read-only action list.

    `doms` is a list of (event_index, Dom) pairs, one per FullSnapshot. Each
    interaction is resolved against the MOST RECENT FullSnapshot that preceded
    it — rrweb renumbers node ids on every page load, so ids from one snapshot
    are meaningless in another. We walk the interaction's id against the latest
    preceding DOM, falling back to earlier ones if the id is a content node.
    """
    acts: list[Action] = []
    seen_fill: dict[int, str] = {}  # node id -> last fill value (collapse repeats)
    latest_dom: Optional[Dom] = None
    latest_idx: int = -1

    for ev in events:
        etype = ev.get("type")
        if etype == 2:
            # A new FullSnapshot begins a new id namespace.
            latest_dom = Dom(ev["data"])
            latest_idx = latest_dom.root.id if latest_dom.root else -1
            continue
        if etype != 3:
            continue
        data = ev.get("data", {})
        source = data.get("source")

        if source == 2:  # MouseInteraction
            mtype = data.get("type")
            # 2 = Click. rrweb reports the DEEPEST hit-test element (often an icon
            # or inner span); walk up to a real interactive ancestor (button/a).
            if mtype == 2:
                node = _resolve(latest_dom, data.get("id", -1))
                node = _clickable_ancestor(node, latest_dom)
                if node is None:
                    continue
                sel = synthesize(node)
                if sel:
                    acts.append(Action(kind="click", selector=sel, comment=_visible_text(node)[:40]))
            # ignore mousemove/down/up/over — noise

        elif source == 5:  # Input
            node = _resolve(latest_dom, data.get("id", -1))
            if node is None or not node.is_element:
                continue
            value = data.get("text", "")
            is_checked = data.get("isChecked", False)
            attr_type = node.attrs.get("type", "")
            if attr_type == "checkbox" or attr_type == "radio":
                kind = "check" if is_checked else "uncheck"
                acts.append(Action(kind=kind, selector=synthesize(node) or "", comment=_visible_text(node)[:30]))
            else:
                prev = seen_fill.get(data.get("id"))
                if prev != value:
                    # rrweb masks password fields — the value is a run of '*'
                    # (e.g. '****'). Mark it so the caller can supply the real
                    # secret at replay time (forever-login without re-auth).
                    if attr_type == "password" or set(value) <= {"*"} and value:
                        val = "[REDACTED]"
                    else:
                        val = value
                    sel = synthesize(node) or ""
                    field_name = node.attrs.get("name") or node.attrs.get("id") or ""
                    acts.append(Action(kind="fill", selector=sel, value=val,
                                       comment=(node.attrs.get("id") or node.attrs.get("name") or "")[:30],
                                       field=field_name))
                seen_fill[data.get("id")] = value

        elif source == 3:  # Scroll — ignore
            continue

    return acts


def _resolve(dom: Optional[Dom], node_id: int) -> Optional[Node]:
    """Resolve a node id against the latest DOM, else walk back isn't possible
    here (single passed dom) — but if absent, drop a hint by searching content."""
    if dom is not None:
        return dom.get(node_id)
    return None


def _clickable_ancestor(node: Optional[Node], dom: Optional[Dom]) -> Optional[Node]:
    """Walk up from a hit-tested leaf to the nearest interactive element.

    rrweb records the DEEPEST element under the pointer — often an <i> icon or a
    <span> inside a <button>. Clicking that leaf directly is valid but produces
    brittle selectors; climb to the enclosing button/a instead.
    """
    if node is None:
        return None
    cur = node
    while cur is not None:
        if cur.is_element and cur.tag in ("button", "a", "input[type=submit]", "select", "textarea"):
            # normalize a bare tag check
            if cur.tag == "button" or cur.tag == "a":
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
        if act.kind == "goto":
            lines.append(f'        await page.goto("{_py_str(act.url)}")')
        elif act.kind == "click":
            sel = act.selector
            if act.comment:
                lines.append(f"        # click {act.comment}")
            if sel.startswith("text="):
                txt = _safe_py_quote(sel[5:])
                lines.append(f'        await page.locator(":has-text({txt})").click()')
            elif sel.startswith("get_by_label"):
                inner = sel[len("get_by_label("):-1]
                lines.append(f'        await page.locator("label:has-text({inner})").click()')
            elif sel.startswith("get_by_role"):
                parts = sel[len("get_by_role("):-1]
                lines.append(f'        await page.get_by_role({parts}).click()')
            else:
                lines.append(f'        await page.locator("{_py_str(sel)}").click()')
        elif act.kind == "fill":
            # rrweb masks passwords to '[REDACTED]' — at replay time pull the real
            # value from the caller-supplied `secrets` dict (keyed by field name).
            if act.value == "[REDACTED]":
                fill_expr = f'secrets.get("{_py_str(act.field)}", "[REDACTED]")'
            else:
                fill_expr = f'"{_py_str(act.value)}"'
            lines.append(f'        # fill {act.comment or "input"}')
            css = act.selector if not act.selector.startswith(("text=", "get_by_")) else None
            if css:
                lines.append(f'        await page.locator("{_py_str(css)}").fill({fill_expr})')
            else:
                lines.append(f'        await page.locator("{_py_str(act.selector)}").fill({fill_expr})')
        elif act.kind == "check":
            lines.append(f"        # check {act.comment or 'checkbox'}")
            lines.append(f'        await page.locator("{_py_str(act.selector)}").check()')
        elif act.kind == "uncheck":
            lines.append(f"        # uncheck {act.comment or 'checkbox'}")
            lines.append(f'        await page.locator("{_py_str(act.selector)}").uncheck()')
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

    # Build one Dom per FullSnapshot so interactions resolve against the right
    # id-space epoch.
    doms: list[Dom] = []
    for ev in events:
        if ev.get("type") == 2:
            doms.append(Dom(ev["data"]))

    dom = doms[-1] if doms else None
    acts = extract_actions(events, doms, start_url)

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
