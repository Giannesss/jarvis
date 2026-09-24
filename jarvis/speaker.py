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

from jarvis import diag, player
from jarvis.config import PIPER_MODEL_PATH, TTS_ENGINE, TTS_VOICE
from jarvis.listener import FFMPEG_PATH

# What ffmpeg is told to decode Edge's mp3 into. One rate for the whole
# module keeps the player simple; Piper's rate is read from the WAV it
# writes, since a different voice can use a different one.
EDGE_SAMPLE_RATE = 22050

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


# --- Playback ---------------------------------------------------------------
#
# Everything audible goes through one place, so that "is Jarvis talking" has
# one answer and "stop talking" has one switch. The Player owns the device;
# this module owns the intervals the capture gate reads and the lock that
# serializes two callers.

# The utterance currently playing, so barge-in (and a shutdown) has something
# to abort. Guarded by its own lock rather than _lock: the whole point is to
# reach a player *while* _lock is held by whoever is speaking.
_current: player.Player | None = None
_current_lock = threading.Lock()

# A stop() that arrived before there was anything to stop.
#
# Measured: "stop after 1.2s" against an Edge synthesis that took 1.53s did
# nothing at all, and the reply then played out in full. Synthesis is a
# second or more of silence during which speak() has been called, the user
# believes Jarvis is answering, and _current is still None -- so a stop
# landing there would be dropped on the floor. It is remembered instead and
# honoured by the next player to start within this same speak() call.
_stop_pending = False


def _pcm_to_wav(pcm: bytes, samplerate: int) -> bytes:
    """Wrap raw PCM in a WAV header, for the winsound fallback path."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(player.BYTES_PER_SAMPLE)
        wav_file.setframerate(samplerate)
        wav_file.writeframes(pcm)
    return buffer.getvalue()


def _play_pcm(pcm: bytes, samplerate: int) -> None:
    """Play one finished utterance, blocking until it ends or is aborted.

    Falls back to winsound if the output device will not open at all. That
    path is uninterruptible -- it is the old behaviour, kept because a
    machine that cannot open a PortAudio stream should still be able to
    answer out loud, and losing barge-in is cheaper than losing the reply.
    """
    global _current

    try:
        sink = player.Player(samplerate)
        sink.start()
    except player.PlaybackUnavailable as e:
        diag.log(f"[play] falling back to winsound: {e}")
        with _playing():
            winsound.PlaySound(_pcm_to_wav(pcm, samplerate), winsound.SND_MEMORY)
        return

    with _current_lock:
        _current = sink
        cancelled = _stop_pending

    if cancelled:
        # stop() arrived while this was still being synthesized.
        sink.abort()

    try:
        with _playing():
            sink.write(pcm)
            sink.done_writing()
            sink.wait()
        if sink.underruns:
            # Never silent: an underrun is an audible hole in a sentence and
            # is otherwise indistinguishable from a natural pause.
            diag.log(f"[play] {sink.underruns} underruns")
    finally:
        sink.close()
        with _current_lock:
            if _current is sink:
                _current = None


def stop() -> None:
    """Cut off whatever this utterance is saying, now or as soon as it starts.

    This is the half of barge-in that lives on the output side; deciding
    *whether* to interrupt is somebody else's job.

    Not a no-op when nothing is playing yet: see _stop_pending. The flag is
    cleared by the next speak(), so a stop that lands in the gap between two
    replies cannot silence the one after it.
    """
    global _stop_pending

    with _current_lock:
        _stop_pending = True
        sink = _current

    if sink is not None:
        sink.abort()


def _speak_piper(text: str) -> None:
    voice = _get_voice()

    t0 = time.perf_counter()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file)
    diag.log(f"[timing] Piper synthesis: {time.perf_counter() - t0:.2f}s")

    buffer.seek(0)
    with wave.open(buffer, "rb") as wav_file:
        samplerate = wav_file.getframerate()
        pcm = wav_file.readframes(wav_file.getnframes())

    _play_pcm(pcm, samplerate)


def _speak_edge(text: str) -> bool:
    """Synthesize with Microsoft Edge TTS. Returns False on any failure
    (e.g. no internet) so the caller can fall back to Piper."""
    t0 = time.perf_counter()
    mp3_path = Path(tempfile.gettempdir()) / "jarvis_tts.mp3"

    try:
        asyncio.run(edge_tts.Communicate(text, TTS_VOICE).save(str(mp3_path)))

        # Decoded straight to raw PCM on stdout rather than to a WAV file:
        # the player takes PCM, and this saves a second temp file and the
        # read back off disk.
        decoded = subprocess.run(
            [
                FFMPEG_PATH, "-y", "-loglevel", "error",
                "-i", str(mp3_path),
                "-f", "s16le", "-ar", str(EDGE_SAMPLE_RATE), "-ac", "1",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
        )
        pcm = decoded.stdout
    except Exception as e:
        print(f"Σφάλμα Edge TTS: {e}")
        return False
    finally:
        mp3_path.unlink(missing_ok=True)

    diag.log(f"[timing] Edge TTS synthesis: {time.perf_counter() - t0:.2f}s")
    _play_pcm(pcm, EDGE_SAMPLE_RATE)
    return True


# Add "elevenlabs" here later (not implemented yet). Piper is the
# always-available fallback/default, so it deliberately isn't listed here.
_VOICE_PROVIDERS = {
    "edge": _speak_edge,
}

# Serializes playback: speak() can be called from the main loop and from the
# scheduler's background thread (see jarvis/scheduler.py), and one output
# device cannot sensibly carry two replies at once. This makes a concurrent
# call simply wait its turn.
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
_intervals: "collections.deque[tuple[float, float]]" = collections.deque(maxlen=8)
_intervals_lock = threading.Lock()

# Start of the interval currently open, while a sound is actually playing.
_playback_start: float | None = None


@contextlib.contextmanager
def _playing():
    """Mark the wall-clock interval during which a sound is actually audible.

    Wrapped around playback only, not around synthesis: Edge TTS spends a
    second on the network making no sound at all, and muting the microphone
    through it would throw away audio the user really did speak.

    The interval must close the instant playback stops, aborted or not. Every
    millisecond it over-reports is a millisecond the capture gate mutes -- and
    after a barge-in those are exactly the milliseconds carrying the words
    that caused it.
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
#
# Still winsound.Beep: it drives the system beep rather than an output
# stream, it is already short enough that cancelling it means nothing, and
# keeping it off the PortAudio path means a device that won't open costs the
# reply's quality, never the cue that tells the user to speak.
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
    global _stop_pending

    with _lock:
        # A stop() aimed at the *previous* reply must not silence this one.
        # Cleared under _lock, so it can only ever cancel a stop that arrived
        # before this utterance was asked for.
        with _current_lock:
            _stop_pending = False

        provider = _VOICE_PROVIDERS.get(TTS_ENGINE)
        if provider is not None:
            if provider(text):
                return
            print("Edge TTS απέτυχε (πιθανώς χωρίς σύνδεση), χρήση Piper.")

        _speak_piper(text)
