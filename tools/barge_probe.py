r"""Measure how loud Jarvis's own voice is at the microphone, against how loud
you are talking over it.

Not part of the app -- a diagnostic, like tools/wake_score_probe.py.

Barge-in has to answer one question on every frame recorded while Jarvis is
talking: is this him, or is this you? The microphone hears both, mixed, and
nothing in the audio says which is which. Every threshold in the feature is
therefore a bet on one number -- **how many dB above his own voice yours
arrives** -- and that number is a property of your room, your speakers, your
mic gain and where they all sit. It cannot be guessed from here, and it is the
reason this probe exists before the feature does.

Read the summary in this order:

  * **margin (speech p50 - echo p50)**. This is the headroom barge-in has to
    work in, and it decides whether the feature is buildable as designed:

        > 12 dB   comfortable. Set BARGE_IN_MARGIN_DB to about half of it.
        6 - 12 dB workable, but expect to tune the onset window too.
        < 6 dB    level alone cannot separate you from him. Turn the speakers
                  down or move the mic; if the margin stays small, barge-in
                  has to be BARGE_IN_MODE=wakeword (say the wake word to
                  interrupt) rather than level-based.
        negative  the mic hears him better than it hears you. Nothing
                  downstream can fix that -- it is a furniture problem.

  * **the would-have-fired table**. For each candidate margin it replays the
    recorded frames through the same rule the feature will use (this many dB
    over the running echo level, sustained for this long) and reports what it
    would have done: a FIRE during the echo-only phase is a reply cut off for
    nothing, a miss during the barge phase is an interruption that did not
    work. Pick the smallest margin with no false fires.

  * **echo p95 vs speech p50**. If the echo's loud tail reaches your median
    speech, a median-based floor will flap. That is the case for a longer
    onset window rather than a bigger margin.

Two modes.

  Echo only -- Jarvis speaks, you stay quiet. This is the baseline and the
  one to run first; it alone answers "how loud is he at the mic".

      .\.venv\Scripts\python.exe tools\barge_probe.py --echo

  Barge -- Jarvis speaks and the probe tells you when to talk over him. Say
  something ordinary at a normal volume, the way you actually would; saying
  it loudly proves nothing, because the whole question is whether normal
  speech clears him.

      .\.venv\Scripts\python.exe tools\barge_probe.py --barge --csv data\barge_probe.csv

Ctrl+C stops early and still prints what it has.

**The capture gate is deliberately bypassed.** listener._should_capture drops
every frame recorded while Jarvis is audible -- which is precisely the audio
this probe is about, so it is patched open for the run. That is also what the
real feature will do, except that it will route those frames to the barge-in
decider instead of into the recording pipeline. Nothing here touches the
database, and no transcribed text is produced or stored: levels only, same
rule the diagnostic log follows.
"""

from __future__ import annotations

import argparse
import collections
import csv
import queue
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis import listener, speaker  # noqa: E402
from jarvis.config import SILENCE_THRESHOLD_DB, TTS_ENGINE  # noqa: E402

# Long enough to talk over comfortably and to give the echo level a stable
# median. Deliberately ordinary Greek -- the point is a normal reply at a
# normal volume, not a test tone.
PASSAGE = (
    "Λοιπόν, να σου πω τι έχω δει μέχρι τώρα. Ο καιρός αύριο θα είναι "
    "καλός, με λίγη συννεφιά το πρωί και ήλιο μετά το μεσημέρι. "
    "Τα μαθήματά σου ξεκινάνε την άλλη εβδομάδα, και έχεις ακόμα δύο "
    "εργασίες να παραδώσεις πριν το τέλος του μήνα. "
    "Θυμήσου επίσης ότι το μαγαζί θέλει παραγγελία για καφέ, γιατί "
    "τελειώνει μέχρι την Παρασκευή. "
    "Αν θέλεις σου τα λέω ένα ένα με τη σειρά, ή σου στέλνω μια λίστα."
)

