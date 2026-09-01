# Alfred — bug review (2026-09-01)

Full build/install + bug-hunt pass across all three components (Python engine,
Hammerspoon/Lua front-end, Raycast extension), done as a follow-up to the prior
"modern-zen review" fixed in `d4d889b`. That commit's 14 issues are already
fixed as of HEAD and are **not** re-listed here. Everything below is new,
verified by reading the exact code path (not just tooling output), and still
present at HEAD (`d4d889b`).

## Build / install / test results

- `voicebridge.py` needs no third-party packages to import or run its test
  suite (only `mlx-whisper`/`mlx-lm`/`tomlkit` are imported lazily, inside the
  specific commands that need them).
- `python3 -m pytest tests/ -rs --tb=line`: **180 passed, 4 skipped, 9
  failed**. The 4 skips are an intentional guard (`soundfile`/`numpy` not
  installed). All 9 failures are `ModuleNotFoundError: No module named
  'tomlkit'` in `tests/test_config_writer.py` (`voicebridge.py:2151`,
  `:2190`) — a missing-dependency gap in this review environment, not a code
  bug.
- Raycast: `npm install`, `npm run typecheck` (tsc --noEmit), `npm run
  lint:eslint`, and `npx vitest run` (104/104) all pass clean.
- Not runnable in this sandboxed review environment (no effect on the
  findings below, which were verified by direct code reading instead):
  `make test-lua` / `lua tests/lua/test_helpers.lua`, `make lint` (ruff,
  luacheck — luacheck also isn't installed on this host).

## Python engine (`voicebridge.py`)

1. **[High] Unsynchronized clipboard copy+paste races across concurrent daemon
   requests — wrong text can be delivered.** `deliver()` /
   `MacosSink` (`voicebridge.py:1635-1670`). The daemon dispatches every POST
   on its own thread with **no lock** (`voicebridge.py:2588-2592`: "no lock,
   no serialization... never block or cross each other"), and locks exist for
   `StreamSession`, `WarmClaude`, `_LOCAL`, and `history_append`, but not for
   the clipboard. Two captures firing close together in paste mode (a quick
   correction dictated while a longer first capture is still processing) can
   interleave `sink.copy(textA)` / `sink.copy(textB)` / `sink.paste()` so
   that A's paste keystroke actually types B's text into the focused
   document.

2. **[High] No lock around `mlx_whisper.transcribe()` despite concurrent
   invocation being architecturally possible.** `voicebridge.py:438`. The
   local-LLM path is explicitly single-flighted via `_LOCAL_LOCK` ("MLX
   generation is not re-entrant on one model"), but transcription has no
   equivalent. A `StreamSession` background thread keeps polling/transcribing
   for up to `_STREAM_ABANDON_POLLS` (~24-48s) after an abandoned recording
   (crash, app switch, no `stream-finish` call); if the user starts a new
   recording in that window, both sessions' threads can call
   `mlx_whisper.transcribe()` unsynchronized on the same cached model.

3. **[Medium] Unsynchronized read-modify-write of `config.toml`.**
   `_set_config_kv` / `cmd_set_intent` / `cmd_set_model` / `cmd_set_processing`
   / `cmd_set_stt` (`voicebridge.py:2184-2247`) all do read → parse → mutate →
   write with no lock, over the same lock-free daemon dispatch. Two
   near-simultaneous settings changes race: both threads read the same
   pre-edit file, each writes back only its own change, and whichever
   finishes last silently wins — the other change is lost with no error
   (both requests report `"saved"`).

4. **[Low-medium] `WarmClaude` teardown races an in-flight `ask()` on the old
   instance.** `_get_warm` (`voicebridge.py:1043-1058`) calls `_WARM._stop()`
   on the old singleton while holding only the pointer-swap lock, not the old
   instance's own lock that `ask()` uses to protect `self._proc`. Self-healing
   in practice — `run_llm()`'s exception handler falls back to a one-shot
   `claude -p` call — so impact is extra latency/stderr noise, not data loss.

5. **[Low] `--limit 0` is silently treated as unspecified.** `cmd_history`
   (`voicebridge.py:2089`): `n = args.limit or 10` coerces an explicit
   `--limit 0` back to 10 because `0` is falsy in Python, ignoring the user's
   request to list zero items.

6. **[Low] Malformed `Content-Length` crashes the request instead of
   returning the normal JSON error.** `do_POST` (`voicebridge.py:2625`):
   `int(self.headers.get("Content-Length", 0))` runs *before* the
   surrounding try/except (which only guards the body read/JSON parse). A
   non-numeric header raises an unhandled `ValueError`; the connection drops
   with a logged traceback instead of the daemon's usual graceful
   `{"code": 1, "err": ...}` response.

7. **[Low] `claude_extra_args`/`codex_extra_args` silently explode into
   per-character argv if misconfigured as a bare string.**
   `run_llm` (`voicebridge.py:1088,1100`): `list(cfg["llm"].get(...) or [])`
   — if a user writes `claude_extra_args = "--foo"` instead of `["--foo"]`
   (an easy TOML typo), `list("--foo")` becomes `['-','-','f','o','o']`,
   corrupting the CLI invocation into nonsense flags with no validation
   error.

## Hammerspoon front-end (`voicebridge.lua`)

1. **[High] `runEngine()` has no recording-state guard — a text/email action
   during an active recording orphans the `sox` process.**
   `runEngine()` (`voicebridge.lua:573-580`) unconditionally sets state to
   `"processing"` with no check for an in-progress recording. Reachable via
   the app window's `processText` handler (`:1038-1042`) or the result
   panel's `onEmail` handler (`:629-633`, since the panel is never closed on
   a new recording): start a recording, then submit text or hit Email on a
   stale panel — the live `sox` task is never SIGINT'd and is orphaned,
   recording silently to a stray temp WAV for up to `MAX_RECORD_SECS`
   (3600s) while the UI claims idle/ready.

2. **[High] The mode-chooser callback re-checks nothing — a hotkey fired
   while the chooser is open orphans the recording and leaks a HUD canvas +
   timer.** `dictateWithMode()`/`typePrompt()` (`voicebridge.lua:811-840`)
   only check `VB.state` when the chooser opens, not in its (async,
   non-blocking) callback. Open the mode chooser (Cmd+Opt+I), then start a
   recording (Cmd+Opt+D) before picking a mode, then pick one: the callback
   restarts recording, overwriting `VB.recTask` (orphaning the first `sox`,
   as in #1) and `VB.hud`/`VB.hudTimer` (leaking the first HUD canvas and a
   0.1s timer that runs forever).

3. **[High] The `MAX_RECORD_SECS` safety cutoff leaves the UI stuck on
   "recording" and silently discards the audio.** `onRecDone()`
   (`voicebridge.lua:713-728`) guards on `VB.state ~= "processing"`, but
   that state transition only happens inside `stopRecording()`. When `sox`'s
   own `trim 0 3600` cap ends the recording on its own (the documented
   safety net for a forgotten recording), `VB.state` is still `"recording"`,
   so `onRecDone` bails after only `destroyHUD()` — the menu bar/HUD freeze
   on the last displayed time, the captured hour of audio is never
   transcribed, and the user must notice and press the hotkey again to
   recover. This defeats the stated purpose of the cap (stops disk growth,
   not the stuck state/lost audio).

4. **[Medium] `ensureVBDir()` only tightens permissions when it creates the
   directory, never when it already exists.** `voicebridge.lua:181-188`
   (compare `voicebridge.py`'s `_secure_dir()`, which `chmod`s
   unconditionally every call specifically because "default 0755 would let
   any other local `staff` account read" the verbatim dictation logs). If
   `~/.voicebridge` already exists with looser permissions (pre-hardening
   leftover, restored from backup, odd umask), Lua-side debug/log writes
   before the daemon (re)starts go into that directory without the
   permission ever being corrected.

5. **[Medium] Unescaped dictation text embedded into raw HTML can break the
   app window.** `openWindow()` (`voicebridge.lua:1246-1256`) concatenates
   `hs.json.encode(init)` (which includes `VB.resultText` and 30 history
   records of arbitrary past dictated/typed text) directly between
   `WIN_HEAD`/`WIN_TAIL` and sets it via `w:html(...)`. `hs.json.encode`
   does not escape `/`, so any captured text containing the literal
   substring `</script>` closes the script block early, breaking (or
   corrupting) the window's UI the next time it's opened.

6. **[Medium] `cancel` doesn't wait for `sox` to exit before allowing a new
   recording, and the 1-second-resolution temp filename can collide.**
   `cancel` (`voicebridge.lua:1388-1396`) calls `terminate()` then
   immediately sets state to idle, unlike `stopRecording()`'s
   wait-for-exit-callback pattern. `tmpWav()` (`:219-222`) names files by
   `os.time()` (1s granularity); restarting fast enough can reuse the
   still-terminating process's filename, risking a corrupted new recording
   or two `sox -d` processes briefly contending for the mic.

7. **[Low] `fmtTime()` isn't clamped for a backward clock jump.**
   `voicebridge.lua:174-176`. `fmtTime(os.time() - VB.recStart)` with a
   negative input (DST fallback, NTP resync, manual clock change mid-recording)
   produces a garbled string like `"-1:55"` instead of something sane; the
   existing tests only cover non-negative inputs.

8. **[Low] The mode/type-prompt choosers have no re-entrancy guard.**
   `voicebridge.lua:811-840`: both only check `VB.state == "idle"`, which
   stays true for as long as the chooser is open. A fast double-press opens
   two independent `hs.chooser` windows; the one not interacted with is left
   dangling with no remaining reference to dismiss it.

9. **[Info, not a bug]** The engine's warm streaming/rolling-preview pipeline
   (`stream-start`/`stream-finish` in `voicebridge.py`) is never invoked
   anywhere in `voicebridge.lua` — confirmed via `grep`, zero matches. The
   only capture path here is a full batch `process` after recording stops,
   so Hammerspoon users get no live preview. Worth confirming whether that's
   intentional (e.g. only wired into the Raycast/window front-end) or a gap.

## Raycast extension (`raycast/src/`)

`npm run typecheck`, `npm run lint:eslint`, and `npx vitest run` (104/104)
all pass — the issues below are logic bugs those checks can't catch.

1. **[High] A client-side request timeout is treated identically to
   "daemon down," causing a duplicate invocation on a slow-but-successful
   call.** `callEngine` (`raycast/src/lib/engine.ts:366-394`): the `catch`
   after `AbortSignal.timeout(DAEMON_TIMEOUT_MS)` (120s) can't distinguish a
   client-side abort from a real connection failure. Aborting the fetch
   doesn't cancel the in-progress server-side work, so on any request slower
   than 120s (a heavy translate+rewrite+optimize chain, a cold `claude`/
   `codex` CLI start) it unconditionally restarts the daemon and re-runs the
   same argv as a second, independent one-shot process — doubled LLM cost, a
   racing second clipboard write, a possible duplicate history entry.

2. **[High] No reentrancy guard on stopping/transcribing a recording.**
   `stopAndTranscribe` (`raycast/src/dictate.tsx:259-318`): the only guard
   is `if (!st) return`, but `stateRef.current` is never cleared and
   `setPhase` only takes effect on the next React render, leaving a real
   window where a double Enter or a racing menu-bar auto-stop can call
   `callEngine(["stream-finish", st.wav, ...])` twice concurrently on the
   same WAV — whichever response resolves last silently overwrites a
   successful result with an error (or vice versa).

3. **[Medium] Double-submit races can pop the navigation stack twice, or
   silently show a stale result.** No submit handler in the app guards
   against a second concurrent invocation:
   - `FeedbackForm.submit` (`raycast/src/lib/ResultView.tsx:53-80`) and
     `IntentForm.onSubmit` (`raycast/src/manage-intents.tsx:157-189`): two
     successful concurrent submits each call `pop()`, and the second pop can
     close more than intended.
   - `reprocess` (`raycast/src/lib/ResultView.tsx:126-145`): switching
     between two "Reprocess As…" formats quickly lets the slower response
     silently overwrite the screen with stale content and no indication an
     overwrite happened.
   - `PipelineForm.onSubmit` (`raycast/src/lib/PipelineForm.tsx:56-110`) has
     no busy flag at all during the network call; a double ⌘+Enter can push
     two (possibly diverging) result screens onto the navigation stack plus
     double LLM cost.

4. **[Low-medium, defensive gap rather than a currently-reachable bug]**
   `readHistory` (`raycast/src/lib/engine.ts:681-701`) validates only
   `rec.text`, not `ts`; `formatHistoryWhen` (`raycast/src/lib/view-logic.ts
   :145-147`) does `(ts || "").replace(...)` with no type guard, which
   throws if `ts` is ever a truthy non-string, crashing the whole History
   command with no error boundary. Verified the current engine always
   writes `ts` as an ISO string (`voicebridge.py:1688`,
   `datetime.now().isoformat()`), so this isn't reachable through normal use
   today — only through a hand-edited or externally-corrupted
   `history.jsonl` — but there's no validation stopping a future engine
   change from tripping it.

5. **[Low] Title truncation can split a unicode surrogate pair.**
   `formatHistoryTitle` (`raycast/src/lib/view-logic.ts:139-142`):
   `title.slice(0, 57)` cuts by UTF-16 code unit, not code point; a history
   entry with an astral-plane character (e.g. emoji) positioned exactly at
   the cut renders a broken glyph. The existing test only covers ASCII.

6. **[Low] A malformed "saved" status with no path/text is treated as
   silent success.** `resolveDelivery` (`raycast/src/lib/engine.ts
   :649-660`) / `deliveryFailure` (`raycast/src/lib/view-logic.ts
   :241-249`): if a `VB_STATUS saved` line arrives with no path and no
   `VB_RESULT` line, the result is still classified as success and the UI
   shows an empty result screen with no error banner.

## Method

Three focused parallel reviews (one per component), each instructed to avoid
re-flagging the issues already fixed in `d4d889b` and earlier commits, and to
verify every finding against the actual current code rather than speculate.
The highest-severity findings from each area were independently re-verified
by direct code reading before inclusion here (all confirmed accurate); the
`readHistory`/`ts` finding above was downgraded after that re-check showed
it isn't reachable through the engine's current own output.
