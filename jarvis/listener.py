from __future__ import annotations

import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from faster_whisper import WhisperModel

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

INITIAL_PROMPT = (
    "Τζάρβις. Γεια σου Τζάρβις, τι κάνεις; Τζάρβις, τι ώρα είναι; "
    "Τζάρβις, πες μου κάτι."
)

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

    Recording stops ~1s after the speaker falls silent, using ffmpeg's
    silencedetect filter on the live stream. A silence right at the start
    (before any speech) doesn't count, so a slow start isn't cut off. Hard
    limits: MAX_RECORD_SECONDS overall, NO_SPEECH_TIMEOUT if nothing is
    ever said.
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
    stop_reason = "eof"

    while True:
        elapsed = time.perf_counter() - t0

        if elapsed >= MAX_RECORD_SECONDS:
            stop_reason = "max"
            break

        if not speech_detected and elapsed >= NO_SPEECH_TIMEOUT:
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

        if "silence_end" in line:
            speech_detected = True
        elif "silence_start" in line and speech_detected:
            stop_reason = "silence"
            break

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