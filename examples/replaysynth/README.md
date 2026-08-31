# Record → Replay (replaysynth)

Turn a one-time manual browser session into a reusable Playwright script.

Record a real Solari browser session (it ships an rrweb DOM-level replay), then
synthesize the interactions into clean Playwright Python and replay them on a
fresh session — reusing a stored profile so authenticated flows replay forever
without re-authenticating.

The idea: **watch someone click once, play it back forever.** rrweb records
every FullSnapshot, click and input; we parse the NDJSON, reconstruct a DOM
index, and synthesize robust selectors (`#id` → `[data-testid]` → `[name]` →
accessible name → structural path). Coordinates in rrweb are unreliable, so we
never trust them — selectors are the source of truth.

## What it does

| File | Step |
| --- | --- |
| `record.py` | Launch a browser (with an optional profile), drive a page, download the rrweb NDJSON replay |
| `replaysynth.py` | Parse the NDJSON, extract clicks/fills/checks, emit `replay_on(page)` |
| `replay.py` | Replay the synthesized actions on a fresh session, reusing a profile to skip login |

## Run

```bash
pip install -r requirements.txt
export SOLARI_API_KEY=slr_live_...   # https://console.getsolari.com
```

### 1. Record

```bash
python record.py --url "https://the-internet.herokuapp.com/login" \
                 --actions actions_login.py \
                 --out session.ndjson
```

`actions_login.py` runs on the live page while the session is recorded; the
resulting `session.ndjson` holds the interactions.

### 2. Synthesize + replay

```bash
python replay.py --replay session.ndjson \
                 --url "https://the-internet.herokuapp.com/login" \
                 --secret password=hunter2
```

`--secret NAME=VALUE` fills the password fields rrweb masks to `[REDACTED]` —
pass the real secret once, replay forever. Use `--profile NAME` to reuse a
stored Solari profile and skip re-authentication entirely.

### Inspect the generated script

```bash
python replaysynth.py session.ndjson --replay-only --out replay.py
```

## Gotchas the example encodes

- **rrweb renumbers node ids on every page load.** A login that redirects emits
  a fresh FullSnapshot, so interactions must be resolved against the snapshot
  that preceded them — not the first one in the file.
- **Clicks target the deepest hit-tested element.** rrweb records the `<i>`
  icon inside a button; we walk up to the enclosing `button`/`a` so the selector
  is stable.
- **Passwords are masked.** rrweb substitutes `*` for the value, so the field
  is emitted as `[REDACTED]` and resolved from `--secret` at replay time.