# Frames in this window right after the prompt belong to neither bucket.
# Nobody starts talking the instant they are told to, so this stretch is
# still clean echo -- but counting it as echo would stretch the phase past
# the moment the user actually began, and counting it as speech would drag
# the speech median down toward the echo. Both directions corrupt the one
# number this probe exists to produce, so it is simply dropped.
BARGE_REACTION_LEAD = 0.6

# Candidate margins the would-have-fired table reports on, in dB over the
# running echo level.
CANDIDATE_MARGINS = (3.0, 6.0, 9.0, 12.0, 15.0)

# How long a frame run must stay above the floor to count as a barge-in.
# Longer than the recorder's ONSET_SECONDS (0.16s) on purpose: a door or a
# desk knock must never cut off a reply, and unlike the recorder this is
# competing with a signal that is already loud.
ONSET_SECONDS = 0.32

# The running echo level is a median over this much recent audio. A median,
# not a mean: a barge-in is short and loud, and would drag a mean up toward
# itself until it stopped being detectable.
ECHO_WINDOW_SECONDS = 2.0


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _describe(name: str, levels: list[float]) -> str:
    if not levels:
        return f"  {name:<22} (no frames)"
    return (
        f"  {name:<22} n={len(levels):<5} "
        f"p05={_percentile(levels, 0.05):6.1f}  "
        f"p50={_percentile(levels, 0.50):6.1f}  "
        f"p95={_percentile(levels, 0.95):6.1f}  "
        f"max={max(levels):6.1f} dB"
    )


def _would_fire(
    prime: list[float], test: list[float], margin: float
) -> tuple[bool, float]:
    """Replay the barge-in rule: prime the echo estimate, then run it.

    Returns (fired, seconds into `test`).

    **`prime` is not optional, and leaving it out is the bug this signature
    exists to prevent.** The rule is "this many dB above the running level of
    recent audio", and the running level means *Jarvis's* level -- which is
    established by the frames before the user speaks. Building the median out
    of the frames under test instead lets the floor chase the interruption
    upward, and nothing ever clears it: a synthetic run with a clean +11.4 dB
    margin reported MISSED at every candidate threshold.

    The window keeps absorbing frames as it goes, exactly as the live decider
    will. A median survives that: a barge-in is a handful of frames against a
    two-second window, so it cannot pull the floor up over itself before the
    onset window has already elapsed.
    """
    window_frames = max(1, int(ECHO_WINDOW_SECONDS / listener.FRAME_SECONDS))
    onset_frames = max(1, int(ONSET_SECONDS / listener.FRAME_SECONDS))
    window = collections.deque(prime[-window_frames:], maxlen=window_frames)
    run = 0

    for index, level in enumerate(test):
        if window:
            floor = statistics.median(window) + margin
            if level > floor:
                run += 1
                if run >= onset_frames:
                    return True, index * listener.FRAME_SECONDS
            else:
                run = 0

        window.append(level)

    return False, 0.0


class _Probe:
    def __init__(self, csv_path: Path | None):
        self.frames: list[tuple[float, float]] = []  # (stream_time, level_db)
        self._csv_file = None
        self._csv = None
        if csv_path is not None:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            self._csv_file = csv_path.open("w", newline="", encoding="utf-8")
            self._csv = csv.writer(self._csv_file)
            self._csv.writerow(["stream_time", "level_db", "phase"])

    def feed(self, stream_time: float, frame: bytes) -> None:
        self.frames.append((stream_time, listener._frame_db(frame)))

    def close(self) -> None:
        if self._csv_file is not None:
            self._csv_file.close()

    def write_csv(self, phases: list[tuple[float, float, str]]) -> None:
        if self._csv is None:
            return
        for stream_time, level in self.frames:
            self._csv.writerow(
                [f"{stream_time:.3f}", f"{level:.2f}", _phase_of(stream_time, phases)]
            )


def _phase_of(stream_time: float, phases: list[tuple[float, float, str]]) -> str:
    for start, end, name in phases:
        if start <= stream_time < end:
            return name
    return "idle"


