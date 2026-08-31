"""record.py — capture a Solari browser session as rrweb NDJSON.

Drives a real Solari browser, records it (recording=True), then downloads the
rrweb NDJSON replay to local disk. This is the "record once" half.

Usage:
    export SOLARI_API_KEY=slr_live_...
    python record.py --url https://example.com --profile login --out session.ndjson
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib


async def main() -> None:
    ap = argparse.ArgumentParser(description="Record a Solari browser session as rrweb NDJSON.")
    ap.add_argument("--url", default="https://example.com", help="page to open")
    ap.add_argument("--profile", default=None, help="reuse a Solari profile by name (skips login)")
    ap.add_argument(
        "--out", default="session.ndjson", help="where to write the decompressed NDJSON"
    )
    ap.add_argument("--actions", default=None, help="optional: path to a .py of actions to run after load")
    args = ap.parse_args()

    api_key = os.environ.get("SOLARI_API_KEY")
    if not api_key:
        raise SystemExit("SOLARI_API_KEY not set — grab one at console.getsolari.com")

    from solari_browser import Solari

    async with Solari(api_key=api_key) as solari:
        # Reuse a profile if given (log in once, replay forever).
        profile_id = None
        if args.profile:
            existing = [p for p in await solari.profiles.list() if p.name == args.profile]
            profile_id = existing[0].id if existing else (await solari.profiles.create(args.profile)).id
            print(f"[record] using profile {profile_id}")

        browser = await solari.launch(profile_id=profile_id, recording=True)
        print(f"[record] session {browser.id} (recording)")

        try:
            page = await browser.new_page()
            await page.goto(args.url)

            if args.actions:
                print(f"[record] running actions from {args.actions}")
                # Actions are a plain Python file with a `run(page)` coroutine.
                ns = {}
                exec(pathlib.Path(args.actions).read_text(encoding="utf-8"), ns)
                await ns["run"](page)

            # Give rrweb a moment to flush its batched events.
            await asyncio.sleep(3)
            session_id = browser.id
        finally:
            await browser.close()
            # close() releases the session; replay becomes available after release.
            await solari.sessions.release_and_wait(session_id)

        # Replay uploads asynchronously AFTER release. Poll for it.
        print("[record] waiting for replay upload...")
        for attempt in range(1, 16):
            await asyncio.sleep(3)
            try:
                blob = await solari.sessions.download_replay(session_id)
            except Exception as err:  # noqa: BLE001 — 404 until uploaded
                print(f"[record]   attempt {attempt}: {err}")
                continue
            # SDK hands back decompressed NDJSON (Content-Encoding honoured by httpx).
            text = blob.decode("utf-8", errors="replace")
            pathlib.Path(args.out).write_text(text, encoding="utf-8")
            lines = [ln for ln in text.splitlines() if ln.strip()]
            print(f"[record] wrote {args.out}: {len(text)} bytes, {len(lines)} rrweb events")
            return

        print("[record] no replay after ~45s — was it recorded with recording=True?")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
