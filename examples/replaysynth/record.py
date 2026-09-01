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

    # Profile state is saved only when explicitly requested:
    python record.py --profile login --save-profile --url https://example.com --actions actions.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a .env next to this script (no dependency).

    Existing environment variables win (setdefault), so a real export always
    overrides the file. .env is gitignored — the key never leaves your disk.
    """
    env_path = pathlib.Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _console_session_id(session_id: str) -> str | None:
    """Extract the console's UUID from Solari's signed composite session id."""
    # Browser API session ids are documented as pool:uuid:org:issuedAt.sig.
    parts = session_id.split(":", 2)
    return parts[1] if len(parts) == 3 and parts[1] else None


async def _new_page(browser):
    """Open a page with profile state because the SDK leaves it unapplied."""
    state = browser.session.storage_state
    if isinstance(state, dict):
        context = await browser.new_context(storage_state=state)
        return await context.new_page()
    return await browser.new_page()


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
    ap.add_argument("--save-profile", action="store_true",
                    help="save the session's cookies/localStorage to --profile when finished")
    ap.add_argument(
        "--out", default="session.ndjson", help="where to write the decompressed NDJSON"
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--manual", action="store_true",
                      help="open the session for a human to drive (live view in Solari console)")
    mode.add_argument("--actions", default=None,
                      help="path to a .py of Playwright actions to run after load")
    args = ap.parse_args()

    if args.save_profile and not args.profile:
        ap.error("--save-profile requires --profile NAME")

    _load_dotenv()
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
        console_session_id = _console_session_id(session_id)
        print(f"[record] session ready (recording; console id {console_session_id or 'see sessions list'})")

        try:
            page = await _new_page(browser)
            await page.goto(args.url)

            if args.manual:
                # Browser sessions do not expose a public stream URL like
                # desktops do. The console owns the authenticated observer
                # connection, so use its real session UUID (not the signed API
                # capability id) and /live route.
                sessions_url = "https://console.getsolari.com/sessions"
                live_url = (
                    f"https://console.getsolari.com/sessions/{console_session_id}/live"
                    if console_session_id
                    else None
                )
                print()
                print("  ┌─────────────────────────────────────────────────────────────┐")
                print("  │  MANUAL RECORDING — drive the browser yourself             │")
                print("  └─────────────────────────────────────────────────────────────┘")
                print()
                if live_url:
                    print("  1. Open this live-view URL in the Solari Console:")
                    print(f"       {live_url}")
                    print("     If it does not open directly, sign in first, open")
                    print(f"     {sessions_url}, select console id {console_session_id},")
                    print("     then choose Open Live View.")
                else:
                    print("  1. Sign in to the Solari Console, open:")
                    print(f"       {sessions_url}")
                    print("     select the running session, then choose Open Live View.")
                print()
                print("  2. Click the live browser canvas to focus it, then interact")
                print("     with the page (clicks, typing, navigation).")
                print("     Everything is recorded as rrweb events.")
                print()
                print("  3. When you're done, press ENTER here to stop recording.")
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

            if args.save_profile:
                # Attaching a profile loads its state; it does not persist new
                # cookies automatically. Save explicitly after the workflow.
                state = await page.context.storage_state()
                result = await solari.profiles.save(profile_id, state)
                print(f"[record] saved profile {profile_id} v{result.version} ({result.size_bytes} bytes)")

            # Give rrweb a moment to flush its batched events.
            await asyncio.sleep(3)
        finally:
            await browser.close()

        await _release_and_download(solari, session_id, args.out)


if __name__ == "__main__":
    asyncio.run(main())