def _drain_into(probe: _Probe, stop: threading.Event) -> None:
    """Pull every frame off the stream until told to stop."""
    audio_queue = listener._stream_audio_queue
    while not stop.is_set():
        try:
            item = audio_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        if item is None:
            return
        stream_time, frame = item
        probe.feed(stream_time, frame)


def _speak_in_background(text: str) -> threading.Thread:
    thread = threading.Thread(target=speaker.speak, args=(text,), daemon=True)
    thread.start()
    return thread


def _prompt_barge(delay: float, stop: threading.Event, prompted: dict) -> None:
    """Tell the user to speak, and record exactly when we told them.

    The wall clock here -- not a fraction of the reply's length -- is what
    splits the echo-only frames from the ones with speech over them. The two
    are not the same instant: this fires `delay` seconds after speak() was
    called, and speak() spends its first second or two synthesizing in
    silence before any playback starts at all.
    """
    if stop.wait(delay):
        return
    prompted["at"] = time.perf_counter()
    print("\n  >>> ΜΙΛΑ ΤΩΡΑ -- πες κάτι κανονικά, πάνω από τη φωνή του <<<\n")


def run(args: argparse.Namespace) -> int:
    probe = _Probe(Path(args.csv) if args.csv else None)

    # The whole point: these are the frames the capture gate exists to throw
    # away. Patched open for the run, which is what the real feature will do
    # too -- routing them to the barge-in decider instead of into the
    # recording pipeline.
    listener._should_capture = lambda stream_time: True

    print(f"TTS engine: {TTS_ENGINE}   speech floor in .env: {SILENCE_THRESHOLD_DB} dB")
    print("Opening the microphone...")
    listener.start_stream()

    stop = threading.Event()
    reader = threading.Thread(target=_drain_into, args=(probe, stop), daemon=True)
    reader.start()

    phases: list[tuple[float, float, str]] = []
    prompted: dict[str, float] = {}

    def mark(name: str, start_wall: float, end_wall: float) -> None:
        origin = listener._stream_origin
        if origin is None:
            return
        phases.append((start_wall - origin, end_wall - origin, name))

    try:
        print("\nBaseline: μείνε σιωπηλός για 3 δευτερόλεπτα...")
        base_start = time.perf_counter()
        time.sleep(3.0)
        mark("baseline", base_start, time.perf_counter())

        print("Ο Τζάρβις μιλάει τώρα.")
        if args.barge:
            print("Περίμενε το μήνυμα και μετά μίλα από πάνω του.")

        speak_start = time.perf_counter()
        speaking = _speak_in_background(PASSAGE)

        if args.barge:
            threading.Thread(
                target=_prompt_barge,
                args=(args.barge_after, stop, prompted),
                daemon=True,
            ).start()

        speaking.join()
        speak_end = time.perf_counter()
        mark("speaking", speak_start, speak_end)

        print("Τέλος. Μείνε σιωπηλός άλλα 2 δευτερόλεπτα...")
        time.sleep(2.0)
    except KeyboardInterrupt:
        print("\n(διακόπηκε)")
    finally:
        stop.set()
        reader.join(timeout=2)
        listener.stop_stream()

    return _summarize(probe, phases, prompted, args)


