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
from typing import Callable, Iterable, NamedTuple

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


def _open_sink(samplerate: int) -> player.Player | None:
    """Open the device for one utterance, honouring a stop that already landed.

    None means the device would not open and the caller must fall back to
    winsound -- uninterruptible, but losing barge-in is cheaper than losing the
    reply.
    """
    global _current

    try:
        sink = player.Player(samplerate)
        sink.start()
    except player.PlaybackUnavailable as e:
        diag.log(f"[play] falling back to winsound: {e}")
        return None

    with _current_lock:
        _current = sink
        cancelled = _stop_pending

    if cancelled:
        # stop() arrived while this was still being synthesized.
        sink.abort()

    return sink


def _release_sink(sink: player.Player) -> None:
    global _current

    if sink.underruns:
        # Never silent: an underrun is an audible hole in a sentence and is
        # otherwise indistinguishable from a natural pause.
        diag.log(f"[play] {sink.underruns} underruns")

    sink.close()
    with _current_lock:
        if _current is sink:
            _current = None


def _winsound_play(pcm: bytes, samplerate: int) -> None:
    with _playing():
        winsound.PlaySound(_pcm_to_wav(pcm, samplerate), winsound.SND_MEMORY)


def _play_pcm(pcm: bytes, samplerate: int) -> None:
    """Play one finished utterance, blocking until it ends or is aborted."""
    sink = _open_sink(samplerate)
    if sink is None:
        _winsound_play(pcm, samplerate)
        return

    try:
        with _playing():
            sink.write(pcm)
            sink.done_writing()
            sink.wait()
    finally:
        _release_sink(sink)


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


def _synth_piper(text: str) -> tuple[bytes, int]:
    """Text -> (pcm, samplerate). Piper's rate is read off the WAV it writes,
    since a different voice can use a different one."""
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

    return pcm, samplerate


def _speak_piper(text: str) -> None:
    pcm, samplerate = _synth_piper(text)
    _play_pcm(pcm, samplerate)


def _synth_edge(text: str) -> bytes | None:
    """Synthesize with Microsoft Edge TTS, at EDGE_SAMPLE_RATE. None on any
    failure (e.g. no internet) so the caller can fall back to Piper."""
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
        return None
    finally:
        mp3_path.unlink(missing_ok=True)

    diag.log(f"[timing] Edge TTS synthesis: {time.perf_counter() - t0:.2f}s")
    return pcm


def _speak_edge(text: str) -> bool:
    """Returns False on any failure, so speak() can fall back to Piper."""
    pcm = _synth_edge(text)
    if pcm is None:
        return False

    _play_pcm(pcm, EDGE_SAMPLE_RATE)
    return True


# Add "elevenlabs" here later (not implemented yet). Piper is the
# always-available fallback/default, so it deliberately isn't listed here.
_VOICE_PROVIDERS = {
    "edge": _speak_edge,
}

