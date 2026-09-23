r"""Measure what the wake-word model actually scores, with no threshold and
no debug floor in the way.

Not part of the app — a diagnostic, like the *_test.py experiments at the
repo root.

jarvis/wakeword.py only prints scores at or above WAKE_SCORE_FLOOR, so a
model scoring below it and a model never being fed look identical in the log
(that floor was 0.1, which hid the reset-suppressed ~0.000 scores this probe
exists to find; it is 0.001 now and tunable). This prints every frame's score
and level regardless, so "lower WAKE_THRESHOLD" can be a decision rather than
a guess:

  * peak score 0.3-0.5  -> genuine narrow margin; the threshold is the knob
  * peak score < 0.1    -> not a threshold problem at all. Check the mic
                           level below, then the model's fit to your accent
  * speech near -35 dB  -> mic gain is too low; openWakeWord does not
                           normalize its input, so fix this before anything
                           else

The per-attempt table at the end is the one to read when the question is
"does it miss most of the time": say the wake word ten times and it shows
ten peak scores. All near zero with one spike is a recognition problem
(accent, model fit, mic); a cluster just under the threshold is a threshold
problem; a clean split between ~0.99 and ~0.00 is neither, and points at
audio never reaching the model (see "holes" in the summary).

Three modes:

  Free-running (the original). Say the wake word whenever you like; each
  run of frames above SPEECH_FLOOR_DB is grouped into an utterance.

      .\.venv\Scripts\python.exe tools\wake_score_probe.py --seconds 30

  Prompted attempts. The probe tells you when to speak and scores each
  attempt in its own window, so an attempt too quiet to cross the speech
  floor still counts as a miss instead of vanishing. --csv keeps every
  frame, --wav keeps the audio.

      .\.venv\Scripts\python.exe tools\wake_score_probe.py --attempts 10 ^
          --csv data\wake_probe.csv --wav data\wake_probe.wav

  Replay. Re-score a saved WAV through the same model with no microphone,
  optionally louder. This is how the gain hypothesis gets tested against
  identical audio instead of against a second performance of it -- and how
  a future custom "Τζάρβις" model gets compared to this one fairly.

      .\.venv\Scripts\python.exe tools\wake_score_probe.py ^
          --replay data\wake_probe.wav --windows data\wake_probe.csv --gain-db 10

Ctrl+C stops a live run early and still prints a summary.
"""

from __future__ import annotations

import argparse
import csv
import queue
import sys
import time
import wave
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

# |sample| at or above this is clipping for our purposes. Too much gain
# destroys a score as thoroughly as too little, and dB alone won't show it:
# a clipped frame reads as a healthy loud one.
CLIP_SAMPLE = 32700

# --- Prompted-attempt pacing, in wall-clock seconds. One cycle is: prompt,
# say it, hold still while the model's 1.5s context catches up, rest.
ATTEMPT_LEAD_IN = 3.0
ATTEMPT_SAY_SECONDS = 4.0
ATTEMPT_TAIL_SECONDS = UTTERANCE_TAIL_SECONDS
ATTEMPT_REST_SECONDS = 1.5

# After the last attempt's window closes, keep draining this much longer for
# frames still sitting in ffmpeg's ~1s buffer. A wall-clock backstop, checked
# every pass -- the one in listener.py that lived inside the queue.Empty
# branch is exactly how a 43-second recording went unnoticed.
DRAIN_BACKSTOP_SECONDS = 5.0

CSV_FIELDS = ("stream_time", "recorded_wall", "db", "peak_sample", "score", "attempt", "gap")


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * fraction), len(ordered) - 1)]


