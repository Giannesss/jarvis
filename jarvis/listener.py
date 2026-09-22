from __future__ import annotations

import array
import collections
import io
import math
import queue
import re
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

from faster_whisper import WhisperModel

from jarvis import wakeword
from jarvis.config import (
    MAX_RECORD_SECONDS,
    NO_SPEECH_TIMEOUT,
    SILENCE_DURATION,
    SILENCE_THRESHOLD_DB,
    WAKE_BEEP,
    WAKE_DEBUG,
    WAKE_RETRIGGER_COOLDOWN,
    WHISPER_MODEL,
)


FFMPEG_PATH = (
    r"C:\Users\User\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe"
)

MICROPHONE_NAME = "Μικρόφωνο (Razer Seiren Mini)"

# silence_start timestamps at or below this are leading silence (before any
# speech); above it, they mark silence that follows speech.
LEADING_SILENCE_MAX = 0.5

SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")

INITIAL_PROMPT = (
    "Τζάρβις. Γεια σου Τζάρβις, τι κάνεις; Τζάρβις, τι ώρα είναι; "
    "Τζάρβις, πες μου κάτι."
)

# --- Wake-word streaming (start_stream/stop_stream/listen_for_wake_word/
# record_command, defined after listen()) shares one persistent ffmpeg
# process for the whole run instead of listen()'s one-shot-per-utterance
# process. listen() itself is untouched and used unmodified whenever
# WAKE_WORD_ENABLED is false.

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80ms at 16kHz
FRAME_BYTES = FRAME_SAMPLES * 2  # 16-bit samples = 2560 bytes/frame
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE

AUDIO_QUEUE_SECONDS = 10  # generous slack; the consumer loops poll faster
AUDIO_QUEUE_MAXSIZE = int(AUDIO_QUEUE_SECONDS / FRAME_SECONDS)
STDERR_QUEUE_MAXSIZE = 200

PREROLL_SECONDS = 1.0
PREROLL_FRAMES = int(PREROLL_SECONDS / FRAME_SECONDS)

# --- record_command() speech detection. It measures each 80ms frame's own
# level instead of reading ffmpeg's silencedetect lines: every frame is
# already in hand, so the decision is drift-free and immune to the delay
# between a silence starting and ffmpeg logging it (silencedetect only
# reports a silence after SILENCE_DURATION of it has already passed, which
# made a natural pause right after the wake word look like end-of-speech).
#
# Every threshold below is in *seconds of recorded audio*, never in frames
# or in wall clock. Frames arrive ~1s at a time and gated frames never
# arrive at all, so counting them measured neither the audio nor the clock:
# one burst could satisfy the whole end-of-speech rule in a millisecond, and
# a 15-second budget could take 43 seconds to spend. See _StopDecider.

# Sound above the noise floor for this long means speech has started -- less
# than that is usually a click or a door.
ONSET_SECONDS = 0.16

# Two frames are contiguous if their timestamps differ by FRAME_SECONDS;
# anything beyond this tolerance is a hole where audio should have been.
GAP_TOLERANCE = FRAME_SECONDS / 2

# A hole up to this long is padded with silence and counted as quiet (it is
# what the microphone heard). A longer one, or more than MAX_LOST_SECONDS of
# them in a single utterance, means too much is missing to transcribe
# honestly: stop with "gap" and let the caller ask again.
MAX_GAP_SECONDS = 0.5
MAX_LOST_SECONDS = 1.0

# How often WAKE_DEBUG prints the current level while waiting for speech.
DEBUG_LEVEL_INTERVAL = 1.0

# dB reported for a completely silent frame (log10(0) is undefined).
SILENT_DB = -90.0

_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _model

    if _model is None:
        print("Φόρτωση Whisper...")
        t0 = time.perf_counter()
        _model = WhisperModel(
            WHISPER_MODEL,
            device="cpu",
            compute_type="int8",
        )
        print(f"[timing] Whisper load: {time.perf_counter() - t0:.2f}s")

    return _model


