"""Cancellable PCM playback.

Split out of speaker.py because the two answer different questions: speaker
turns text into audio, this turns audio into sound and can be told to stop
halfway. Nothing here knows what a sentence is.

**Why this module exists at all.** speaker.py played with
`winsound.PlaySound(wav_bytes, SND_MEMORY)`, which blocks until the sound
ends and cannot be cancelled -- Python refuses the one flag combination that
would help:

    winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_ASYNC)
    RuntimeError: Cannot play asynchronously from memory

So barge-in is not a feature that can be bolted onto the old speaker: a reply
already handed to winsound is going to finish. It also had no way to answer
"how much of that did we actually say", which is what the conversation
history has to be committed from once a reply can be cut in half.

One stream per utterance, fed from a deque by PortAudio's callback thread:

  * **Gapless across chunks.** A streamed reply arrives as several separately
    synthesized pieces. Playing each with its own PlaySound call would put a
    device open/close between every sentence; here they are appended to the
    same running stream and the seam is a buffer hand-off.
  * **Stops within one blocksize** (~23ms at the default 512 frames), which
    is what makes talking over Jarvis feel like interrupting a person rather
    than waiting for a machine to notice.
  * **Underruns are counted, never silent.** If synthesis falls behind
    playback the callback has nothing to hand the device and plays silence --
    audible as a gap mid-sentence, and otherwise completely invisible. Same
    discipline as listener._frames_evicted, which exists for the same reason.

The `sounddevice` import is deferred into start(), not taken at module
scope: it loads the PortAudio DLL, and jarvis/skills.py imports speaker.py
(and so this) transitively, including from the test suite. Same lazy idiom as
listener._get_model and speaker._get_voice.
"""

from __future__ import annotations

import collections
import threading

from jarvis import diag
from jarvis.config import PLAYBACK_BLOCKSIZE, PLAYBACK_DEVICE

# 16-bit signed mono is the only format anything upstream produces: Piper
# writes it, and ffmpeg is told to decode Edge's mp3 to it.
BYTES_PER_SAMPLE = 2


class PlaybackUnavailable(RuntimeError):
    """The output device could not be opened.

    Distinct from every other failure here so speaker.py can fall back to
    winsound rather than lose the reply entirely: a machine that cannot open
    a PortAudio stream can usually still make a sound the old way.
    """


def _resolve_device(spec: str):
    """Turn PLAYBACK_DEVICE into something sounddevice accepts.

    Empty means "PortAudio's own default", which is deliberately not the same
    thing as the Windows default winsound used: measured on this machine,
    PortAudio picks device 3 (MME, the monitor's HDMI audio) while winsound
    follows the system setting. That difference is exactly why this is
    configurable and why the first run of the new player has to be checked by
    ear -- a reply coming out of the wrong speaker is silence as far as
    anyone sitting at the desk is concerned.

    A bare number is an index; anything else is matched as a case-insensitive
    substring of a device name, so .env can say "Realtek" and survive the
    indexes being renumbered when a USB device appears.
    """
    spec = (spec or "").strip()
    if not spec:
        return None

    try:
        return int(spec)
    except ValueError:
        pass

    import sounddevice as sd

    wanted = spec.lower()
    for index, device in enumerate(sd.query_devices()):
        if device["max_output_channels"] > 0 and wanted in device["name"].lower():
            return index

    raise PlaybackUnavailable(f"No output device matching PLAYBACK_DEVICE={spec!r}.")