class _Probe:
    """Everything measured about one session, live or replayed.

    Frames come in through feed(); the caller decides where they come from
    and which attempt (if any) they belong to, because that attribution is
    the one part that differs between a microphone and a WAV file.
    """

    def __init__(self, threshold: float, score_key: str, csv_path: Path | None = None):
        self.threshold = threshold
        self.score_key = score_key

        self.scores: list[float] = []
        self.speech_scores: list[float] = []
        self.idle_scores: list[float] = []
        self.speech_db: list[float] = []
        self.quiet_db: list[float] = []

        self.utterances: list[dict] = []
        self._current: dict | None = None

        self.attempts: dict[int, dict] = {}
        self.gaps = 0
        self.lost_seconds = 0.0
        self.clipped_frames = 0

        self._csv_file = None
        self._csv = None
        if csv_path is not None:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            self._csv_file = csv_path.open("w", newline="", encoding="utf-8")
            self._csv = csv.writer(self._csv_file)
            self._csv.writerow(CSV_FIELDS)

    def note_gap(self, seconds: float) -> None:
        self.gaps += 1
        self.lost_seconds += seconds

    def feed(
        self,
        model,
        ts: float,
        frame: bytes,
        recorded_wall: float | None = None,
        attempt: int | None = None,
        gap: float = 0.0,
        echo: bool = True,
    ) -> tuple[float, float]:
        """Score one frame and fold it into every tally. Returns (dB, score)."""
        samples = np.frombuffer(frame, dtype=np.int16)
        level = listener._frame_db(frame)
        # int32 first: abs(-32768) overflows int16 and would read as -32768.
        peak_sample = int(np.abs(samples.astype(np.int32)).max()) if samples.size else 0
        score = float(model.predict(samples).get(self.score_key, 0.0))

        self.scores.append(score)
        if peak_sample >= CLIP_SAMPLE:
            self.clipped_frames += 1

        loud = level > SPEECH_FLOOR_DB
        if loud:
            self.speech_db.append(level)
            self.speech_scores.append(score)
        else:
            self.quiet_db.append(level)
            self.idle_scores.append(score)

        self._group_utterance(ts, level, score, loud)
        if attempt is not None:
            self._note_attempt(attempt, ts, level, score, loud)

        if self._csv is not None:
            self._csv.writerow(
                (
                    f"{ts:.3f}",
                    "" if recorded_wall is None else f"{recorded_wall:.3f}",
                    f"{level:.1f}",
                    peak_sample,
                    f"{score:.6f}",
                    "" if attempt is None else attempt,
                    f"{gap:.3f}" if gap else "",
                )
            )

        if echo and (score >= 0.02 or loud):
            bar = "#" * int(score * 40)
            mark = " <-- would fire" if score >= self.threshold else ""
            tag = f" a{attempt}" if attempt is not None else "   "
            print(f"  {ts:6.2f}s{tag}  {level:6.1f} dB  {score:.3f} {bar}{mark}", flush=True)

        return level, score

    def _group_utterance(self, ts: float, level: float, score: float, loud: bool) -> None:
        """Group frames into utterances so ten repetitions read as ten
        numbers rather than as scrollback."""
        if loud:
            if self._current is None:
                self._current = {"start": ts, "end": ts, "peak_db": level, "peak_score": score}
                self.utterances.append(self._current)
            self._current["end"] = ts
            self._current["peak_db"] = max(self._current["peak_db"], level)
            self._current["peak_score"] = max(self._current["peak_score"], score)
        elif self._current is not None:
            if ts - self._current["end"] <= UTTERANCE_TAIL_SECONDS:
                self._current["peak_score"] = max(self._current["peak_score"], score)
            else:
                self._current = None

    def _note_attempt(self, attempt: int, ts: float, level: float, score: float, loud: bool) -> None:
        row = self.attempts.get(attempt)
        if row is None:
            row = {
                "start": ts,
                "frames": 0,
                "loud_frames": 0,
                "peak_db": level,
                "peak_score": score,
                "peak_at": ts,
            }
            self.attempts[attempt] = row
        row["frames"] += 1
        row["loud_frames"] += int(loud)
        row["peak_db"] = max(row["peak_db"], level)
        if score > row["peak_score"]:
            row["peak_score"] = score
            row["peak_at"] = ts

    def close(self) -> None:
        if self._csv_file is not None:
            self._csv_file.close()
            self._csv_file = None
            self._csv = None

    # --- reporting

    def attempt_peaks(self) -> list[float]:
        if self.attempts:
            return [row["peak_score"] for _, row in sorted(self.attempts.items())]
        return [u["peak_score"] for u in self.utterances]

    def summary(self, expected_attempts: int = 0) -> None:
        print("\n--- summary ---")
        lost = f", {self.lost_seconds:.2f}s lost" if self.gaps else ""
        print(f"frames: {len(self.scores)}   holes: {self.gaps}{lost}   clipped frames: {self.clipped_frames}")
        print(f"peak score: {max(self.scores):.3f}   (threshold {self.threshold})")
        # Buckets start below the debug floor: 0.06 and 0.00 look the same in
        # jarvis/wakeword.py's log, and they mean very different things here.
        for cutoff in (0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5):
            print(f"  frames >= {cutoff:.2f}: {sum(1 for s in self.scores if s >= cutoff)}")

        # The distribution question the bucket counts can't answer: is the
        # model near zero while you speak, or close-but-under?
        if self.speech_scores:
            print(
                f"score while speaking: p50 {_percentile(self.speech_scores, 0.5):.3f}  "
                f"p90 {_percentile(self.speech_scores, 0.9):.3f}  "
                f"p99 {_percentile(self.speech_scores, 0.99):.3f}  "
                f"max {max(self.speech_scores):.3f}  ({len(self.speech_scores)} frames)"
            )
        if self.idle_scores:
            print(
                f"score while quiet:    p99 {_percentile(self.idle_scores, 0.99):.3f}  "
                f"max {max(self.idle_scores):.3f}  ({len(self.idle_scores)} frames)"
            )

        if self.speech_db:
            print(
                f"speech level: median {_percentile(self.speech_db, 0.5):.1f} dB, "
                f"peak {max(self.speech_db):.1f} dB  ({len(self.speech_db)} frames)"
            )
        else:
            print(f"speech level: nothing above {SPEECH_FLOOR_DB:.0f} dB -- mic gain is very low")
        if self.quiet_db:
            print(f"noise floor:  median {_percentile(self.quiet_db, 0.5):.1f} dB")

        if self.attempts or expected_attempts:
            self._print_attempts(expected_attempts)
        elif self.utterances:
            self._print_utterances()

        self._print_reading()

    def _print_attempts(self, expected: int) -> None:
        total = max(expected, max(self.attempts, default=0))
        print("\nattempts (each scored in its own window, silent ones included):")
        for i in range(1, total + 1):
            row = self.attempts.get(i)
            if row is None:
                print(f"  {i:2d}. no frames in window -- audio never arrived for it")
                continue
            fired = " <-- would fire" if row["peak_score"] >= self.threshold else ""
            heard = "" if row["loud_frames"] else "  (nothing above the speech floor)"
            print(
                f"  {i:2d}. peak {row['peak_db']:6.1f} dB  score {row['peak_score']:.3f}  "
                f"at +{row['peak_at'] - row['start']:.1f}s{fired}{heard}"
            )
        hits = sum(1 for row in self.attempts.values() if row["peak_score"] >= self.threshold)
        print(f"  {hits}/{total} would fire at threshold {self.threshold}")

    def _print_utterances(self) -> None:
        print(
            f"\nutterances (runs above {SPEECH_FLOOR_DB:.0f} dB; peak score includes "
            f"the {UTTERANCE_TAIL_SECONDS:.1f}s after each, where it usually lands):"
        )
        for i, u in enumerate(self.utterances, 1):
            seconds = u["end"] - u["start"] + listener.FRAME_SECONDS
            fired = " <-- would fire" if u["peak_score"] >= self.threshold else ""
            print(
                f"  {i:2d}. at {u['start']:6.2f}s  {seconds:4.2f}s  "
                f"peak {u['peak_db']:6.1f} dB  score {u['peak_score']:.3f}{fired}"
            )
        hits = sum(1 for u in self.utterances if u["peak_score"] >= self.threshold)
        print(f"  {hits}/{len(self.utterances)} would fire at threshold {self.threshold}")

    def _print_reading(self) -> None:
        """A first reading of the numbers. The decision is still yours --
        this only says which of the three explanations the shape fits."""
        peaks = self.attempt_peaks()
        if not peaks:
            print("\nreading: no attempts registered at all -- check the mic device and gain first.")
            return

        fired = sum(1 for p in peaks if p >= self.threshold)
        median = _percentile(peaks, 0.5)
        print(f"\nreading: {fired}/{len(peaks)} fired, median attempt peak {median:.3f}")

        if fired and median < 0.05:
            print("  Bimodal: it either recognizes you outright or scores near zero.")
            print("  That is not a threshold curve. Suspect audio not reaching the model")
            print("  (holes above) or the utterance landing outside the model's window.")
        elif median < 0.05:
            print("  Near-zero throughout: a genuine model/accent mismatch. No threshold")
            print("  value fixes this -- a custom Greek model is the path.")
        elif median < self.threshold:
            print("  Close but under: a calibration problem. Mic gain/placement and then")
            print("  a threshold retuned on these numbers should fix it.")
        else:
            print("  Scoring well here. If it still misses in the app, the difference is")
            print("  in the app's path, not in the model: check holes and the capture gate.")

        if self.speech_db and _percentile(self.speech_db, 0.5) < -40:
            print("  Mic gain looks low (speech median below -40 dB) -- fix that first.")
        if self.clipped_frames:
            print(f"  {self.clipped_frames} clipped frames -- too much gain also destroys scores.")
        if self.gaps > 2:
            print(f"  {self.gaps} holes ({self.lost_seconds:.2f}s lost) -- audio delivery is itself suspect.")