def preload() -> None:
    """Load the Whisper model now instead of on the first listen() call."""
    _get_model()


def _read_lines(pipe, line_queue: "queue.Queue[str | None]") -> None:
    try:
        for line in iter(pipe.readline, ""):
            line_queue.put(line)
    except Exception as e:
        line_queue.put(f"[reader error] {e}\n")
    finally:
        line_queue.put(None)  # sentinel: ffmpeg closed stderr (process exited)


def listen() -> str | None:
    """Record from the Razer microphone and transcribe locally with Whisper.

    Recording stops once the speaker falls silent, using ffmpeg's
    silencedetect filter on the live stream. silencedetect only logs
    "silence_start: X" after SILENCE_DURATION of silence has already
    elapsed, and only logs "silence_end" once sound returns — so a
    silence_start with X near 0 is leading silence before any speech,
    while a silence_start with a later X means speech happened first and
    the trailing silence has already been waited out (safe to stop right
    away). NO_SPEECH_TIMEOUT only fires while we're still waiting out that
    leading silence (a silence_start near 0 with no silence_end after it);
    if no silence event has been logged yet, sound has been present from
    the start, so only MAX_RECORD_SECONDS is enforced. Hard limit either
    way: MAX_RECORD_SECONDS overall.
    """

    wav_path = Path(tempfile.gettempdir()) / "jarvis_mic.wav"

    command = [
        FFMPEG_PATH,
        "-y",
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostats",
        "-f",
        "dshow",
        "-audio_buffer_size",
        "1000",
        "-i",
        f"audio={MICROPHONE_NAME}",
        "-t",
        str(MAX_RECORD_SECONDS),
        "-af",
        f"silencedetect=noise={SILENCE_THRESHOLD_DB}dB:d={SILENCE_DURATION}",
        "-c:a",
        "pcm_s16le",
        str(wav_path),
    ]

    print("Μίλησε τώρα...")

    t0 = time.perf_counter()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as e:
        print(f"Σφάλμα εκκίνησης FFmpeg: {e}")
        return None

    line_queue: "queue.Queue[str | None]" = queue.Queue()
    reader = threading.Thread(
        target=_read_lines, args=(process.stderr, line_queue), daemon=True
    )
    reader.start()

    stderr_lines: list[str] = []
    speech_detected = False
    leading_silence_pending = False
    stop_reason = "eof"

    while True:
        elapsed = time.perf_counter() - t0

        if elapsed >= MAX_RECORD_SECONDS:
            stop_reason = "max"
            break

        if leading_silence_pending and elapsed >= NO_SPEECH_TIMEOUT:
            stop_reason = "no_speech"
            break

        try:
            line = line_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        if line is None:
            stop_reason = "eof"
            break

        stderr_lines.append(line)

        if "silence_start" in line:
            match = SILENCE_START_RE.search(line)
            silence_at = float(match.group(1)) if match else 0.0

            if silence_at > LEADING_SILENCE_MAX:
                # Speech happened before this silence, and SILENCE_DURATION
                # has already elapsed by the time this line shows up.
                speech_detected = True
                stop_reason = "silence"
                break

            leading_silence_pending = True
        elif "silence_end" in line and leading_silence_pending:
            speech_detected = True
            leading_silence_pending = False

    if stop_reason in ("silence", "no_speech", "max") and process.poll() is None:
        try:
            process.stdin.write("q")
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    else:
        process.wait()

    reader.join(timeout=1)
    while True:
        try:
            line = line_queue.get_nowait()
        except queue.Empty:
            break
        if line is not None:
            stderr_lines.append(line)

    print(f"[timing] Recording: {time.perf_counter() - t0:.2f}s")

    if stop_reason == "eof" and process.returncode != 0:
        error = "".join(stderr_lines).strip()
        print(f"Σφάλμα μικροφώνου: {error or 'άγνωστο σφάλμα'}")
        return None

    if not wav_path.exists() or wav_path.stat().st_size == 0:
        print("Δεν δημιουργήθηκε σωστή ηχογράφηση.")
        return None

    try:
        model = _get_model()

        t0 = time.perf_counter()
        segments, info = model.transcribe(
            str(wav_path),
            language="el",
            vad_filter=True,
            beam_size=5,
            initial_prompt=INITIAL_PROMPT,
        )

        text = " ".join(
            segment.text.strip()
            for segment in segments
            if segment.text.strip()
        ).strip()
        print(f"[timing] Transcription: {time.perf_counter() - t0:.2f}s")

        if not text:
            print("Δεν κατάλαβα τι είπες.")
            return None

        return text

    except Exception as e:
        print(f"Σφάλμα Whisper: {e}")
        return None

    finally:
        try:
            wav_path.unlink(missing_ok=True)
        except OSError:
            pass


