# Phase 1 status — wake-word reliability

Branch: `fix/wake-word`. Written 2026-09-23 as a handoff snapshot, not a
permanent doc — fold anything still true into `CLAUDE.md` once Phase 1 closes.

## The symptom this phase started from

Live testing showed "Hey Jarvis" sometimes triggering within a couple of
seconds, and sometimes taking 30-90s on the same setup. The hits that did
land scored high (0.93-0.99), which ruled out "the model can't hear this
voice" as the whole story and pointed at something intermittent — a delivery
problem (frames not reaching the model) or a scoring problem (frames arriving
but scoring low), rather than a fit problem.

## What was built to diagnose it

`tools/wake_score_probe.py` (commit `d8d8541`) — a standalone diagnostic, not
part of the app. Three modes: free-running capture, **prompted attempts**
(scores N tries in their own windows so a too-quiet attempt counts as a miss
instead of vanishing), and **replay** (re-scores a saved WAV with no
microphone, optionally with gain applied — lets a gain hypothesis be tested
against identical audio instead of a second performance of it).

Alongside it, `jarvis/listener.py` (commit `32f40a4`) got instrumentation
that shipped no behavior change:

- **`_gap_blame()`** — a hole in the frame stream can come from three causes
  that look identical to any consumer: the capture gate refused the frame
  (Jarvis was audible), the queue overran and `_enqueue_bounded` evicted the
  oldest frame, or ffmpeg never produced it. Two new counters
  (`_frames_gated`, `_frames_evicted`) let the gap line say which, and
  whether the hole opened at the start of a wait or mid-wait.
- **`WAKE_SCORE_FLOOR`** (default `0.001`, was hardcoded at `0.1`) — the old
  floor hid the one score that mattered: a wake word spoken into a
  just-reset model scores ~0.000, not merely low, and that printed nothing.
  Now every frame down to the floor prints, at four decimal places.
- A 5s heartbeat while waiting for the wake word, checked *before* the
  blocking `get()` so it still fires when the queue has starved completely.

## Finding #1: the beep-reset bug (fixed, commit `2cab8c8`)

Replaying `data/wake_probe.wav` through the model showed utterances that
score 0.999 clean scoring **0.000** when `wakeword.reset()` lands under
~0.5s before them, recovering only at ~0.96s. `reset()` reseeds the model's
16-frame window with embeddings of four seconds of random noise — it's not a
degraded score, it's no score at all, for about a second.

The bug: `listen_for_wake_word()` was the only state transition that emptied
the queues by draining rather than by calling `flush()` (which raises
`_capture_floor`). Draining left ffmpeg's already-buffered ~1s of audio to
arrive right after entry and be accepted on a floor the *previous* turn had
set. Those stale frames set `last_ts`, and then the frames the capture gate
legitimately dropped while the beep played (0.16s beep + 0.4s `ECHO_PAD` ≈
0.56s, seven frames) read as a hole — so the model reset itself at the exact
moment the beep told the user to speak.

Fix (both landed in `2cab8c8`):
- **(B)** `listen_for_wake_word()` now calls `flush()` on entry instead of
  draining, so the leftover second is dropped by the floor instead of
  arriving and manufacturing a hole.
- **(E)** the retrigger cooldown no longer restarts on a hole — after a hole
  nothing was just spoken, so there's nothing for the cooldown to suppress,
  and a freshly-reset window scores ~0.000 rather than spuriously high, so
  it can't re-fire on its own either.

`test_record_timing.py` pins the new behavior with an inverted version of the
test that pinned the old one (`test_a_detection_inside_the_post_hole_cooldown_is_ignored`),
plus a new test for the flush-on-entry path. Both fail against the pre-fix
commit.

## Probe run after the fix

A prompted 10-attempt run was captured live post-fix (`data/wake_probe.csv`,
`data/wake_probe.wav`, timestamped 14:29, six minutes after `2cab8c8`):

