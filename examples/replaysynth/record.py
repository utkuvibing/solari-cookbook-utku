"""record.py — capture a Solari browser session as rrweb NDJSON.

Drives a real Solari browser, records it (recording=True), then downloads the
rrweb NDJSON replay to local disk. This is the "record once" half.

Two modes:

  --manual    launch the session, print the live-view URL for the Solari
              console, and wait for a human to interact with it. When the
              human presses ENTER here, the session is released and the
              recording downloaded.

  --actions   run a deterministic Playwright script (see actions_login.py)
              against the recorded session. Useful for repeatable tests.

Usage:
    export SOLARI_API_KEY=slr_live_...
    python record.py --url https://example.com --manual --out session.ndjson
    python record.py --url https://example.com --actions actions_login.py --out session.ndjson
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys


async def _release_and_download(solari, session_id: str, out_path: str) -> None:
    """Release a session, wait for the async replay upload, and save the NDJSON."""
    # close() already released the session; replay becomes available after release.
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
        pathlib.Path(out_path).write_text(text, encoding="utf-8")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        print(f"[record] wrote {out_path}: {len(text)} bytes, {len(lines)} rrweb events")
        return

    print("[record] no replay after ~45s — was it recorded with recording=True?")
    raise SystemExit(1)


async def main() -> None:
    ap = argparse.ArgumentParser(description="Record a Solari browser session as rrweb NDJSON.")
    ap.add_argument("--url", default="https://example.com", help="page to open")
    ap.add_argument("--profile", default=None, help="reuse a Solari profile by name (skips login)")
    ap.add_argument(
        "--out", default="session.ndjson", help="where to write the decompressed NDJSON"
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--manual", action="store_true",
                      help="open the session for a human to drive (live view in Solari console)")
    mode.add_argument("--actions", default=None,
                      help="path to a .py of Playwright actions to run after load")
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
        session_id = browser.id
        print(f"[record] session {session_id} (recording)")

        try:
            page = await browser.new_page()
            await page.goto(args.url)

            if args.manual:
                # Solari browsers don't expose a public stream URL the way
                # desktops do. The supported manual workflow is:
                #   1. Open the session in the Solari console live view.
                #   2. Click through the workflow yourself.
                #   3. Press ENTER here to finish.
                console_url = f"https://console.getsolari.com/sessions/{session_id}"
                print()
                print("  ┌─────────────────────────────────────────────────────────────┐")
                print("  │  MANUAL RECORDING — drive the browser yourself             │")
                print("  └─────────────────────────────────────────────────────────────┘")
                print()
                print(f"  1. Open the live view in your browser:")
                print(f"       {console_url}")
                print()
                print(f"  2. Interact with the page (clicks, typing, navigation).")
                print(f"     Everything is recorded as rrweb events.")
                print()
                print(f"  3. When you're done, press ENTER here to stop recording.")
                print()
                # Use a thread for input() so the asyncio loop stays alive.
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, lambda: input("  [press ENTER to finish] "))
                print()
                print("[record] manual session finished")
            elif args.actions:
                print(f"[record] running actions from {args.actions}")
                # Actions are a plain Python file with a `run(page)` coroutine.
                ns = {}
                exec(pathlib.Path(args.actions).read_text(encoding="utf-8"), ns)
                await ns["run"](page)
            else:
                print("[record] no --manual or --actions; recording will contain only page load.")

            # Give rrweb a moment to flush its batched events.
            await asyncio.sleep(3)
        finally:
            await browser.close()

        await _release_and_download(solari, session_id, args.out)


if __name__ == "__main__":
    asyncio.run(main())