class Player:
    """One output stream for one utterance.

    Lifecycle: start() -> write() any number of times -> done_writing() ->
    wait(). abort() may be called at any point from another thread and is
    what barge-in uses; after it, played_seconds says how much was handed to
    the device before it stopped.
    """

    def __init__(self, samplerate: int, device=None, blocksize: int | None = None):
        self.samplerate = samplerate
        self._device = _resolve_device(PLAYBACK_DEVICE) if device is None else device
        self._blocksize = PLAYBACK_BLOCKSIZE if blocksize is None else blocksize

        self._buffer: "collections.deque[bytes]" = collections.deque()
        self._offset = 0  # bytes of _buffer[0] already handed over
        self._played_bytes = 0
        self._underruns = 0

        self._lock = threading.Lock()
        self._finished = threading.Event()
        self._input_closed = False
        self._aborted = False

        self._sd = None
        self._stream = None

    # --- the callback thread ---------------------------------------------

    def _callback(self, outdata, frames, time_info, status) -> None:
        """Hand PortAudio one block. Runs on its thread; must not block."""
        need = frames * BYTES_PER_SAMPLE
        out = memoryview(outdata)
        filled = 0

        with self._lock:
            while filled < need and self._buffer:
                chunk = self._buffer[0]
                take = min(need - filled, len(chunk) - self._offset)
                out[filled : filled + take] = chunk[self._offset : self._offset + take]
                filled += take
                self._offset += take
                if self._offset >= len(chunk):
                    self._buffer.popleft()
                    self._offset = 0

            self._played_bytes += filled
            short = need - filled
            drained = not self._buffer and self._input_closed

            if short:
                out[filled:need] = b"\x00" * short
                if not drained:
                    # Synthesis fell behind playback: this block is a hole in
                    # the middle of a sentence. Counted, because it is
                    # otherwise indistinguishable from a natural pause.
                    self._underruns += 1

        if drained:
            raise self._sd.CallbackStop

    def _on_finished(self) -> None:
        self._finished.set()

    # --- the controlling thread ------------------------------------------

    def start(self) -> None:
        try:
            import sounddevice as sd
        except Exception as e:  # missing DLL, broken install
            raise PlaybackUnavailable(f"sounddevice unavailable: {e}") from e

        self._sd = sd
        try:
            self._stream = sd.RawOutputStream(
                samplerate=self.samplerate,
                blocksize=self._blocksize,
                device=self._device,
                channels=1,
                dtype="int16",
                callback=self._callback,
                finished_callback=self._on_finished,
            )
            self._stream.start()
        except Exception as e:
            raise PlaybackUnavailable(f"could not open the output device: {e}") from e

    def write(self, pcm: bytes) -> None:
        """Queue more audio. Never blocks; ignored once aborted or closed."""
        if not pcm:
            return
        with self._lock:
            if self._aborted or self._input_closed:
                return
            self._buffer.append(pcm)

    def done_writing(self) -> None:
        """No more audio is coming, so draining the buffer ends the stream."""
        with self._lock:
            self._input_closed = True

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the buffer drains or abort() lands. True if finished."""
        return self._finished.wait(timeout)

    def abort(self) -> None:
        """Stop now, discarding whatever is still queued.

        Safe from any thread but the callback's, and idempotent: barge-in and
        a normal end-of-reply can race, and the loser must not raise.
        """
        with self._lock:
            if self._aborted:
                return
            self._aborted = True
            self._buffer.clear()
            self._offset = 0

        if self._stream is not None:
            try:
                self._stream.abort()
            except Exception as e:
                diag.log(f"[play] abort failed: {e}")
        self._finished.set()

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception as e:
                diag.log(f"[play] close failed: {e}")
            self._stream = None
        self._finished.set()

    # --- what happened ----------------------------------------------------

    @property
    def played_seconds(self) -> float:
        """Audio handed to the device, in seconds.

        Deliberately "handed to", not "heard": PortAudio is up to one
        blocksize plus the device's own latency ahead of the speaker cone, so
        this overstates by ~20-50ms. That is the right direction to be wrong
        in -- the caller uses it to decide how much of a reply to commit to
        the conversation history, and crediting Jarvis with a few extra
        milliseconds he almost said beats dropping a word he did.
        """
        with self._lock:
            return self._played_bytes / (self.samplerate * BYTES_PER_SAMPLE)

    @property
    def underruns(self) -> int:
        with self._lock:
            return self._underruns

    @property
    def aborted(self) -> bool:
        with self._lock:
            return self._aborted
