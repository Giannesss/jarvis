from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from faster_whisper import WhisperModel


FFMPEG_PATH = (
    r"C:\Users\User\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe"
)

MICROPHONE_NAME = "Μικρόφωνο (Razer Seiren Mini)"
RECORD_SECONDS = 6

_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _model

    if _model is None:
        print("Φόρτωση Whisper...")
        _model = WhisperModel(
            "base",
            device="cpu",
            compute_type="int8",
        )

    return _model


def listen() -> str | None:
    """Record from the Razer microphone and transcribe locally with Whisper."""

    wav_path = Path(tempfile.gettempdir()) / "jarvis_mic.wav"

    command = [
        FFMPEG_PATH,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "dshow",
        "-audio_buffer_size",
        "1000",
        "-i",
        f"audio={MICROPHONE_NAME}",
        "-t",
        str(RECORD_SECONDS),
        "-c:a",
        "pcm_s16le",
        str(wav_path),
    ]

    print("Μίλησε τώρα...")

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=RECORD_SECONDS + 10,
        )
    except subprocess.TimeoutExpired:
        print("Timeout στην ηχογράφηση.")
        return None
    except OSError as e:
        print(f"Σφάλμα εκκίνησης FFmpeg: {e}")
        return None

    if result.returncode != 0:
        error = result.stderr.strip()
        print(f"Σφάλμα μικροφώνου: {error or 'άγνωστο σφάλμα'}")
        return None

    if not wav_path.exists() or wav_path.stat().st_size == 0:
        print("Δεν δημιουργήθηκε σωστή ηχογράφηση.")
        return None

    try:
        model = _get_model()

        segments, info = model.transcribe(
            str(wav_path),
            language="el",
            vad_filter=True,
            beam_size=5,
        )

        text = " ".join(
            segment.text.strip()
            for segment in segments
            if segment.text.strip()
        ).strip()

        if not text:
            print("Δεν κατάλαβα τι είπες.")
            return None

        print(f"Εσύ: {text}")
        return text

    except Exception as e:
        print(f"Σφάλμα Whisper: {e}")
        return None

    finally:
        try:
            wav_path.unlink(missing_ok=True)
        except OSError:
            pass