class _WavSink:
    """Writes the session to a 16kHz mono WAV whose timeline matches
    stream_time: holes are padded with real silence, so a replay lines up
    frame-for-frame with the CSV."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._wave = wave.open(str(path), "wb")
        self._wave.setnchannels(1)
        self._wave.setsampwidth(2)
        self._wave.setframerate(listener.SAMPLE_RATE)

    def pad(self, seconds: float) -> None:
        samples = int(round(seconds * listener.SAMPLE_RATE))
        if samples > 0:
            self._wave.writeframes(b"\x00\x00" * samples)

    def write(self, frame: bytes) -> None:
        self._wave.writeframes(frame)

    def close(self) -> None:
        self._wave.close()


def _attempt_schedule(count: int, start: float) -> list[tuple[float, float]]:
    """(prompt_at, window_end) per attempt, in perf_counter seconds."""
    cycle = ATTEMPT_SAY_SECONDS + ATTEMPT_TAIL_SECONDS + ATTEMPT_REST_SECONDS
    first = start + ATTEMPT_LEAD_IN
    return [
        (first + i * cycle, first + i * cycle + ATTEMPT_SAY_SECONDS + ATTEMPT_TAIL_SECONDS)
        for i in range(count)
    ]


def _attempt_for(schedule: list[tuple[float, float]], recorded_wall: float | None) -> int | None:
    """Which attempt a frame belongs to, asked of the time it was *recorded*.

    Never of the time it arrived: ffmpeg hands over ~1s at a time, so a frame
    spoken during attempt 3 typically reaches us during attempt 4. See
    CLAUDE.md, "The recording clock".
    """
    if recorded_wall is None:
        return None
    for index, (start, end) in enumerate(schedule, 1):
        if start <= recorded_wall <= end:
            return index
    return None


def _run_live(args, model, probe: _Probe) -> int:
    wav = _WavSink(Path(args.wav)) if args.wav else None

    listener.start_stream()
    schedule = _attempt_schedule(args.attempts, time.perf_counter()) if args.attempts else []
    if schedule:
        print(
            f"\n{args.attempts} prompted attempts, {ATTEMPT_SAY_SECONDS:.0f}s each. "
            f"Speak only when told, at your normal distance and volume.\n"
            f"Starting in {ATTEMPT_LEAD_IN:.0f}s.\n",
            flush=True,
        )
        deadline = schedule[-1][1] + DRAIN_BACKSTOP_SECONDS
    else:
        print(f"Say the wake word. Listening for {args.seconds:.0f}s (Ctrl+C to stop).\n", flush=True)
        deadline = time.perf_counter() + args.seconds

    audio_queue = listener._stream_audio_queue
    prompted = 0
    last_ts: float | None = None
    covered_last_window = False

    try:
        while True:
            now = time.perf_counter()

            while prompted < len(schedule) and now >= schedule[prompted][0]:
                prompted += 1
                print(f"\n>>> Attempt {prompted}/{args.attempts} — say «Hey Jarvis» NOW", flush=True)

            if covered_last_window or now >= deadline:
                break

            try:
                item = audio_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                print("Stream ended unexpectedly.")
                break

            ts, frame = item

            # Same hole rule listen_for_wake_word() uses -- a reset here
            # costs the model its ~1.5s of context, so holes are part of
            # what we are measuring, not noise to smooth over.
            gap = 0.0
            if last_ts is not None and ts - last_ts > listener.FRAME_SECONDS + listener.GAP_TOLERANCE:
                gap = ts - last_ts - listener.FRAME_SECONDS
                probe.note_gap(gap)
                print(f"  [gap {gap:.2f}s -> model reset]", flush=True)
                model.reset()
                if wav is not None:
                    wav.pad(gap)
            last_ts = ts

            if wav is not None:
                wav.write(frame)

            origin = listener._stream_origin
            recorded_wall = None if origin is None else origin + ts
            probe.feed(
                model,
                ts,
                frame,
                recorded_wall=recorded_wall,
                attempt=_attempt_for(schedule, recorded_wall),
                gap=gap,
            )

            if schedule and recorded_wall is not None and recorded_wall >= schedule[-1][1]:
                covered_last_window = True
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        listener.stop_stream()
        if wav is not None:
            wav.close()

    if not probe.scores:
        print("\nNo frames captured at all -- the stream never delivered audio.")
        return 1

    probe.summary(expected_attempts=args.attempts)
    if args.csv:
        print(f"\nper-frame CSV: {args.csv}")
    if args.wav:
        print(f"session audio:  {args.wav}  (re-score it with --replay)")
    return 0


def _load_windows(path: Path) -> dict[int, tuple[float, float]]:
    """Attempt -> (first stream_time, last stream_time) from a session CSV,
    so a replay of that session's WAV reports the same attempts."""
    windows: dict[int, tuple[float, float]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not row.get("attempt"):
                continue
            attempt = int(row["attempt"])
            ts = float(row["stream_time"])
            low, high = windows.get(attempt, (ts, ts))
            windows[attempt] = (min(low, ts), max(high, ts))
    return windows


def _run_replay(args, model, probe: _Probe) -> int:
    path = Path(args.replay)
    with wave.open(str(path), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (
            1,
            2,
            listener.SAMPLE_RATE,
        ):
            print(
                f"{path}: need mono 16-bit {listener.SAMPLE_RATE}Hz, got "
                f"{source.getnchannels()}ch {source.getsampwidth() * 8}-bit "
                f"{source.getframerate()}Hz"
            )
            return 1
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype=np.int16)

    print(f"Replaying {path} ({len(samples) / listener.SAMPLE_RATE:.1f}s)", flush=True)
    if args.gain_db:
        scaled = samples.astype(np.float32) * (10 ** (args.gain_db / 20))
        clipped = int(np.count_nonzero(np.abs(scaled) >= 32767))
        samples = np.clip(scaled, -32768, 32767).astype(np.int16)
        print(f"Gain {args.gain_db:+.1f} dB ({clipped} samples clipped by it)", flush=True)

    windows = _load_windows(Path(args.windows)) if args.windows else {}

    model.reset()
    frame_count = len(samples) // listener.FRAME_SAMPLES
    for index in range(frame_count):
        start = index * listener.FRAME_SAMPLES
        chunk = samples[start : start + listener.FRAME_SAMPLES]
        ts = start / listener.SAMPLE_RATE
        attempt = next((a for a, (low, high) in windows.items() if low <= ts <= high), None)
        probe.feed(model, ts, chunk.tobytes(), attempt=attempt)

    if not probe.scores:
        print("\nThe file held no full frames.")
        return 1

    probe.summary(expected_attempts=max(windows, default=0))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=30.0, help="free-running capture length")
    parser.add_argument("--attempts", type=int, default=0, help="prompted attempts instead of --seconds")
    parser.add_argument("--csv", help="write one row per frame here")
    parser.add_argument("--wav", help="save the captured audio here, for --replay")
    parser.add_argument("--replay", help="re-score a saved WAV instead of using the microphone")
    parser.add_argument("--windows", help="session CSV whose attempt windows the replay should reuse")
    parser.add_argument("--gain-db", type=float, default=0.0, help="apply this gain before re-scoring")
    args = parser.parse_args()

    print(f"Model: {WAKE_MODEL_PATH} (threshold in .env: {WAKE_THRESHOLD})")
    print("Loading...")
    model = wakeword._get_model()

    probe = _Probe(WAKE_THRESHOLD, wakeword._score_key, Path(args.csv) if args.csv else None)
    try:
        if args.replay:
            return _run_replay(args, model, probe)
        return _run_live(args, model, probe)
    finally:
        probe.close()


if __name__ == "__main__":
    sys.exit(main())
