import io
import time
import wave
import winsound
from pathlib import Path

from piper import PiperVoice

from jarvis.config import PIPER_MODEL_PATH

_voice = PiperVoice.load(Path(PIPER_MODEL_PATH))


def speak(text: str) -> None:
    t0 = time.perf_counter()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        _voice.synthesize_wav(text, wav_file)
    print(f"[timing] Piper synthesis: {time.perf_counter() - t0:.2f}s")
    winsound.PlaySound(buffer.getvalue(), winsound.SND_MEMORY)