# --- The recording clock.
#
# Each frame carries the stream_time at which it was recorded: audio seconds
# since the device opened, counted for gated frames too. Audio is produced in
# real time even though it *arrives* in ~1s bursts, so
#
#     recorded_wall = _stream_origin + stream_time
#
# is when a frame was actually said. That mapping is what lets the capture
# gate and record_command share one clock; the pipeline's delay never enters
# it, because buffering changes when a frame arrives, not when it was spoken.
#
# _stream_origin is estimated as a rolling minimum of (wall_at_read -
# stream_time), which equals the true origin plus that frame's arrival delay.
# The minimum is therefore the tightest estimate available, and it corrects
# itself for the ~1.4s it takes DirectShow to hand over the first frame. The
# window keeps it from being pinned forever by one early sample if the driver
# later drops audio (which would make stream_time under-count real time).
ORIGIN_WINDOW_SECONDS = 30.0
ORIGIN_BUCKET_SECONDS = 1.0

_stream_origin: float | None = None
_origin_samples: "collections.deque[list[float]]" = collections.deque()

# Audio recorded before this stream_time is stale and skipped by every
# consumer. See flush().
_capture_floor = 0.0


def _note_origin(stream_time: float, wall: float) -> None:
    """Fold one (frame, arrival) pair into the _stream_origin estimate."""
    global _stream_origin

    bucket = stream_time // ORIGIN_BUCKET_SECONDS
    candidate = wall - stream_time

    if _origin_samples and _origin_samples[-1][0] == bucket:
        _origin_samples[-1][1] = min(_origin_samples[-1][1], candidate)
    else:
        _origin_samples.append([bucket, candidate])

    oldest = stream_time - ORIGIN_WINDOW_SECONDS
    while _origin_samples and _origin_samples[0][0] * ORIGIN_BUCKET_SECONDS < oldest:
        _origin_samples.popleft()

    _stream_origin = min(sample[1] for sample in _origin_samples)


def _should_capture(stream_time: float) -> bool:
    """Whether audio recorded at stream_time belongs in the pipeline.

    The question is about when the audio was *recorded*, not when it was
    read. ffmpeg hands over ~1s of audio at a time, so asking
    speaker.is_speaking() here -- as this did -- mutes the wrong second in
    both directions: it drops live audio while Jarvis's own voice, buffered
    from before he stopped, sails through a cooldown far too short to cover
    the delay. Comparing recorded times needs no such constant.
    """
    # Deferred import: speaker.py imports FFMPEG_PATH from this module, so
    # importing speaker at module level here would be circular.
    from jarvis import speaker

    origin = _stream_origin
    if origin is None:
        # First frames of the stream, no estimate yet. Fall back to the
        # cruder question; Jarvis isn't talking this early anyway.
        return not speaker.is_speaking()

    return not speaker.was_speaking(origin + stream_time)


def _enqueue_bounded(item_queue: "queue.Queue", item) -> None:
    """Put item on a bounded queue, dropping the oldest entry to make room
    instead of blocking if full — a sliding window of recent data."""
    while True:
        try:
            item_queue.put_nowait(item)
            return
        except queue.Full:
            try:
                item_queue.get_nowait()
            except queue.Empty:
                pass


def _drain(item_queue: "queue.Queue") -> None:
    """Discard everything currently queued without processing it."""
    while True:
        try:
            item_queue.get_nowait()
        except queue.Empty:
            return


