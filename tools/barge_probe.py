r"""Measure how loud Jarvis's own voice is at the microphone, against how loud
you are talking over it.

Not part of the app -- a diagnostic, like tools/wake_score_probe.py.

Barge-in has to answer one question on every frame recorded while Jarvis is
talking: is this him, or is this you? The microphone hears both, mixed, and
nothing in the audio says which is which. Every threshold in the feature is
therefore a bet on one number -- **how far above his own voice yours reaches**
-- and that number is a property of your room, your speakers, your mic gain
and where they all sit. It cannot be guessed from here, and it is the reason
this probe exists before the feature does.

Read the summary in this order:

  * **the decision rule block**. This is the headline, because it is the only
    thing that reports the statistic the feature actually tests. The rule
    compares one frame against a *running median* of recent audio, and speech
    is bursty against its own median by 25-30 dB -- so the number that decides
    the feature is not the gap between two medians, it is how far each signal
    reaches above the running floor and *stays* there for ONSET_SECONDS:

        echo alone reaches +X dB   -> any margin at or below X false-fires on
                                      Jarvis's own vowels
        you over him      +Y dB   -> any margin above Y misses you

    Buildable iff Y > X, with the operating point between them. If Y <= X,
    level alone cannot separate you from him: turn the speakers down or move
    the mic, and if it stays that way, barge-in has to be
    BARGE_IN_MODE=wakeword (say the wake word to interrupt).

  * **the would-have-fired table**, which is the same thing measured per
    candidate rather than as a bound. A FIRE during the echo-only phase is a
    reply cut off for nothing; a miss during the barge phase is an
    interruption that did not work. Pick the smallest margin with no false
    fire that still fires on the barge.

  * **margin (speech p50 - echo p50)** is reported last and is *context, not
    a verdict*. It compares two medians, while the rule compares an
    instantaneous frame to a median. The two can differ by the burstiness
    figure above, so a healthy-looking margin here is routinely accompanied
    by false fires at every candidate. That is not a contradiction; it is the
    two panels measuring different things.

Two modes. **Run --echo first, and give it a CSV.** The echo-only run is the
one that establishes the false-fire bound, and it needs a long clean stretch
of Jarvis talking: in a --barge run the echo bucket ends the moment you are
prompted, which leaves only a few seconds of it.

  Echo only -- Jarvis speaks, you stay quiet.

      .\.venv\Scripts\python.exe tools\barge_probe.py --echo --csv data\echo.csv

  Barge -- Jarvis speaks and the probe tells you when to talk over him. Say
  something ordinary at a normal volume, the way you actually would; saying
  it loudly proves nothing, because the whole question is whether normal
  speech clears him.

      .\.venv\Scripts\python.exe tools\barge_probe.py --barge --csv data\barge.csv

Ctrl+C stops early and still prints what it has.

**The buckets are cut from real playback, not from speak().** speak() spends
its first seconds synthesizing in total silence -- Edge TTS is a network
round trip -- and counting that silence as "echo" drops the echo median by
several dB and primes the false-fire replay on an empty room. Both make the
feature look far more buildable than it is. speaker._intervals already
records the wall-clock spans during which a sound was actually audible (it is
what the capture gate reads), so the phases are cut from those, and frames
inside the reply but outside them are reported as `synthesis` and dropped.

**The barge bucket is bounded.** It covers BARGE_WINDOW_SECONDS after the
prompt and no more. Left to run to the end of the reply it is mostly Jarvis
again -- you say one phrase and he keeps talking for another fifteen seconds
-- and its median then measures the mixture ratio rather than your voice.

**The capture gate is deliberately bypassed.** listener._should_capture drops
every frame recorded while Jarvis is audible -- which is precisely the audio
this probe is about, so it is patched open for the run. That is also what the
real feature will do, except that it will route those frames to the barge-in
decider instead of into the recording pipeline. Nothing here touches the
database, and no transcribed text is produced or stored: levels only, same
rule the diagnostic log follows.

**It refuses to run blind.** Every phase is watched, and the run is abandoned
the moment the microphone stops feeding it. This exists because of a run that
did not: the mic had dropped off the USB bus, the probe captured 13 frames
(1.04s, one -audio_buffer_size 1000 buffer) and then nothing, and it went on
to spend 3s on a baseline, ~20s on a reply and 2s on a tail against a dead
ffmpeg -- reporting a *statistics* problem ("0 frames of clean echo") and a
plausible-looking -47 dB room level that was really one buffer of
device-teardown noise. A --barge run 40 seconds later captured nothing at
all. Neither said the word microphone, and ffmpeg's own explanation was
sitting unread in _stream_stderr_queue the whole time, which is what
_ffmpeg_said reads now.
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

# How much audio after the reaction lead counts as "you over Jarvis".
#
# This bound is the whole difference between measuring your voice and
# measuring a mixture. You are asked for one ordinary phrase; the reply runs
# for another fifteen or twenty seconds afterwards, and every one of those
# seconds is echo-only audio that would land in the speech bucket. Measured
# on a real run, an unbounded bucket was 18.2s long and alternated between
# your level and the echo's, with a median sitting halfway between the two
# and moving with how long you happened to speak.
#
# Generous enough for a phrase said at a normal pace, short enough that it
# cannot swallow the rest of the reply.
BARGE_WINDOW_SECONDS = 2.5

# Candidate margins the would-have-fired table reports on, in dB over the
# running echo level.
#
# These start where they do because speech is bursty against its own running
# median by 25-30 dB, so every candidate below about 18 dB fires on Jarvis's
# own vowels -- a table that stopped at 15 dB reported a false fire on every
# row and pointed at no usable operating point at all.
CANDIDATE_MARGINS = (9.0, 12.0, 15.0, 18.0, 21.0, 24.0, 27.0)

# How long a frame run must stay above the floor to count as a barge-in.
# Longer than the recorder's ONSET_SECONDS (0.16s) on purpose: a door or a
# desk knock must never cut off a reply, and unlike the recorder this is
# competing with a signal that is already loud.
ONSET_SECONDS = 0.32

# The running echo level is a median over this much recent audio. A median,
# not a mean: a barge-in is short and loud, and would drag a mean up toward
# itself until it stopped being detectable.
ECHO_WINDOW_SECONDS = 2.0

# How long the probe tolerates no frame being *read* off ffmpeg before it
# gives up on the run.
#
# Far more aggressive than listener.STREAM_STALL_TIMEOUT (15s), and it can
# afford to be: that one guards a wake-word wait, where nobody talking for a
# minute is the ordinary case, while here audio is supposed to be flowing
# continuously from the moment the baseline starts. Still above both numbers
# it has to clear -- the ~1.4s DirectShow takes to hand over its first frame,
# and the 1.008s worst inter-frame gap a bursty dshow input produces -- so it
# cannot fire on jitter.
#
# It watches listener._stream_frames_read rather than the queue, for the
# reason listener's own backstop does: an empty queue is a normal state, a
# frozen read counter is not.
STREAM_SILENT_TIMEOUT = 3.0


class _StreamGone(Exception):
    """The microphone stopped feeding the probe mid-run.

    Raised from whichever phase noticed, so run() can abandon the script and
    report a device problem instead of letting _summarize describe the
    statistics of an empty sample.
    """


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


def _window_frames() -> int:
    return max(1, int(ECHO_WINDOW_SECONDS / listener.FRAME_SECONDS))


def _onset_frames() -> int:
    return max(1, int(ONSET_SECONDS / listener.FRAME_SECONDS))


def _deltas(prime: list[float], test: list[float]) -> list[float]:
    """Each test frame's level minus the running median in front of it.

    This is the quantity the decision rule thresholds, so every number the
    summary reports about the rule is derived from it rather than from a
    second, differently-shaped statistic.

    **`prime` is not optional, and leaving it out is the bug this signature
    exists to prevent.** The running level means *Jarvis's* level, which is
    established by the frames before the user speaks. Building the median out
    of the frames under test instead lets the floor chase the interruption
    upward, and nothing ever clears it: a synthetic run with a clean +11.4 dB
    margin reported MISSED at every candidate threshold.

    The window keeps absorbing frames as it goes, exactly as the live decider
    will. A median survives that: a barge-in is a handful of frames against a
    two-second window, so it cannot pull the floor up over itself before the
    onset window has already elapsed.
    """
    size = _window_frames()
    window = collections.deque(prime[-size:], maxlen=size)
    out: list[float] = []
    for level in test:
        out.append(level - statistics.median(window) if window else float("-inf"))
        window.append(level)
    return out


def _would_fire(
    prime: list[float], test: list[float], margin: float
) -> tuple[bool, float]:
    """Replay the barge-in rule. Returns (fired, seconds into `test`)."""
    onset = _onset_frames()
    run = 0
    for index, delta in enumerate(_deltas(prime, test)):
        if delta > margin:
            run += 1
            if run >= onset:
                return True, (index - onset + 1) * listener.FRAME_SECONDS
        else:
            run = 0
    return False, 0.0


def _reach(prime: list[float], test: list[float]) -> float:
    """The largest margin this audio would still fire at.

    Equivalently: the highest level, in dB over the running median, that the
    signal holds for a sustained ONSET_SECONDS. For echo-only audio that is
    the false-fire bound -- any margin at or below it cuts off a reply for
    nothing. For barge audio it is the miss bound. The feature is buildable
    exactly when the second exceeds the first, which is the one comparison
    the old summary never made.
    """
    deltas = _deltas(prime, test)
    onset = _onset_frames()
    if len(deltas) < onset:
        return float("-inf")
    return max(
        min(deltas[i:i + onset]) for i in range(len(deltas) - onset + 1)
    )


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

    def write_csv(self, label) -> None:
        if self._csv is None:
            return
        for stream_time, level in self.frames:
            self._csv.writerow(
                [f"{stream_time:.3f}", f"{level:.2f}", label(stream_time)]
            )


def _drain_into(probe: _Probe, stop: threading.Event, ended: threading.Event) -> None:
    """Pull every frame off the stream until told to stop.

    `ended` is the EOF sentinel promoted to something the main thread can see.
    This used to just `return`, which is why a run against a dead ffmpeg
    looked exactly like a run against a silent room.
    """
    audio_queue = listener._stream_audio_queue
    while not stop.is_set():
        try:
            item = audio_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        if item is None:
            ended.set()
            return
        stream_time, frame = item
        probe.feed(stream_time, frame)


def _ffmpeg_said() -> list[str]:
    """Whatever ffmpeg has put on stderr, which the probe has to read itself.

    listener routes these lines to diag.write() now, but that only records for
    a real Jarvis session (diag stays disabled until start_session, which this
    tool deliberately never calls -- the log is a record of runs of Jarvis,
    not of diagnostics). So the queue is the probe's copy.
    """
    stderr_queue = listener._stream_stderr_queue
    if stderr_queue is None:
        return []
    # The tail: a stream that died mid-run was working when it printed its
    # banner, so the last thing it said is the only interesting part.
    return listener._stderr_lines(stderr_queue)[-6:]


def _watchdog() -> dict:
    """State for _check_stream: the last read count and when it last moved."""
    return {"read": listener._stream_frames_read, "since": time.perf_counter()}


def _check_stream(state: dict, ended: threading.Event) -> None:
    """Raise _StreamGone if the microphone has stopped feeding the probe."""
    if ended.is_set():
        raise _StreamGone("ffmpeg's audio stream ended")

    now = time.perf_counter()
    read = listener._stream_frames_read
    if read != state["read"]:
        state["read"], state["since"] = read, now
    elif now - state["since"] > STREAM_SILENT_TIMEOUT:
        raise _StreamGone(
            f"no frame was read for {STREAM_SILENT_TIMEOUT:.1f}s "
            f"(stuck at {read} frames)"
        )


def _sleep_watching(seconds: float, state: dict, ended: threading.Event) -> None:
    """time.sleep(seconds), abandoning the run if the stream dies during it."""
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        _check_stream(state, ended)
        time.sleep(0.05)


def _join_watching(thread: threading.Thread, state: dict, ended: threading.Event) -> None:
    """thread.join(), abandoning the run if the stream dies while waiting.

    The reply is the longest phase by far -- ~20s -- so joining it blind is
    where most of a dead run's time used to go.
    """
    while thread.is_alive():
        _check_stream(state, ended)
        thread.join(0.05)


def _speak_in_background(text: str) -> threading.Thread:
    thread = threading.Thread(target=speaker.speak, args=(text,), daemon=True)
    thread.start()
    return thread


def _wait_for_playback(stop: threading.Event, timeout: float = 30.0) -> float | None:
    """Block until a sound is actually audible, and return when that was.

    speaker._playback_start is set by the _playing() context manager at the
    instant playback begins, which is the event this probe cares about and
    which speak() returning cannot tell us.
    """
    deadline = time.perf_counter() + timeout
    while not stop.is_set() and time.perf_counter() < deadline:
        started = speaker._playback_start
        if started is not None:
            return started
        if stop.wait(0.02):
            break
    return None


def _prompt_barge(delay: float, stop: threading.Event, prompted: dict) -> None:
    """Tell the user to speak, and record exactly when we told them.

    The wall clock here -- not a fraction of the reply's length -- is what
    splits the echo-only frames from the ones with speech over them.

    The delay is counted from **playback onset**, not from the speak() call.
    Synthesis is silent and its length varies with the network, so counting
    from the call made `--barge-after 6` mean anything from four to six
    seconds of actual echo, and left the echo bucket short by however long
    Edge TTS happened to take.
    """
    started = _wait_for_playback(stop)
    if started is None:
        return
    remaining = delay - (time.perf_counter() - started)
    if remaining > 0 and stop.wait(remaining):
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
    try:
        listener.start_stream()
    except Exception as e:
        # start_stream() checks that the device really opened now, so this is
        # where an unplugged or busy microphone lands -- before a 25-second
        # script rather than after one.
        probe.close()
        print(f"\nThe microphone did not open:\n  {e}")
        return 1

    stop = threading.Event()
    ended = threading.Event()
    reader = threading.Thread(target=_drain_into, args=(probe, stop, ended), daemon=True)
    reader.start()

    # Wall clock throughout. Converting to stream time needs _stream_origin,
    # which is a rolling estimate that is still tightening while the run is
    # in progress -- so every boundary is converted once, at the end, with
    # the final estimate. Converting each as it happened mixed origins and
    # shifted the phase edges against each other.
    marks: list[tuple[float, float, str]] = []
    prompted: dict[str, float] = {}
    gone: str | None = None
    watch = _watchdog()

    try:
        print("\nBaseline: μείνε σιωπηλός για 3 δευτερόλεπτα...")
        base_start = time.perf_counter()
        _sleep_watching(3.0, watch, ended)
        marks.append((base_start, time.perf_counter(), "baseline"))

        # The baseline is the cheapest possible check that the microphone is
        # real, and it is shorter than STREAM_SILENT_TIMEOUT -- so a device
        # that opened and then produced nothing at all would otherwise not be
        # caught until several seconds into the reply. Explicit here, and
        # decisive: three silent seconds still produce ~33 frames.
        if not probe.frames:
            raise _StreamGone("no audio at all arrived during the 3s baseline")

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

        _join_watching(speaking, watch, ended)
        speak_end = time.perf_counter()
        marks.append((speak_start, speak_end, "reply"))

        # The spans during which a sound was actually in the room. speak()
        # brackets synthesis as well, and synthesis is silent.
        for start, end in list(speaker._intervals):
            if end >= speak_start and start <= speak_end:
                marks.append((start, end, "audible"))

        print("Τέλος. Μείνε σιωπηλός άλλα 2 δευτερόλεπτα...")
        _sleep_watching(2.0, watch, ended)
    except KeyboardInterrupt:
        print("\n(διακόπηκε)")
    except _StreamGone as e:
        gone = str(e)
        # Nothing left to measure, so stop the reply rather than talking at an
        # empty room through the diagnosis. This is speaker.stop() doing the
        # output half of barge-in, for once with no decider behind it.
        speaker.stop()
    finally:
        stop.set()
        reader.join(timeout=2)
        # Before stop_stream(), which drops the queue this reads.
        said = _ffmpeg_said()
        listener.stop_stream()

    if gone is not None:
        return _report_gone(probe, gone, said)

    return _summarize(probe, marks, prompted, args)


def _report_gone(probe: _Probe, why: str, said: list[str]) -> int:
    """Say that the microphone died, and say it in those words.

    The failure this replaces reported "Not enough clean echo to replay the
    rule: 0 frames" next to a -47 dB room level -- two true statements about
    a sample that did not exist, and no mention of the device. Nothing here
    is a barge-in measurement, so none is printed.
    """
    probe.close()
    seconds = len(probe.frames) * listener.FRAME_SECONDS
    print(f"\nThe microphone stopped feeding the probe: {why}")
    print(f"  {len(probe.frames)} frames captured ({seconds:.2f}s of audio) "
          f"before it stopped.")
    if said:
        print("  ffmpeg said:")
        for line in said:
            print(f"    {line}")
    else:
        # Not an omission -- it separates a device that errored out from one
        # that simply went quiet under a live ffmpeg, which are different
        # faults with different fixes (see listener.STREAM_STALL_TIMEOUT).
        print("  ffmpeg said nothing: it went quiet rather than erroring out.")
    print(f"\n  No barge-in numbers are reported, because nothing here "
          f"measures one.\n  Check that {listener.MICROPHONE_NAME} is plugged "
          f"in and not held by another\n  program, then re-run.")
    return 1


def _summarize(probe: _Probe, marks, prompted: dict, args) -> int:
    # Every bail-out closes the CSV. They used to fall out above write_csv()
    # and close(), leaving a half-written file open until interpreter exit --
    # which is why the abandoned barge run left a CSV holding a header and
    # nothing else, with no hint that it had been given up on rather than
    # simply having no rows to write.
    if not probe.frames:
        # A backstop now: run()'s baseline check catches this first and names
        # the device to go and look at.
        probe.close()
        print("\nNo frames were captured at all -- check the microphone.")
        return 1

    origin = listener._stream_origin
    if origin is None:
        probe.close()
        print("\nThe stream never produced an origin estimate.")
        return 1

    def to_stream(wall: float) -> float:
        return wall - origin

    spans = [(to_stream(s), to_stream(e), name) for s, e, name in marks]
    audible = [(s, e) for s, e, name in spans if name == "audible"]
    reply = next(((s, e) for s, e, name in spans if name == "reply"), None)
    baseline_span = next(((s, e) for s, e, name in spans if name == "baseline"), None)

    if not audible:
        probe.close()
        print("\nPlayback never became audible, so there is nothing to compare.")
        return 1

    barge_at = None
    if args.barge:
        if "at" not in prompted:
            print("\nThe speak prompt never fired, so only the echo was measured.")
        else:
            barge_at = to_stream(prompted["at"]) + BARGE_REACTION_LEAD
    lead_start = barge_at - BARGE_REACTION_LEAD if barge_at is not None else None
    barge_end = barge_at + BARGE_WINDOW_SECONDS if barge_at is not None else None

    def label(t: float) -> str:
        """One place deciding which bucket a frame lands in.

        The summary and the CSV both read it, so a row can always be traced
        back to the population it was counted in -- they were computed by two
        separate pieces of logic before, which is a standing invitation for
        the file and the numbers to disagree.
        """
        if baseline_span and baseline_span[0] <= t < baseline_span[1]:
            return "baseline"
        if any(s <= t < e for s, e in audible):
            if barge_at is not None:
                if t >= barge_end:
                    return "post"      # he is still talking; not your voice
                if t >= barge_at:
                    return "barge"
                if t >= lead_start:
                    return "reaction"  # see BARGE_REACTION_LEAD
            return "echo"
        if reply and reply[0] <= t < reply[1]:
            return "synthesis"         # inside speak(), but silent
        return "idle"

    buckets: dict[str, list[float]] = collections.defaultdict(list)
    for stream_time, level in probe.frames:
        buckets[label(stream_time)].append(level)

    probe.write_csv(label)
    probe.close()

    baseline = buckets["baseline"]
    # Already in recorded order, which is what the replay needs: it rebuilds
    # a running median as it goes.
    echo = buckets["echo"]
    barge = buckets["barge"]

    print("\n" + "=" * 72)
    print("LEVELS")
    print(_describe("room (silent)", baseline))
    print(_describe("Jarvis only (echo)", echo))
    if args.barge:
        print(_describe("you over Jarvis", barge))

    dropped = [
        (name, len(buckets[name]))
        for name in ("synthesis", "reaction", "post")
        if buckets[name]
    ]
    if dropped:
        print("\n  dropped from both buckets:")
        why = {
            "synthesis": "inside speak() but silent -- Edge TTS on the network",
            "reaction": "reaction time after the prompt",
            "post": f"more than {BARGE_WINDOW_SECONDS:.1f}s after the prompt"
                    " -- he is still talking",
        }
        for name, count in dropped:
            print(f"    {name:<10} {count:>4} frames "
                  f"({count * listener.FRAME_SECONDS:5.2f}s)  {why[name]}")

    # The prime is the audio the live decider will be holding when the user
    # starts talking, and it must be real echo. Priming on the synthesis
    # silence -- which is what cutting the phase from speak() used to do --
    # starts the floor at the empty room's level, and then the first vowel
    # Jarvis utters clears every candidate margin.
    split = _window_frames()
    echo_prime, echo_test = echo[:split], echo[split:]

    if len(echo_test) >= _onset_frames():
        print("\n" + "=" * 72)
        print(f"DECISION RULE  (sustained {ONSET_SECONDS:.2f}s over a running "
              f"{ECHO_WINDOW_SECONDS:.1f}s median)")
        echo_reach = _reach(echo_prime, echo_test)
        print(f"  echo alone reaches   {echo_reach:+6.1f} dB over its own running "
              f"median")
        print(f"     -> a margin at or below {echo_reach:.0f} dB false-fires on his "
              f"own vowels")
        if barge:
            barge_reach = _reach(echo, barge)
            print(f"  you over Jarvis      {barge_reach:+6.1f} dB")
            print(f"     -> a margin above {barge_reach:.0f} dB misses you")
            if barge_reach > echo_reach:
                lo, hi = echo_reach, barge_reach
                print(f"\n  => usable margins: {lo:.0f} to {hi:.0f} dB. "
                      f"Try BARGE_IN_MARGIN_DB around {(lo + hi) / 2:.0f}.")
            else:
                print("\n  => no usable margin: he reaches as far over the floor as "
                      "you do.")
                print("     Turn the speakers down or move the mic and re-run. If it "
                      "stays\n     this way, barge-in has to be "
                      "BARGE_IN_MODE=wakeword.")
        else:
            print("  you over Jarvis      (not measured -- this is an --echo run)")

        print("\n" + "=" * 72)
        print("WOULD-HAVE-FIRED")
        print(f"  {'margin':>8}  {'echo-only phase':<26}  barge phase")
        for candidate in CANDIDATE_MARGINS:
            false_fire, false_at = _would_fire(echo_prime, echo_test, candidate)
            left = f"FIRE at +{false_at:.2f}s (false)" if false_fire else "silent (good)"
            if barge:
                hit, hit_at = _would_fire(echo, barge, candidate)
                right = f"FIRE at +{hit_at:.2f}s (good)" if hit else "MISSED"
            else:
                right = "(not measured)"
            print(f"  {candidate:6.0f} dB  {left:<26}  {right}")
        print("\n  Pick the smallest margin with no false fire that still fires"
              " on the barge.")
    else:
        print(f"\n  Not enough clean echo to replay the rule: {len(echo)} frames, and "
              f"the\n  first {split} prime the floor. Run --echo (or raise "
              f"--barge-after) for a\n  longer stretch of Jarvis talking alone.")

    if echo and barge:
        print("\n" + "=" * 72)
        print("CONTEXT  (not the verdict -- see the decision rule block above)")
        margin = _percentile(barge, 0.50) - _percentile(echo, 0.50)
        print(f"  margin (speech p50 - echo p50): {margin:+.1f} dB")
        burst = _percentile(echo, 0.95) - _percentile(echo, 0.50)
        print(f"  echo burstiness (p95 - p50):    {burst:+.1f} dB")
        print("\n  These two are why the margin above is not the verdict: the rule")
        print("  thresholds single frames against a median, so a margin has to clear")
        print("  the echo's *burstiness*, not its median. Whenever the burstiness")
        print("  exceeds the margin, expect false fires at every candidate below it.")
        print(f"\n  (For the same reason 'echo p95 {_percentile(echo, 0.95):.1f} dB "
              f"vs speech p50 {_percentile(barge, 0.50):.1f} dB'")
        print("   is near-tautological when both sides are speech, and is no longer")
        print("   reported as a finding.)")

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
                        help="seconds of audible playback before the speak prompt")
    parser.add_argument("--csv", help="write one row per frame here")
    args = parser.parse_args()

    if not args.echo and not args.barge:
        parser.error("choose --echo (run this first) or --barge")

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
