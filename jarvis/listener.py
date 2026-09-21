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

FRAME_SAMPLES = 1280  # 80ms at 16kHz
FRAME_BYTES = FRAME_SAMPLES * 2  # 16-bit samples = 2560 bytes/frame
FRAME_SECONDS = FRAME_SAMPLES / 16000

AUDIO_QUEUE_SECONDS = 10  # generous slack; the consumer loops poll faster
AUDIO_QUEUE_MAXSIZE = int(AUDIO_QUEUE_SECONDS / FRAME_SECONDS)
STDERR_QUEUE_MAXSIZE = 200

# How long after speaker.speak() stops before captured audio is trusted
# again (room echo / playback buffering tail).
WAKE_WORD_COOLDOWN = 0.4

PREROLL_SECONDS = 1.0
PREROLL_FRAMES = int(PREROLL_SECONDS / FRAME_SECONDS)

# --- record_command() speech detection. It measures each 80ms frame's own
# level instead of reading ffmpeg's silencedetect lines: every frame is
# already in hand, so the decision is drift-free and immune to the delay
# between a silence starting and ffmpeg logging it (silencedetect only
# reports a silence after SILENCE_DURATION of it has already passed, which
# made a natural pause right after the wake word look like end-of-speech).

# Consecutive frames above the noise floor before speech is considered
# started — one frame alone is usually a click or a door.
SPEECH_ONSET_FRAMES = 2

# Consecutive frames below the noise floor that end the recording, once
# speech has actually started.
SILENCE_STOP_FRAMES = max(1, round(SILENCE_DURATION / FRAME_SECONDS))

MAX_RECORD_FRAMES = int(MAX_RECORD_SECONDS / FRAME_SECONDS)

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


class _CaptureGate:
    """Tracks whether incoming stream data should be captured, muting
    while speaker.speak() is playing and for WAKE_WORD_COOLDOWN afterward
    so Jarvis never hears its own voice. One instance per reader thread."""

    def __init__(self) -> None:
        self._mute_until = 0.0

    def should_capture(self) -> bool:
        # Deferred import: speaker.py imports FFMPEG_PATH from this module,
        # so importing speaker at module level here would be circular.
        from jarvis import speaker

        now = time.perf_counter()
        if speaker.is_speaking():
            self._mute_until = now + WAKE_WORD_COOLDOWN
            return False
        return now >= self._mute_until


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
    _CaptureGate) — this keeps Jarvis's own voice out of the pipeline
    entirely, not just filtered later."""
    global _stream_frames_read

    gate = _CaptureGate()
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
            _stream_frames_read += 1
            stream_time = _stream_frames_read * FRAME_SECONDS
            if gate.should_capture():
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
    gate = _CaptureGate()
    try:
        for line_bytes in iter(stderr.readline, b""):
            line = line_bytes.decode("utf-8", errors="replace")
            stream_time = _stream_frames_read * FRAME_SECONDS
            if gate.should_capture():
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

        _stream_frames_read = 0
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
    """Throw away everything captured so far on both stream queues.

    Called after every spoken reply in conversation mode so the next turn
    starts from silence: the capture gate already drops frames while
    speaker.speak() runs, but anything queued just before it started
    talking would otherwise be recorded as if the user had said it."""
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

    print("Πες «Hey Jarvis»...")

    while True:
        item = audio_queue.get()
        if item is None:
            raise RuntimeError("Η ροή μικροφώνου τερμάτισε απρόσμενα.")

        _, frame = item
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


def record_command(
    preroll: bytes = b"",
    no_speech_timeout: float = NO_SPEECH_TIMEOUT,
) -> tuple[str | None, str]:
    """Record one spoken command off the persistent stream and transcribe it.

    Waits for speech to actually start (SPEECH_ONSET_FRAMES frames above
    SILENCE_THRESHOLD_DB) and only then stops on silence — SILENCE_DURATION
    of continuous quiet. Nothing said within no_speech_timeout stops with
    "no_speech" and no transcription at all; that is what ends conversation
    mode. Hard cap either way: MAX_RECORD_SECONDS.

    preroll is prepended to the audio and is used only on the WAKE_BEEP=false
    path; follow-up turns in conversation mode always pass nothing.

    Returns (text, stop_reason), where stop_reason is one of "speech_end",
    "no_speech", "max" or "stream_end" — the caller needs it to tell "you
    finished talking" from "you never started"."""
    audio_queue = _stream_audio_queue
    stderr_queue = _stream_stderr_queue
    if audio_queue is None or stderr_queue is None:
        raise RuntimeError("Wake-word stream not started; call start_stream() first.")

    # silencedetect lines no longer drive this loop, but the queue still
    # fills during the idle period; clear it so it doesn't go stale.
    _drain(stderr_queue)

    t0 = time.perf_counter()
    audio = bytearray(preroll)
    captured_frames = 0
    loud_run = 0  # consecutive loud frames, before speech has started
    silent_run = 0  # consecutive quiet frames, after speech has started
    speech_started = False
    stop_reason = "running"  # never a stop condition; each break sets a real one
    last_level_print = t0

    no_speech_frames = max(1, int(no_speech_timeout / FRAME_SECONDS))

    print("Μίλησε τώρα...")
    _debug(
        f"[rec] floor {SILENCE_THRESHOLD_DB} dB, no-speech timeout "
        f"{no_speech_timeout:.1f}s, pre-roll {len(preroll) / FRAME_BYTES:.0f} frames"
    )

    while True:
        try:
            item = audio_queue.get(timeout=0.2)
        except queue.Empty:
            # No frames at all (e.g. capture muted while a timer
            # announcement plays): fall back to wall clock so this can't hang.
            if time.perf_counter() - t0 >= MAX_RECORD_SECONDS + no_speech_timeout:
                stop_reason = "max"
                break
            continue

        if item is None:
            stop_reason = "stream_end"
            break

        _, frame = item
        audio += frame
        captured_frames += 1

        level = _frame_db(frame)
        loud = level > SILENCE_THRESHOLD_DB

        if not speech_started:
            now = time.perf_counter()
            if WAKE_DEBUG and now - last_level_print >= DEBUG_LEVEL_INTERVAL:
                last_level_print = now
                print(
                    f"[rec] waiting for speech: {level:.1f} dB "
                    f"(floor {SILENCE_THRESHOLD_DB} dB, "
                    f"{captured_frames * FRAME_SECONDS:.1f}s of "
                    f"{no_speech_timeout:.1f}s)"
                )

            loud_run = loud_run + 1 if loud else 0
            if loud_run >= SPEECH_ONSET_FRAMES:
                speech_started = True
                silent_run = 0
                _debug(
                    f"[rec] speech started at "
                    f"{captured_frames * FRAME_SECONDS:.2f}s ({level:.1f} dB)"
                )
            elif captured_frames >= no_speech_frames:
                stop_reason = "no_speech"
                break
        elif loud:
            silent_run = 0
        else:
            silent_run += 1
            if silent_run >= SILENCE_STOP_FRAMES:
                stop_reason = "speech_end"
                break

        if captured_frames >= MAX_RECORD_FRAMES:
            stop_reason = "max"
            break

    print(f"[timing] Recording: {time.perf_counter() - t0:.2f}s")
    _debug(
        f"[rec] stopped: {stop_reason} after {captured_frames} frames "
        f"({captured_frames * FRAME_SECONDS:.2f}s captured, "
        f"speech_started={speech_started})"
    )

    if not speech_started:
        # Nothing was said. Transcribing pure silence only invites Whisper
        # to hallucinate a phrase out of room noise.
        return None, stop_reason

    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
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