_stream_process: subprocess.Popen | None = None
_stream_threads: list[threading.Thread] = []
_stream_audio_queue: "queue.Queue[tuple[float, bytes] | None] | None" = None
_stream_stderr_queue: "queue.Queue[tuple[float, str] | None] | None" = None
_stream_frames_read = 0
_stream_lock = threading.Lock()


def _stream_audio_reader(
    stdout, audio_queue: "queue.Queue[tuple[float, bytes] | None]"
) -> None:
    """Continuously drains ffmpeg's raw-PCM stdout so its pipe never backs
    up. Always advances the frame counter (so stream_time keeps pace with
    ffmpeg's own PTS/silencedetect clock even for dropped frames), but only
    enqueues frames for scoring/recording while not muted (see
    _should_capture) — this keeps Jarvis's own voice out of the pipeline
    entirely, not just filtered later. Each frame is tagged with the
    stream_time at which it was *recorded*, which is the only clock its
    consumers may reason with."""
    global _stream_frames_read

    buffer = b""
    try:
        while True:
            chunk = stdout.read(FRAME_BYTES - len(buffer))
            if not chunk:
                break
            buffer += chunk
            if len(buffer) < FRAME_BYTES:
                continue
            frame, buffer = buffer[:FRAME_BYTES], buffer[FRAME_BYTES:]
            stream_time = _stream_frames_read * FRAME_SECONDS  # start of frame
            _stream_frames_read += 1
            _note_origin(stream_time + FRAME_SECONDS, time.perf_counter())
            if _should_capture(stream_time):
                _enqueue_bounded(audio_queue, (stream_time, frame))
    except Exception:
        pass
    finally:
        audio_queue.put(None)


def _stream_stderr_reader(
    stderr, stderr_queue: "queue.Queue[tuple[float, str] | None]"
) -> None:
    """Mirrors _stream_audio_reader for stderr silencedetect lines: always
    drains stderr (ffmpeg must never block on a full stderr pipe either),
    decodes manually since the process runs in binary mode (stdout carries
    raw audio), and only enqueues while not muted, same as the audio
    frames — a silence event logged while Jarvis is talking is dropped at
    the same point, not just later at the listen_for_wake_word() flush."""
    try:
        for line_bytes in iter(stderr.readline, b""):
            line = line_bytes.decode("utf-8", errors="replace")
            stream_time = _stream_frames_read * FRAME_SECONDS
            if _should_capture(stream_time):
                _enqueue_bounded(stderr_queue, (stream_time, line))
    except Exception:
        pass
    finally:
        stderr_queue.put(None)


def start_stream() -> None:
    """Launch one persistent ffmpeg process, shared by listen_for_wake_word()
    and record_command(), so the microphone is opened exactly once. A no-op
    if already running. Raises on failure to start ffmpeg (caller decides
    how to fall back)."""
    global _stream_process, _stream_threads, _stream_audio_queue, _stream_stderr_queue, _stream_frames_read
    global _stream_origin, _capture_floor

    with _stream_lock:
        if _stream_process is not None:
            return

        command = [
            FFMPEG_PATH,
            "-hide_banner",
            "-loglevel",
            "info",
            "-nostats",
            "-f",
            "dshow",
            "-audio_buffer_size",
            "1000",
            "-i",
            f"audio={MICROPHONE_NAME}",
            "-af",
            f"silencedetect=noise={SILENCE_THRESHOLD_DB}dB:d={SILENCE_DURATION}",
            "-f",
            "s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            "pipe:1",
        ]

        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # stream_time restarts with the device, so everything keyed to it does
        # too: the origin estimate and the staleness floor.
        _stream_frames_read = 0
        _stream_origin = None
        _origin_samples.clear()
        _capture_floor = 0.0

        audio_queue: "queue.Queue[tuple[float, bytes] | None]" = queue.Queue(
            maxsize=AUDIO_QUEUE_MAXSIZE
        )
        stderr_queue: "queue.Queue[tuple[float, str] | None]" = queue.Queue(
            maxsize=STDERR_QUEUE_MAXSIZE
        )

        audio_thread = threading.Thread(
            target=_stream_audio_reader, args=(process.stdout, audio_queue), daemon=True
        )
        stderr_thread = threading.Thread(
            target=_stream_stderr_reader, args=(process.stderr, stderr_queue), daemon=True
        )
        audio_thread.start()
        stderr_thread.start()

        _stream_process = process
        _stream_threads = [audio_thread, stderr_thread]
        _stream_audio_queue = audio_queue
        _stream_stderr_queue = stderr_queue