# The same dispatch for the streaming path, which needs the audio rather than
# the sound: a chunk is synthesized here and written into a player that is
# already running, so synthesis and playback cannot be one call. Kept as a
# second dict rather than folded into the first because the two contracts
# differ -- these return PCM and never touch the device.
_SYNTH_PROVIDERS = {
    "edge": _synth_edge,
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


def _audible_start() -> None:
    global _playback_start

    with _intervals_lock:
        _playback_start = time.perf_counter()


def _audible_end() -> None:
    global _playback_start

    with _intervals_lock:
        if _playback_start is not None:
            _intervals.append((_playback_start, time.perf_counter()))
            _playback_start = None


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

    A streamed reply cannot use the `with` form -- its playback starts inside a
    loop and ends in a different iteration -- so the two halves are callable
    separately. _audible_end() is idempotent for that reason.
    """
    _audible_start()
    try:
        yield
    finally:
        _audible_end()


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


# --- Streaming -------------------------------------------------------------


class SpeechResult(NamedTuple):
    """What was actually heard, and whether something cut it off.

    `spoken` is what belongs in the conversation history: a reply interrupted
    halfway was, as far as the user is concerned, only its first half.
    """

    spoken: str
    aborted: bool


def _synthesize(text: str) -> tuple[bytes, int]:
    """One chunk -> (pcm, samplerate). Empty pcm means it could not be said.

    Same Edge-then-Piper fallback speak() has, and the same message, because it
    is the same failure -- only the audio comes back instead of a sound.
    """
    provider = _SYNTH_PROVIDERS.get(TTS_ENGINE)
    if provider is not None:
        pcm = provider(text)
        if pcm:
            return pcm, EDGE_SAMPLE_RATE
        print("Edge TTS απέτυχε (πιθανώς χωρίς σύνδεση), χρήση Piper.")

    try:
        return _synth_piper(text)
    except Exception as e:
        # A chunk that cannot be synthesized at all costs that chunk, never
        # the rest of the reply.
        print(f"Σφάλμα σύνθεσης φωνής: {e}")
        return b"", 0


def _heard(parts: list[tuple[str, float, float]], played: float) -> str:
    """Which chunks the user actually heard, given how much audio played.

    A chunk counts as spoken once half of it has played. The halves are not
    symmetric: `played` is what reached the device rather than the speaker cone
    (see player.played_seconds), so it already leans towards crediting a few
    milliseconds Jarvis nearly said -- and the string this returns is what the
    model is told it said. Dropping a sentence the user heard in full makes the
    next reply repeat it; keeping one they barely heard the start of makes it
    reference something they never got.
    """
    return " ".join(
        text for text, start, end in parts if played >= start + (end - start) / 2
    ).strip()


def speak_stream(
    chunks: Iterable[str], on_chunk: Callable[[str], None] | None = None
) -> SpeechResult:
    """Speak a reply as it arrives, one chunk at a time, abortably.

    `chunks` is pulled lazily and is usually a generator over a model's token
    stream (see jarvis/chunker.py). It stops being pulled the moment a
    barge-in lands, which is also what closes that generator and cancels the
    rest of the generation.

    `on_chunk` is called with each piece just before it is handed to the
    device, and is how a caller shows the reply at the pace it is spoken. It
    belongs here rather than in the generator for one reason: the generator runs
    ahead of playback by a chunk or two, so printing from it shows text that an
    interruption a moment later means nobody ever hears.

    One player for the whole reply, so the seam between two chunks is a buffer
    hand-off rather than a device open and close. The one thing that can force
    a second player is the samplerate changing under it -- Edge failing
    mid-reply and Piper taking over at a different rate -- and that is logged,
    because it is the only case in which a reply has an audible join in it.
    """
    global _stop_pending

    with _lock:
        with _current_lock:
            _stop_pending = False
        return _stream_chunks(chunks, on_chunk)


def _stream_chunks(
    chunks: Iterable[str], on_chunk: Callable[[str], None] | None = None
) -> SpeechResult:
    parts: list[tuple[str, float, float]] = []  # (text, start, end) in seconds
    sink: player.Player | None = None
    samplerate: int | None = None
    written = 0.0  # audio handed over so far, across every player
    played = 0.0  # audio that reached a device, for players already released
    aborted = False
    spoken_anyway = ""  # played through winsound, so heard in full or not at all

    def release(current: player.Player) -> float:
        """Let the queued audio finish, close the device, and report what of it
        actually reached the device."""
        current.done_writing()
        current.wait()
        _audible_end()
        heard = current.played_seconds
        _release_sink(current)
        return heard

    pieces = iter(chunks)
    try:
        while True:
            # Checked before the next piece is pulled, not after: pulling is
            # what waits on the model, so a stop that lands during it would
            # otherwise cost one more chunk of generation and one more
            # synthesis after the reply was already over.
            if sink is not None and sink.aborted:
                aborted = True
                break

            try:
                text = next(pieces)
            except StopIteration:
                break

            if sink is not None and sink.aborted:
                aborted = True
                break

            pcm, rate = _synthesize(text)
            if not pcm:
                continue

            if sink is not None and rate != samplerate:
                # A running PortAudio stream cannot change samplerate, so the
                # engine changing mid-reply costs exactly one join.
                diag.log(
                    f"[play] samplerate changed mid-reply "
                    f"({samplerate} -> {rate}); one seam"
                )
                played += release(sink)
                if sink.aborted:
                    aborted = True
                    sink = None
                    break
                sink = None

            if sink is None:
                samplerate = rate
                sink = _open_sink(rate)
                if sink is None:
                    # No output device at all. winsound cannot be interrupted
                    # and cannot be appended to, so each chunk is its own
                    # sound: seams, no barge-in, but the reply survives.
                    if on_chunk is not None:
                        on_chunk(text)
                    _winsound_play(pcm, rate)
                    spoken_anyway = f"{spoken_anyway} {text}".strip()
                    continue
                _audible_start()

            chunk_seconds = len(pcm) / (rate * player.BYTES_PER_SAMPLE)
            parts.append((text, written, written + chunk_seconds))
            written += chunk_seconds
            if on_chunk is not None:
                on_chunk(text)
            sink.write(pcm)
    finally:
        if sink is not None:
            played += release(sink)
            aborted = aborted or sink.aborted
        else:
            _audible_end()

    if not parts:
        # Nothing reached a player: every chunk went out through winsound, or
        # there was nothing to say at all.
        return SpeechResult(spoken_anyway, False)

    spoken = _heard(parts, played)
    if spoken_anyway:
        spoken = f"{spoken_anyway} {spoken}".strip()

    diag.log(
        f"[play] streamed {len(parts)} chunks, {written:.2f}s audio, "
        f"{played:.2f}s played" + (" (interrupted)" if aborted else "")
    )
    return SpeechResult(spoken, aborted)