| attempt | peak score | fired (≥0.5)? | time to cross threshold |
|---|---|---|---|
| 1 | 0.9987 | yes | 2.88s |
| 2 | 0.9965 | yes | 3.28s |
| 3 | 0.9977 | yes | 2.56s |
| 4 | 0.9979 | yes | 1.84s |
| 5 | 0.9988 | yes | 2.24s |
| 6 | 0.9965 | yes | 0.64s |
| 7 | 0.9798 | yes | 1.60s |
| 8 | 0.9816 | yes | 1.52s |
| 9 | 0.4012 | **no** | — |
| 10 | 0.4007 | **no** | — |

**8/10 fire rate.** The 8 hits confirm the model itself scores well
(0.98-0.999) and reacts fast once it does (0.64-3.28s) — consistent with
"the hits that land score 0.93-0.99" from before this phase, and evidence
the beep-reset bug is no longer degrading otherwise-clean attempts to zero:
neither miss here scored anywhere near 0.000, both scored ~0.40.

That 0.40 ceiling is itself informative. The probe's own diagnostic
guidance (`tools/wake_score_probe.py --help`) reads a peak in 0.3-0.5 as "a
genuine narrow margin — the threshold is the knob," as opposed to a
near-zero peak (recognition/accent/mic problem) or a clean 0.99/0.00 split
(frames never reaching the model). Mic level doesn't obviously explain the
miss either: attempts 9 and 10 peaked at -34.2dB / -34.8dB, in the same
range as attempts 1, 6 and 8, which all fired cleanly. So on this run, 2/10
misses look like ordinary threshold-margin variance, not a repeat of the
reset bug and not a mic-gain problem.

## Still open: the 30-90s long tail

The original complaint was delays up to 90s, not misses inside a single ~4s
attempt window. Nothing in this phase's data has reproduced or explained
that directly — the prompted-attempt probe only measures the few seconds
after the user is told to speak, so it can't see a 90s stall by
construction. The instrumentation built for it (heartbeat, `_gap_blame`,
score floor) is in place but hasn't yet been read against a live run that
actually exhibits the long tail.

Per the heartbeat/gap data reasoning that motivated `_gap_blame()`: a stall
that long is not explained by ordinary frame loss (holes are bounded by
`MAX_GAP_SECONDS`/`MAX_LOST_SECONDS` and would surface as a `"gap"` stop
reason or a logged reset, not silence) — which points at a **scoring-side**
issue rather than a **delivery-side** one: audio is reaching the model, but
something about a live 30-90s wait behaves differently from a scripted
10-attempt session (queue starvation the heartbeat hasn't caught in a
directed test, model score drift over a longer idle stretch, cooldown/reset
interaction outside the beep-hole case just fixed, etc.). This is a
hypothesis, not a finding — it hasn't been isolated yet.

## What to test next (at the mic)

1. **Reproduce the long tail with `WAKE_DEBUG=true` running.** The goal is a
   live session that actually hits a 30-90s delay with the heartbeat, gap
   lines, and score floor all visible — none of the current data has that.
2. **Read the heartbeat cadence during a long wait.** If it stops appearing
   or slows down, the queue is starving (delivery-side, ffmpeg/device). If
   it keeps ticking every 5s but no score ever gets near threshold, that's
   scoring-side and points back at the model/session state.
3. **Watch for repeated `reset()` calls during a single long wait.** If the
   model is getting reset far more often than expected outside the
   just-fixed beep-hole case, each reset buys ~1s of near-zero scoring —
   several of those could plausibly compound into the 30-90s range.
4. **Re-run the 10-attempt prompted probe a few more times** to see whether
   the ~2/10 narrow-margin miss rate (peak ~0.40) is stable. If it climbs
   toward 4-5/10, `WAKE_THRESHOLD` is too tight for real conditions and
   lowering it is the fix this phase's roadmap note anticipated. If it stays
   around 2/10, it's probably not worth chasing further.
5. **Try `--replay` with `--gain-db`** on a saved long-tail session's WAV (if
   one gets captured) to check whether a gain bump moves the borderline
   ~0.40 scores meaningfully, before touching mic hardware or `.env`.