def stop_stream() -> None:
    """Tear down the stream started by start_stream(). Idempotent: safe to
    call even if it was never started or already stopped."""
    global _stream_process, _stream_threads, _stream_audio_queue, _stream_stderr_queue

    with _stream_lock:
        process = _stream_process
        if process is None:
            return

        if process.poll() is None:
            try:
                process.stdin.write(b"q")
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

        for thread in _stream_threads:
            thread.join(timeout=1)

        _stream_process = None
        _stream_threads = []
        _stream_audio_queue = None
        _stream_stderr_queue = None


def _debug(message: str) -> None:
    if WAKE_DEBUG:
        print(message)


def _frame_db(frame: bytes) -> float:
    """RMS level of one frame in dBFS, comparable to SILENCE_THRESHOLD_DB
    (the same unit ffmpeg's silencedetect noise= option takes)."""
    samples = array.array("h")
    samples.frombytes(frame)
    if not samples:
        return SILENT_DB

    total = 0
    for sample in samples:
        total += sample * sample
    rms = math.sqrt(total / len(samples))

    if rms <= 0:
        return SILENT_DB
    return 20 * math.log10(rms / 32768.0)


def flush() -> None:
    """Discard everything recorded up to this instant, so the next turn
    starts clean.

    A watermark, not just a drain. Emptying the queues was never enough:
    ffmpeg still held the same second of audio in its own buffer and handed
    it over immediately afterwards, so the reply Jarvis had just finished
    speaking came back as the next turn's opening words. Raising the floor
    drops that second wherever it happens to be sitting — while anything
    recorded *after* this instant survives, which draining could never
    manage."""
    global _capture_floor

    if _stream_origin is not None:
        _capture_floor = max(_capture_floor, time.perf_counter() - _stream_origin)

    if _stream_audio_queue is not None:
        _drain(_stream_audio_queue)
    if _stream_stderr_queue is not None:
        _drain(_stream_stderr_queue)


def acknowledge() -> None:
    """Signal "go ahead" after the wake word fired, then clear the queues.

    The flush is the point: it drops the wake word itself, the beep, and
    the room's echo of the beep, so record_command() starts from nothing
    and Whisper never sees the wake word as the command. A no-op when
    WAKE_BEEP is false — with no beep there is nothing to signal and
    nothing to flush, and the caller keeps the pre-roll instead."""
    if not WAKE_BEEP:
        return

    # Deferred import: speaker.py imports FFMPEG_PATH from this module.
    from jarvis import speaker

    speaker.beep_ready()
    flush()


