from __future__ import annotations

import asyncio
import collections
import contextlib
import io
import subprocess
import tempfile
import threading
import time
import wave
import winsound
from pathlib import Path

import edge_tts

from jarvis import diag
from jarvis.config import PIPER_MODEL_PATH, TTS_ENGINE, TTS_VOICE
from jarvis.listener import FFMPEG_PATH

# Loaded on first use, not at import: the model is ~63MB off disk, and with
# TTS_ENGINE=edge it is only ever needed if Edge synthesis fails. Importing
# this module (which jarvis/skills.py does, transitively) must stay cheap.
# Same lazy-singleton idiom as listener._get_model and wakeword._get_model.
_voice = None


def _get_voice():
    global _voice

    if _voice is None:
        # Deferred with the load for the same reason: importing this module
        # shouldn't require the piper package when Piper is never used.
        from piper import PiperVoice

        print("Φόρτωση Piper...")
        t0 = time.perf_counter()
        _voice = PiperVoice.load(Path(PIPER_MODEL_PATH))
        diag.log(f"[timing] Piper load: {time.perf_counter() - t0:.2f}s")

    return _voice


def _speak_piper(text: str) -> None:
    voice = _get_voice()

    t0 = time.perf_counter()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file)
    diag.log(f"[timing] Piper synthesis: {time.perf_counter() - t0:.2f}s")
    with _playing():
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

    diag.log(f"[timing] Edge TTS synthesis: {time.perf_counter() - t0:.2f}s")
    with _playing():
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
    """True while speak() is running -- synthesis included, since it holds
    _lock for the whole call. Only a fallback for the capture gate now, used
    before the stream has an origin estimate; was_speaking() is the real
    question. See listener._should_capture."""
    return _lock.locked()


# How long after a sound stops before captured audio is trusted again. This
# is the room's echo of it and nothing else: the pipeline's ~1s delay is
# handled by comparing *recorded* times (listener._should_capture), not by
# padding this. It was doing both jobs before, and was three times too small
# for the second one.
ECHO_PAD = 0.4

# Closed (start, end) wall-clock intervals during which Jarvis was audible,
# newest last. A handful is plenty: a frame older than the last few seconds
# is long gone from the stream queue either way.
_intervals: collections.deque[tuple[float, float]] = collections.deque(maxlen=8)
_intervals_lock = threading.Lock()

# Start of the interval currently open, while a sound is actually playing.
_playback_start: float | None = None


@contextlib.contextmanager
def _playing():
    """Mark the wall-clock interval during which a sound is actually audible.

    Wrapped around playback only, not around synthesis: Edge TTS spends a
    second on the network making no sound at all, and muting the microphone
    through it would throw away audio the user really did speak.
    """
    global _playback_start

    with _intervals_lock:
        _playback_start = time.perf_counter()
    try:
        yield
    finally:
        with _intervals_lock:
            _intervals.append((_playback_start, time.perf_counter()))
            _playback_start = None


def was_speaking(at_wall: float, pad: float = ECHO_PAD) -> bool:
    """True if Jarvis was audible at wall-clock time at_wall (or within pad
    afterwards, while the room was still ringing with it).

    Unlike is_speaking(), this answers about a moment in the *past*, which is
    the only answerable form of the question for the capture gate: a frame
    reaches the gate up to a second after the audio in it was recorded. See
    listener._should_capture.

    Deliberately not padded before `start`: audio recorded just before Jarvis
    opened his mouth is the user's, and keeping it is the point.
    """
    with _intervals_lock:
        open_start = _playback_start
        intervals = list(_intervals)

    if open_start is not None and at_wall >= open_start:
        return True  # still playing, so it has no end to compare against yet

    return any(start <= at_wall <= end + pad for start, end in intervals)


# Short non-speech cues for wake-word/conversation mode (see main.py).
# Played under _lock and _playing() like speak(), so the capture gate drops
# the frames recorded while they sound -- and, just as importantly, keeps the
# ones recorded before they started.
READY_BEEP = (880, 120)  # wake word fired, go ahead
DONE_BEEP = (523, 160)  # back to waiting for the wake word


def _beep(frequency: int, duration_ms: int) -> None:
    with _lock, _playing():
        try:
            winsound.Beep(frequency, duration_ms)
        except RuntimeError as e:
            # No audio device / beep unsupported: a missing cue must never
            # take down the conversation loop.
            print(f"Σφάλμα ήχου: {e}")


def beep_ready() -> None:
    _beep(*READY_BEEP)


def beep_done() -> None:
    _beep(*DONE_BEEP)


def speak(text: str) -> None:
    with _lock:
        provider = _VOICE_PROVIDERS.get(TTS_ENGINE)
        if provider is not None:
            if provider(text):
                return
            print("Edge TTS απέτυχε (πιθανώς χωρίς σύνδεση), χρήση Piper.")

        _speak_piper(text)
