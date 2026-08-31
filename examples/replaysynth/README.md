# Record → Replay (replaysynth)

Turn a recorded browser session into a reusable Playwright script.

Solari can record any browser session (`recording=True`) as an rrweb DOM-level
replay. ReplaySynth takes that NDJSON, reconstructs the DOM, and synthesizes
the interactions into clean Playwright Python that replays on a fresh session.

The idea: **record once, replay forever.** The recording is the source
material — it can come from a human driving the session manually, from a
Playwright script, or from any other automation. ReplaySynth doesn't care who
clicked; it turns the recorded rrweb stream into code.

## What it does

| File | Step |
| --- | --- |
| `record.py` | Launch a browser (optional profile), let a human drive it (`--manual`) or run a script (`--actions`), download the rrweb NDJSON replay |
| `replaysynth.py` | Parse the NDJSON (FullSnapshots + incremental mutations), extract clicks/fills/checks, emit `replay_on(page)` |
| `replay.py` | Replay the synthesized actions on a fresh session, optionally reusing a stored profile |
| `test_replaysynth.py` | Offline unit tests — no API key needed (`pytest test_replaysynth.py`) |

## Run

```bash
pip install -r requirements.txt
export SOLARI_API_KEY=slr_live_...   # https://console.getsolari.com
```

### 1. Record

Manual mode — a human drives the live session through the Solari console:

```bash
python record.py --url "https://the-internet.herokuapp.com/login" \
                 --manual \
                 --out session.ndjson
```

This prints the console live-view URL for the session; you click through the
workflow there, then press ENTER. The recording is downloaded when the session
releases.

Deterministic mode — a Playwright actions file drives the session instead
(useful for repeatable tests of the synthesizer itself):

```bash
python record.py --url "https://the-internet.herokuapp.com/login" \
                 --actions actions_login.py \
                 --out session.ndjson
```

### 2. Synthesize + replay

```bash
python replay.py --replay session.ndjson \
                 --url "https://the-internet.herokuapp.com/login" \
                 --secret password=hunter2
```

`--secret NAME=VALUE` fills the password fields rrweb masks to `[REDACTED]` —
the real secret lives only on the command line at replay time, never in the
recording or the generated code.

### Inspect the generated script

```bash
python replaysynth.py session.ndjson --replay-only --out replay.py
```

## Profiles: what's proven vs. what's supported

A Solari **profile** stores cookies/localStorage across sessions. The intended
authenticated-flow pattern is:

1. Record session A with `--profile login` and authenticate (manually or via
   actions). The profile now holds the logged-in state.
2. Record session B with the **same profile** — you're already logged in — and
   perform the *post-login* workflow.
3. Synthesize session B and replay it on a fresh browser with
   `--profile login`. The generated script contains only the post-login steps;
   no credentials are needed or present.

Note the boundary: replaying a recording that *contains* the login form steps
against an already-authenticated profile may fail, because the site redirects
away from its login page. Record the workflow you want to replay in the state
you want to replay it from.

## Gotchas the example encodes

- **rrweb renumbers node ids on every page load.** A login that redirects emits
  a fresh FullSnapshot, so interactions are resolved against the snapshot that
  preceded them — not the first one in the file.
- **Dynamic pages mutate the DOM after the snapshot.** React/Vue insert modals
  and buttons that exist only in rrweb IncrementalSnapshot mutations.
  ReplaySynth applies the `adds` / `removes` / `attributes` / `texts` mutation
  types to keep its DOM index current; nodes removed from the page are dropped
  from the index so no stale selector is synthesized.
- **Clicks target the deepest hit-tested element.** rrweb records the `<i>`
  icon inside a button; we walk up to the enclosing `button` / `a` /
  `input[type=submit|button]` / `select` / `textarea` so the selector is
  stable.
- **Manual typing arrives as incremental values** (`u`, `ut`, `utk`, `utku`).
  Consecutive updates to the same field collapse into one final `fill()`; a
  click or navigation between two fills of the same field keeps them separate.
- **Passwords are masked.** rrweb substitutes `*` for the value, so the field
  is emitted as `[REDACTED]` and resolved from `--secret` at replay time.
- **Selectors are synthesized, not recorded.** The fallback chain is `#id` →
  `[data-testid]`/similar → `[name]`/`[placeholder]`/`[aria-label]` →
  associated `<label>` (`get_by_label`) → role + accessible name
  (`get_by_role` for buttons/links/submit-inputs) → visible text → structural
  path. Coordinates are never used.