def _summarize(probe: _Probe, phases, prompted: dict, args) -> int:
    if not probe.frames:
        print("\nNo frames were captured at all -- check the microphone.")
        return 1

    speaking_phase = next((p for p in phases if p[2] == "speaking"), None)
    if speaking_phase is None:
        print("\nPlayback never started, so there is nothing to compare.")
        return 1

    origin = listener._stream_origin
    barge_at = None
    if args.barge:
        if "at" not in prompted:
            print("\nThe speak prompt never fired, so only the echo was measured.")
        elif origin is None:
            print("\nThe stream never produced an origin estimate.")
        else:
            # Converted on the same clock every frame carries, so the split
            # lands where the user was actually told to speak.
            barge_at = (prompted["at"] - origin) + BARGE_REACTION_LEAD

    baseline: list[float] = []
    echo: list[float] = []
    barge: list[float] = []
    dropped = 0

    # Frames between the prompt and the end of the reaction window are
    # neither: see BARGE_REACTION_LEAD.
    lead_start = barge_at - BARGE_REACTION_LEAD if barge_at is not None else None

    for stream_time, level in probe.frames:
        phase = _phase_of(stream_time, phases)
        if phase == "baseline":
            baseline.append(level)
        elif phase == "speaking":
            if barge_at is not None and stream_time >= barge_at:
                barge.append(level)
            elif lead_start is not None and stream_time >= lead_start:
                dropped += 1
            else:
                echo.append(level)

    # Both lists are already in recorded order, which is what the
    # would-have-fired replay needs: it rebuilds a running median as it goes.
    echo_ordered, barge_ordered = echo, barge

    # Label the CSV with the same split the summary used, so a row can be
    # traced back to which population it landed in. Inserted at the front
    # because _phase_of takes the first match and "speaking" spans both.
    if barge_at is not None:
        speak_end = speaking_phase[1]
        phases.insert(0, (barge_at, speak_end, "barge"))
        phases.insert(0, (lead_start, barge_at, "reaction"))

    probe.write_csv(phases)
    probe.close()

    print("\n" + "=" * 72)
    print("LEVELS")
    print(_describe("room (silent)", baseline))
    print(_describe("Jarvis only (echo)", echo))
    if args.barge:
        print(_describe("you over Jarvis", barge))
        if dropped:
            print(f"  ({dropped} frames dropped as reaction time after the prompt)")

    if echo and barge:
        margin = _percentile(barge, 0.50) - _percentile(echo, 0.50)
        print(f"\n  margin (speech p50 - echo p50): {margin:+.1f} dB")
        if margin < 6:
            print("  -> too small for a level-based rule. See the module docstring.")
        elif margin < 12:
            print("  -> workable; expect to tune the onset window as well.")
        else:
            print(f"  -> comfortable. Try BARGE_IN_MARGIN_DB around {margin / 2:.0f}.")

        print(f"\n  echo p95 {_percentile(echo, 0.95):.1f} dB vs speech p50 "
              f"{_percentile(barge, 0.50):.1f} dB")

    if echo_ordered:
        print("\n" + "=" * 72)
        print(f"WOULD-HAVE-FIRED  (onset {ONSET_SECONDS:.2f}s, "
              f"echo window {ECHO_WINDOW_SECONDS:.1f}s)")
        print(f"  {'margin':>8}  {'echo-only phase':<26}  barge phase")
        # The false-positive run primes on the opening of the echo and is
        # tested on the rest of it; the true-positive run primes on the whole
        # echo, which is what the live decider will be holding when the user
        # starts talking.
        split = max(1, int(ECHO_WINDOW_SECONDS / listener.FRAME_SECONDS))
        for candidate in CANDIDATE_MARGINS:
            false_fire, false_at = _would_fire(
                echo_ordered[:split], echo_ordered[split:], candidate
            )
            left = f"FIRE at +{false_at:.2f}s (false)" if false_fire else "silent (good)"
            if barge_ordered:
                hit, hit_at = _would_fire(echo_ordered, barge_ordered, candidate)
                right = f"FIRE at +{hit_at:.2f}s (good)" if hit else "MISSED"
            else:
                right = "(not measured)"
            print(f"  {candidate:6.0f} dB  {left:<26}  {right}")
        print("\n  Pick the smallest margin with no false fire that still fires"
              " on the barge.")

    print("=" * 72)
    if args.csv:
        print(f"Per-frame levels written to {args.csv}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure Jarvis's echo at the mic.")
    parser.add_argument("--echo", action="store_true",
                        help="Jarvis speaks, you stay quiet (run this first)")
    parser.add_argument("--barge", action="store_true",
                        help="Jarvis speaks and you talk over him when prompted")
    parser.add_argument("--barge-after", type=float, default=6.0,
                        help="seconds into playback before the speak prompt")
    parser.add_argument("--csv", help="write one row per frame here")
    args = parser.parse_args()

    if not args.echo and not args.barge:
        parser.error("choose --echo (run this first) or --barge")

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