def listen_for_wake_word() -> bytes:
    """Block until the wake word fires, returning the ~1s of audio ending
    with the frame that fired (the pre-roll).

    The caller uses that pre-roll only when WAKE_BEEP is false; with the
    beep on, acknowledge() flushes instead and the pre-roll is discarded,
    since it necessarily contains the wake word.

    Both queues are flushed on entry so a backlog from the previous turn
    isn't treated as fresh audio, and the model is reset — openWakeWord
    scores each frame from the ~1.5s before it, so without a reset the
    wake word still sitting in its buffer re-fires immediately. Frames are
    fed but detections ignored for WAKE_RETRIGGER_COOLDOWN after that
    reset, giving the emptied buffer time to refill with fresh audio."""
    audio_queue = _stream_audio_queue
    stderr_queue = _stream_stderr_queue
    if audio_queue is None or stderr_queue is None:
        raise RuntimeError("Wake-word stream not started; call start_stream() first.")

    _drain(audio_queue)
    _drain(stderr_queue)
    wakeword.reset()

    preroll: "collections.deque[bytes]" = collections.deque(maxlen=PREROLL_FRAMES)
    cooldown_frames = int(WAKE_RETRIGGER_COOLDOWN / FRAME_SECONDS)
    frames_seen = 0
    last_ts: float | None = None

    print("Πες «Hey Jarvis»...")

    while True:
        item = audio_queue.get()
        if item is None:
            raise RuntimeError("Η ροή μικροφώνου τερμάτισε απρόσμενα.")

        ts, frame = item
        if ts < _capture_floor:
            continue  # recorded before the last flush; stale by definition

        if last_ts is not None and ts - last_ts > FRAME_SECONDS + GAP_TOLERANCE:
            # Frames are missing (Jarvis was talking, or the queue overran).
            # openWakeWord scores each frame from the ~1.5s of audio before
            # it, so feeding it audio welded across a hole leaves that window
            # holding two different moments spliced together. The buffer is
            # invalid either way, so clear it and re-serve the cooldown.
            _debug(f"[wake] gap of {ts - last_ts - FRAME_SECONDS:.2f}s, model reset")
            wakeword.reset()
            preroll.clear()
            frames_seen = 0

        last_ts = ts
        frames_seen += 1

        # Scored even during the cooldown: the model needs the frames to
        # rebuild its buffer, we just don't act on what it says yet.
        fired = wakeword.detect(frame)
        preroll.append(frame)  # includes the firing frame, not just before it

        if fired:
            if frames_seen <= cooldown_frames:
                _debug(
                    f"[wake] ignored (cooldown, "
                    f"{frames_seen * FRAME_SECONDS:.2f}s of "
                    f"{WAKE_RETRIGGER_COOLDOWN:.2f}s)"
                )
                continue

            wakeword.reset()  # so the tail of this utterance can't re-fire
            _debug(f"[wake] fired after {frames_seen * FRAME_SECONDS:.2f}s")
            return b"".join(preroll)


def _silence(seconds: float) -> bytes:
    """PCM silence, for padding a hole to its real length."""
    return b"\x00\x00" * int(round(seconds * SAMPLE_RATE))


class _StopDecider:
    """When to stop recording, decided from (timestamp, level) pairs alone.

    Every threshold is in stream time — the clock each frame was recorded
    on — so the decision no longer depends on when frames arrive. Counting
    frames instead, as this used to, measured neither audio nor time: one
    ~1s burst arrives in a millisecond and could satisfy the whole
    end-of-speech rule on its own, while a 15s frame budget collected over a
    starved stream ran 43s of wall clock without a single stop condition
    noticing.

    Holes are therefore classified rather than papered over:

      * contiguous      — the ordinary case;
      * a small hole    — padded with real silence and counted as quiet,
                          because quiet is what the microphone heard;
      * a large one, or — too much of the utterance is missing to transcribe
        too much lost     honestly. Whisper would hear the surviving words
        in total          welded together and answer confidently anyway, so
                          stop with "gap" and let the caller ask again. Same
                          principle as the fuzzy triggers' third guard: a
                          silent wrong answer costs more than another try.

    Pure: no clock, no queue, no I/O. Tests drive it from a list of frames.
    """

    def __init__(
        self,
        no_speech_timeout: float,
        max_seconds: float | None = None,
    ) -> None:
        self.no_speech_timeout = no_speech_timeout
        self.max_seconds = MAX_RECORD_SECONDS if max_seconds is None else max_seconds

        self.speech_started = False
        self.pad_seconds = 0.0  # silence to insert before the frame just fed
        self.lost_seconds = 0.0  # audio that never arrived at all
        self.audio_seconds = 0.0  # recorded time spanned so far

        self._origin: float | None = None
        self._end = 0.0  # stream time just after the last frame fed
        self._loud_since: float | None = None
        self._silent_since: float | None = None

    def feed(self, ts: float, level_db: float) -> str | None:
        """Feed one frame — ts is when it was recorded, level_db its level.

        Returns None to keep recording, or the reason to stop: "speech_end",
        "no_speech", "gap" or "max".
        """
        self.pad_seconds = 0.0

        if self._origin is None:
            self._origin = ts
        else:
            gap = ts - self._end
            if gap > GAP_TOLERANCE:
                self.lost_seconds += gap
                if gap > MAX_GAP_SECONDS or self.lost_seconds > MAX_LOST_SECONDS:
                    return "gap"

                self.pad_seconds = gap
                # Nothing was kept from that stretch, so it counts as quiet —
                # and it breaks any run of loud frames straddling it.
                self._loud_since = None
                if self._silent_since is None:
                    self._silent_since = self._end

        self._end = ts + FRAME_SECONDS
        self.audio_seconds = self._end - self._origin

        if level_db > SILENCE_THRESHOLD_DB:
            self._silent_since = None
            if self._loud_since is None:
                self._loud_since = ts
            if not self.speech_started and self._end - self._loud_since >= ONSET_SECONDS:
                self.speech_started = True
        else:
            self._loud_since = None
            if self._silent_since is None:
                self._silent_since = ts
            if self.speech_started and self._end - self._silent_since >= SILENCE_DURATION:
                return "speech_end"

        if not self.speech_started and self.audio_seconds >= self.no_speech_timeout:
            return "no_speech"
        if self.audio_seconds >= self.max_seconds:
            return "max"
        return None


