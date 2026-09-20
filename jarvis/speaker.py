import asyncio
import io
import subprocess
import tempfile
import threading
import time
import wave
import winsound
from pathlib import Path

import edge_tts
from piper import PiperVoice

from jarvis.config import PIPER_MODEL_PATH, TTS_ENGINE, TTS_VOICE
from jarvis.listener import FFMPEG_PATH

_voice = PiperVoice.load(Path(PIPER_MODEL_PATH))


def _speak_piper(text: str) -> None:
    t0 = time.perf_counter()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        _voice.synthesize_wav(text, wav_file)
    print(f"[timing] Piper synthesis: {time.perf_counter() - t0:.2f}s")
    winsound.PlaySound(buffer.getvalue(), winsound.SND_MEMORY)


def _speak_edge(text: str) -> bool:
    """Synthesize with Microsoft Edge TTS. Returns False on any failure
    (e.g. no internet) so the caller can fall back to Piper."""
    t0 = time.perf_counter()
    mp3_path = Path(tempfile.gettempdir()) / "jarvis_tts.mp3"
    wav_path = Path(tempfile.gettempdir()) / "jarvis_tts.wav"

    try:
        asyncio.run(edge_tts.Communicate(text, TTS_VOICE).save(str(mp3_path)))

        subprocess.run(
            [FFMPEG_PATH, "-y", "-loglevel", "error", "-i", str(mp3_path), str(wav_path)],
            check=True,
            capture_output=True,
        )

        wav_bytes = wav_path.read_bytes()
    except Exception as e:
        print(f"Σφάλμα Edge TTS: {e}")
        return False
    finally:
        mp3_path.unlink(missing_ok=True)
        wav_path.unlink(missing_ok=True)

    print(f"[timing] Edge TTS synthesis: {time.perf_counter() - t0:.2f}s")
    winsound.PlaySound(wav_bytes, winsound.SND_MEMORY)
    return True


# Add "elevenlabs" here later (not implemented yet). Piper is the
# always-available fallback/default, so it deliberately isn't listed here.
_VOICE_PROVIDERS = {
    "edge": _speak_edge,
}

# Serializes playback: speak() can be called from the main loop and from a
# skill timer's background thread (see jarvis/skills.py), and winsound has no
# concept of concurrent sounds — a second PlaySound call while one is still
# running cuts it off instead of queuing. This makes a concurrent call simply
# wait its turn.
_lock = threading.Lock()


def is_speaking() -> bool:
    """True while speak() is actively playing audio (it holds _lock for the
    whole synchronous playback). Used by the wake-word listener to avoid
    scoring Jarvis's own voice."""
    return _lock.locked()


def speak(text: str) -> None:
    with _lock:
        provider = _VOICE_PROVIDERS.get(TTS_ENGINE)
        if provider is not None:
            if provider(text):
                return
            print("Edge TTS απέτυχε (πιθανώς χωρίς σύνδεση), χρήση Piper.")

        _speak_piper(text)
