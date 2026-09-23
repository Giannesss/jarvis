r"""Measure what the wake-word model actually scores, with no threshold and
no debug floor in the way.

Not part of the app — a diagnostic, like the *_test.py experiments at the
repo root.

jarvis/wakeword.py only prints scores at or above _DEBUG_SCORE_FLOOR (0.1),
so a model scoring 0.02 and a model never being fed look identical in the
log. This prints every frame's score and level, so "lower WAKE_THRESHOLD"
can be a decision rather than a guess:

  * peak score 0.3-0.5  -> genuine narrow margin; the threshold is the knob
  * peak score < 0.1    -> not a threshold problem at all. Check the mic
                           level below, then the model's fit to your accent
  * speech near -35 dB  -> mic gain is too low; openWakeWord does not
                           normalize its input, so fix this before anything
                           else

The per-utterance table at the end is the one to read when the question is
"does it miss most of the time": say the wake word ten times and it shows ten
peak scores. All near zero with one spike is a recognition problem (accent,
model fit, mic); a cluster just under the threshold is a threshold problem.

Usage:
    .\.venv\Scripts\python.exe tools\wake_score_probe.py [--seconds 30]

Say the wake word every few seconds, varying distance and loudness the way
you actually talk to Jarvis. Ctrl+C stops early and still prints a summary.
"""

from __future__ import annotations

import argparse
import queue
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis import listener, wakeword  # noqa: E402
from jarvis.config import WAKE_MODEL_PATH, WAKE_THRESHOLD  # noqa: E402

# Loud enough to be a plausible wake word rather than room noise. Frames
# below this are still scored, but are summarized separately -- mixing them
# into the "speech" percentiles would drag them toward the noise floor.
SPEECH_FLOOR_DB = -45.0

# openWakeWord scores each frame from the ~1.5s of audio before it, so an
# utterance's peak score lands *after* its loud frames end. Quiet frames
# within this much of the last loud one still count toward that utterance.
UTTERANCE_TAIL_SECONDS = 1.5


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * fraction), len(ordered) - 1)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=30.0)
    args = parser.parse_args()

    print(f"Model: {WAKE_MODEL_PATH} (threshold in .env: {WAKE_THRESHOLD})")
    print("Loading...")
    model = wakeword._get_model()
    score_key = wakeword._score_key

    listener.start_stream()
    print(f"Say the wake word. Listening for {args.seconds:.0f}s (Ctrl+C to stop).\n")

    audio_queue = listener._stream_audio_queue
    scores: list[float] = []
    speech_db: list[float] = []
    quiet_db: list[float] = []
    utterances: list[dict] = []
    current: dict | None = None
    gaps = 0
    last_ts: float | None = None
    deadline = time.perf_counter() + args.seconds

    try:
        while time.perf_counter() < deadline:
            try:
                item = audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                print("Stream ended unexpectedly.")
                break

            ts, frame = item

            # Same hole rule listen_for_wake_word() uses -- a reset here
            # costs the model its ~1.5s of context, so holes are part of
            # what we are measuring, not noise to smooth over.
            if last_ts is not None and ts - last_ts > listener.FRAME_SECONDS + listener.GAP_TOLERANCE:
                gaps += 1
                print(f"  [gap {ts - last_ts - listener.FRAME_SECONDS:.2f}s -> model reset]")
                model.reset()
            last_ts = ts

            level = listener._frame_db(frame)
            score = model.predict(np.frombuffer(frame, dtype=np.int16)).get(score_key, 0.0)
            scores.append(score)
            (speech_db if level > SPEECH_FLOOR_DB else quiet_db).append(level)

            # Group frames into utterances so ten repetitions read as ten
            # numbers rather than as scrollback.
            if level > SPEECH_FLOOR_DB:
                if current is None:
                    current = {"start": ts, "end": ts, "peak_db": level, "peak_score": score}
                    utterances.append(current)
                current["end"] = ts
                current["peak_db"] = max(current["peak_db"], level)
                current["peak_score"] = max(current["peak_score"], score)
            elif current is not None:
                if ts - current["end"] <= UTTERANCE_TAIL_SECONDS:
                    current["peak_score"] = max(current["peak_score"], score)
                else:
                    current = None

            # A bar makes the shape of each utterance readable while it runs.
            if score >= 0.02 or level > SPEECH_FLOOR_DB:
                bar = "#" * int(score * 40)
                mark = " <-- would fire" if score >= WAKE_THRESHOLD else ""
                print(f"  {ts:6.2f}s  {level:6.1f} dB  {score:.3f} {bar}{mark}")
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        listener.stop_stream()

    if not scores:
        print("\nNo frames captured at all -- the stream never delivered audio.")
        return 1

    print("\n--- summary ---")
    print(f"frames: {len(scores)}   holes: {gaps}")
    print(f"peak score: {max(scores):.3f}   (threshold {WAKE_THRESHOLD})")
    # Buckets start below the debug floor: 0.06 and 0.00 look the same in
    # jarvis/wakeword.py's log, and they mean very different things here.
    for cutoff in (0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5):
        print(f"  frames >= {cutoff:.2f}: {sum(1 for s in scores if s >= cutoff)}")

    if speech_db:
        print(
            f"speech level: median {_percentile(speech_db, 0.5):.1f} dB, "
            f"peak {max(speech_db):.1f} dB  ({len(speech_db)} frames)"
        )
    else:
        print(f"speech level: nothing above {SPEECH_FLOOR_DB:.0f} dB -- mic gain is very low")
    if quiet_db:
        print(f"noise floor:  median {_percentile(quiet_db, 0.5):.1f} dB")

    if utterances:
        print(
            f"\nutterances (runs above {SPEECH_FLOOR_DB:.0f} dB; peak score includes "
            f"the {UTTERANCE_TAIL_SECONDS:.1f}s after each, where it usually lands):"
        )
        for i, u in enumerate(utterances, 1):
            seconds = u["end"] - u["start"] + listener.FRAME_SECONDS
            fired = " <-- would fire" if u["peak_score"] >= WAKE_THRESHOLD else ""
            print(
                f"  {i:2d}. at {u['start']:6.2f}s  {seconds:4.2f}s  "
                f"peak {u['peak_db']:6.1f} dB  score {u['peak_score']:.3f}{fired}"
            )
        hits = sum(1 for u in utterances if u["peak_score"] >= WAKE_THRESHOLD)
        print(f"  {hits}/{len(utterances)} would fire at threshold {WAKE_THRESHOLD}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
