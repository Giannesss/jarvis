from __future__ import annotations

import collections
import io
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


def listen_for_wake_word() -> tuple[bytes, float]:
    """Block until the wake word fires. Flushes both queues first so a
    backlog from the previous turn (e.g. queued silence while brain.ask()
    ran) isn't treated as fresh audio. Returns (preroll_bytes,
    segment_start_time): the ~1s of audio just before the trigger (so
    words spoken in the same breath as the wake word aren't lost), and the
    stream's internal clock at the exact frame that fired, for
    record_command() to rebase its silence timing against."""
    audio_queue = _stream_audio_queue
    stderr_queue = _stream_stderr_queue
    if audio_queue is None or stderr_queue is None:
        raise RuntimeError("Wake-word stream not started; call start_stream() first.")

    _drain(audio_queue)
    _drain(stderr_queue)

    preroll: "collections.deque[bytes]" = collections.deque(maxlen=PREROLL_FRAMES)

    print("Πες «Hey Jarvis»...")

    while True:
        item = audio_queue.get()
        if item is None:
            raise RuntimeError("Η ροή μικροφώνου τερμάτισε απρόσμενα.")
        stream_time, frame = item
        if wakeword.detect(frame):
            return b"".join(preroll), stream_time
        preroll.append(frame)


def record_command(preroll: bytes, segment_start_time: float) -> str | None:
    """Continue recording after listen_for_wake_word() fires, reusing the
    same persistent stream (mic opened only once for the whole run). Same
    silence/timeout state machine as listen() (see its docstring), rebased
    to segment_start_time — the moment the wake word fired, not ffmpeg's
    process start (which no longer resets per utterance) and not the start
    of the pre-roll — so a natural pause right after the wake word still
    reads as leading silence instead of ending the recording early."""
    audio_queue = _stream_audio_queue
    stderr_queue = _stream_stderr_queue
    if audio_queue is None or stderr_queue is None:
        raise RuntimeError("Wake-word stream not started; call start_stream() first.")

    _drain(stderr_queue)  # discard idle-period leftovers from before the trigger

    t0 = time.perf_counter()
    audio = bytearray(preroll)
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

        got_something = False

        while True:
            try:
                item = stderr_queue.get_nowait()
            except queue.Empty:
                break
            got_something = True
            if item is None:
                stop_reason = "eof"
                break
            stream_time, line = item
            relative = stream_time - segment_start_time

            if "silence_start" in line:
                if relative > LEADING_SILENCE_MAX:
                    stop_reason = "silence"
                else:
                    leading_silence_pending = True
            elif "silence_end" in line and leading_silence_pending:
                leading_silence_pending = False

        if stop_reason in ("eof", "silence"):
            break

        while True:
            try:
                item = audio_queue.get_nowait()
            except queue.Empty:
                break
            got_something = True
            if item is None:
                stop_reason = "eof"
                break
            _, frame = item
            audio += frame

        if stop_reason == "eof":
            break

        if not got_something:
            time.sleep(0.02)

    print(f"[timing] Recording: {time.perf_counter() - t0:.2f}s")

    if not audio:
        print("Δεν δημιουργήθηκε σωστή ηχογράφηση.")
        return None

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
            return None

        return text

    except Exception as e:
        print(f"Σφάλμα Whisper: {e}")
        return None