def record_command(
    preroll: bytes = b"",
    no_speech_timeout: float = NO_SPEECH_TIMEOUT,
) -> tuple[str | None, str]:
    """Record one spoken command off the persistent stream and transcribe it.

    Waits for speech to actually start (ONSET_SECONDS above
    SILENCE_THRESHOLD_DB) and only then stops on silence — SILENCE_DURATION
    of continuous quiet. Nothing said within no_speech_timeout stops with
    "no_speech" and no transcription at all; that is what ends conversation
    mode. Hard cap either way: MAX_RECORD_SECONDS of audio, and a wall-clock
    backstop above that for when audio stops arriving.

    _StopDecider makes every one of those calls, on the clock the frames
    were recorded on. This loop only moves bytes: read a frame, skip it if
    it predates the last flush, pad any hole to its true length, and stop
    when told.

    preroll is prepended to the audio and is used only on the WAKE_BEEP=false
    path; follow-up turns in conversation mode always pass nothing.

    Returns (text, stop_reason), where stop_reason is one of "speech_end",
    "no_speech", "gap", "max", "timeout" or "stream_end" — the caller needs
    it to tell "you finished talking" from "you never started" from "what
    reached me had holes in it"."""
    audio_queue = _stream_audio_queue
    stderr_queue = _stream_stderr_queue
    if audio_queue is None or stderr_queue is None:
        raise RuntimeError("Wake-word stream not started; call start_stream() first.")

    # silencedetect lines no longer drive this loop, but the queue still
    # fills during the idle period; clear it so it doesn't go stale.
    _drain(stderr_queue)

    t0 = time.perf_counter()
    audio = bytearray(preroll)
    decider = _StopDecider(no_speech_timeout)
    captured_frames = 0
    stop_reason = "running"  # never a stop condition; each break sets a real one
    last_level_print = t0
    first_frame_at: float | None = None  # wall clock, for the invariant below

    # The backstop for "audio stopped arriving", in wall clock because that
    # is the failure it catches. Generous: every real stop is the decider's.
    wall_budget = MAX_RECORD_SECONDS + no_speech_timeout

    print("Μίλησε τώρα...")
    _debug(
        f"[rec] floor {SILENCE_THRESHOLD_DB} dB, no-speech timeout "
        f"{no_speech_timeout:.1f}s, pre-roll {len(preroll) / FRAME_BYTES:.0f} frames"
    )

    while True:
        # Checked every pass, not only when the queue starves. Living inside
        # the queue.Empty branch made it unreachable for exactly the failure
        # it existed for: a trickle of frames kept the queue non-empty while
        # the clock ran to 43 seconds.
        if time.perf_counter() - t0 >= wall_budget:
            stop_reason = "timeout"
            break

        try:
            item = audio_queue.get(timeout=0.2)
        except queue.Empty:
            continue

        if item is None:
            stop_reason = "stream_end"
            break

        ts, frame = item
        if ts < _capture_floor:
            continue  # recorded before the last flush; stale by definition

        if first_frame_at is None:
            first_frame_at = time.perf_counter()

        level = _frame_db(frame)
        was_started = decider.speech_started
        verdict = decider.feed(ts, level)

        if verdict == "gap":
            stop_reason = "gap"
            break

        if decider.pad_seconds:
            # Keep the WAV honest about how long the pause really was:
            # concatenating across a hole welds words together, and Whisper
            # transcribes the weld.
            _debug(f"[rec] padded a {decider.pad_seconds:.2f}s hole with silence")
            audio += _silence(decider.pad_seconds)

        audio += frame
        captured_frames += 1

        if decider.speech_started and not was_started:
            _debug(
                f"[rec] speech started at "
                f"{decider.audio_seconds:.2f}s ({level:.1f} dB)"
            )
        elif not decider.speech_started:
            now = time.perf_counter()
            if WAKE_DEBUG and now - last_level_print >= DEBUG_LEVEL_INTERVAL:
                last_level_print = now
                print(
                    f"[rec] waiting for speech: {level:.1f} dB "
                    f"(floor {SILENCE_THRESHOLD_DB} dB, "
                    f"{decider.audio_seconds:.1f}s of {no_speech_timeout:.1f}s)"
                )

        if verdict is not None:
            stop_reason = verdict
            break

    ended_at = time.perf_counter()
    wall = ended_at - t0

    # Both clocks, always: the wall-clock number on its own is what hid this
    # whole class of bug.
    print(f"[timing] Recording: {wall:.2f}s wall / {decider.audio_seconds:.2f}s audio")

    # The invariant, split so it means something. Waiting for ffmpeg's buffer
    # to reach the new floor is a fixed ~1s cost at the start of every turn
    # and says nothing about health; the ratio *after* frames start flowing
    # is what should sit near 1.00, and what drops when audio is going
    # missing. A future regression should be visible in this line alone.
    waiting = (first_frame_at - t0) if first_frame_at else wall
    recording = (ended_at - first_frame_at) if first_frame_at else 0.0
    _debug(
        f"[rec] stopped: {stop_reason} after {captured_frames} frames "
        f"({decider.audio_seconds:.2f}s audio in {wall:.2f}s wall: "
        f"{waiting:.2f}s waiting for the pipeline, then "
        f"{decider.audio_seconds / recording if recording else 0:.2f} audio/wall; "
        f"{decider.lost_seconds:.2f}s lost to gaps, "
        f"speech_started={decider.speech_started})"
    )

    if stop_reason == "gap":
        # Deliberately not transcribed. What survived has holes in it, and a
        # confident wrong transcript costs more than asking again.
        return None, stop_reason

    if not decider.speech_started:
        # Nothing was said. Transcribing pure silence only invites Whisper
        # to hallucinate a phrase out of room noise.
        return None, stop_reason

    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(bytes(audio))
    wav_buffer.seek(0)

    try:
        model = _get_model()

        t0 = time.perf_counter()
        segments, info = model.transcribe(
            wav_buffer,
            language="el",
            vad_filter=True,
            beam_size=5,
            initial_prompt=INITIAL_PROMPT,
        )

        text = " ".join(
            segment.text.strip()
            for segment in segments
            if segment.text.strip()
        ).strip()
        print(f"[timing] Transcription: {time.perf_counter() - t0:.2f}s")

        if not text:
            print("Δεν κατάλαβα τι είπες.")
            return None, stop_reason

        return text, stop_reason

    except Exception as e:
        print(f"Σφάλμα Whisper: {e}")
        return None, stop_reason
