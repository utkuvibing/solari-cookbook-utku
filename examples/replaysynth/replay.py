"""replay.py — download a recorded Solari session, synthesize it, replay it.

Reads an rrweb NDJSON replay (already on disk, from `record.py --out`), turns it
into a Playwright action list via `replaysynth`, then replays it against a FRESH
Solari cloud-browser session. If `--profile` is given, the login already stored
in that profile is reused, so authenticated flows replay forever without
re-authenticating.

Usage:
    export SOLARI_API_KEY=slr_live_...
    python replay.py --replay session.ndjson --profile login --url https://example.com
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import pathlib
import sys


def _load_replay_on(path: str):
    """Import a synthesized `replay_on(page)` from a module file."""
    spec = importlib.util.spec_from_file_location("synthesized_replay", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.replay_on


def _synth(replay_path: pathlib.Path, out_path: pathlib.Path, url: str | None = None) -> None:
    """Synthesize replay_on(page) from an rrweb NDJSON file."""
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    import replaysynth

    events: list[dict] = []
    with replay_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(replaysynth.json.loads(line))

    full = next((e for e in events if e.get("type") == 2), None)
    if full is None:
        raise SystemExit("replay: no FullSnapshot (type=2) in the replay")

    start_url = url
    if not start_url:
        start_url = next(
            (e.get("data", {}).get("href", "") for e in events if e.get("type") == 4), ""
        ) or "https://example.com"

    # One Dom per FullSnapshot — rrweb renumbers node ids per page load.
    doms = [replaysynth.Dom(e["data"]) for e in events if e.get("type") == 2]
    acts = replaysynth.extract_actions(events, doms, start_url)
    code = replaysynth.to_python(acts, start_url, standalone=False)
    out_path.write_text(code, encoding="utf-8")
    print(f"[replay] synthesized {len(acts)} actions -> {out_path}")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Replay a recorded Solari session.")
    ap.add_argument("--replay", required=True, help="path to the .ndjson replay")
    ap.add_argument("--profile", default=None, help="Solari profile name — reuse login, skip re-auth")
    ap.add_argument("--url", default=None, help="start URL (defaults to the one recorded in the replay)")
    ap.add_argument("--synth", default="generated_replay.py", help="where to write the synthesized script")
    ap.add_argument("--stealth", action="store_true", help="launch stealthy (proxy/captcha-capable)")
    ap.add_argument("--secret", action="append", default=[], metavar="NAME=VALUE",
                    help="secret to substitute for a [REDACTED] field (e.g. --secret password=hunter2). Repeatable.")
    args = ap.parse_args()

    # Parse --secret NAME=VALUE pairs into the dict handed to replay_on(page, secrets).
    secrets: dict[str, str] = {}
    for item in args.secret:
        if "=" in item:
            k, v = item.split("=", 1)
            secrets[k] = v

    replay_path = pathlib.Path(args.replay)
    script_path = pathlib.Path(args.synth)
    _synth(replay_path, script_path, args.url)
    replay_on = _load_replay_on(str(script_path))

    api_key = os.environ.get("SOLARI_API_KEY")
    if not api_key:
        raise SystemExit("SOLARI_API_KEY not set — grab one at console.getsolari.com")
    from solari_browser import Solari

    async with Solari(api_key=api_key) as solari:
        profile_id = None
        if args.profile:
            existing = [p for p in await solari.profiles.list() if p.name == args.profile]
            profile_id = existing[0].id if existing else (await solari.profiles.create(args.profile)).id
            print(f"[replay] profile {profile_id} — reusing login, skipping re-auth")
        else:
            print("[replay] no profile — recording WITHOUT stored login")

        browser = await solari.launch(profile_id=profile_id, stealth=args.stealth)
        print(f"[replay] session {browser.id}")
        try:
            page = await browser.new_page()
            start = args.url
            if not start:
                import json as _json
                for line in replay_path.read_text(encoding="utf-8").splitlines():
                    try:
                        ev = _json.loads(line)
                    except Exception:
                        continue
                    if ev.get("type") == 4:
                        start = ev.get("data", {}).get("href", "")
                        break
            await page.goto(start or "https://example.com")
            print("[replay] running synthesized actions")
            await replay_on(page, secrets=secrets)
            print("[replay] finished")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
