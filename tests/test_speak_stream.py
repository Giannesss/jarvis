"""Tests for speaker.speak_stream: one reply, several chunks, one sound.

Two things are being pinned, and only one of them is about audio.

  * **The seam.** A streamed reply is synthesized in pieces, and every piece
    must go into the *same* player: a device open and close between two
    sentences is audible, and the player was split out of speaker.py precisely
    so that it would not be.
  * **The accounting.** A reply can be cut off mid-word, so what the model
    generated and what the user heard are different strings -- and the second
    is what brain.Turn commits to history. Getting it wrong is not a cosmetic
    bug: too little and the next reply repeats a sentence the user already
    heard, too much and it refers back to one they never got.

Nothing here opens a device. jarvis.player has its own file
(tests/test_player.py), so the Player is faked wholesale; what is under test is
the orchestration around it.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from jarvis import speaker


RATE = 22050
SECOND = RATE * 2  # bytes of 16-bit mono per second

# Captured before any test patches it: FallbackTests is about the real
# Edge-then-Piper dispatch inside it, which every other test here replaces.
_real_synthesize = speaker._synthesize


class _FakePlayer:
    """Stands in for jarvis.player.Player.

    `cap_seconds` is how much of what it was handed actually reached the
    device, which is the only thing the accounting reads: an interrupted reply
    is one whose played_seconds stops short of what was written.
    """

    def __init__(self, samplerate, device=None, blocksize=None):
        self.samplerate = samplerate
        self.written = b""
        self.aborted = False
        self.closed = False
        self.underruns = 0
        self.cap_seconds = float("inf")
        self.on_write = None

    def start(self) -> None:
        pass

    def write(self, pcm: bytes) -> None:
        if self.aborted:
            return
        self.written += pcm
        if self.on_write is not None:
            self.on_write(self)

    def done_writing(self) -> None:
        pass

    def wait(self, timeout=None) -> bool:
        return True

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        self.closed = True

    @property
    def played_seconds(self) -> float:
        return min(len(self.written) / SECOND, self.cap_seconds)


class SpeakStreamTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.players: list[_FakePlayer] = []
        self.on_write = None

        def make_player(samplerate, device=None, blocksize=None):
            sink = _FakePlayer(samplerate)
            sink.on_write = self.on_write
            self.players.append(sink)
            return sink

        patcher = mock.patch.object(speaker.player, "Player", make_player)
        patcher.start()
        self.addCleanup(patcher.stop)

        # One second of recognisable audio per chunk, so order is checkable and
        # every chunk is exactly 1.0s long.
        self.tones: dict[str, bytes] = {}
        self.rates: dict[str, int] = {}
        self.synthesized: list[str] = []

        def synth(text: str) -> tuple[bytes, int]:
            self.synthesized.append(text)
            if text in self.tones:
                return self.tones[text], self.rates.get(text, RATE)
            return bytes([len(self.synthesized), 0]) * RATE, RATE

        synth_patcher = mock.patch.object(speaker, "_synthesize", synth)
        synth_patcher.start()
        self.addCleanup(synth_patcher.stop)

        winsound_patcher = mock.patch.object(speaker, "winsound")
        self.winsound = winsound_patcher.start()
        self.addCleanup(winsound_patcher.stop)

        speaker._stop_pending = False
        speaker._intervals.clear()
        self.addCleanup(speaker._intervals.clear)
        self.addCleanup(setattr, speaker, "_stop_pending", False)

    def run_stream(self, chunks) -> speaker.SpeechResult:
        """speak_stream with its printing swallowed."""
        with contextlib.redirect_stdout(io.StringIO()):
            return speaker.speak_stream(chunks)


class ContinuityTests(SpeakStreamTestCase):
    def test_every_chunk_goes_into_one_player_in_order(self) -> None:
        result = self.run_stream(["ένα", "δύο", "τρία"])

        self.assertEqual(len(self.players), 1)
        sink = self.players[0]
        self.assertEqual(
            sink.written,
            bytes([1, 0]) * RATE + bytes([2, 0]) * RATE + bytes([3, 0]) * RATE,
        )
        self.assertEqual(result.spoken, "ένα δύο τρία")
        self.assertFalse(result.aborted)
        self.assertTrue(sink.closed)

    def test_chunks_are_pulled_lazily(self) -> None:
        # The generator is a token stream: pulling it eagerly would wait for
        # the whole reply and throw the feature away.
        seen: list[str] = []

        def chunks():
            for text in ("ένα", "δύο"):
                seen.append(text)
                yield text

        self.on_write = lambda sink: self.assertLessEqual(len(seen), 2)
        self.run_stream(chunks())
        self.assertEqual(seen, ["ένα", "δύο"])

    def test_a_chunk_that_cannot_be_synthesized_does_not_lose_the_rest(self) -> None:
        self.tones["δύο"] = b""
        self.rates["δύο"] = 0

        result = self.run_stream(["ένα", "δύο", "τρία"])

        self.assertEqual(len(self.players), 1)
        self.assertEqual(result.spoken, "ένα τρία")

    def test_a_samplerate_change_mid_reply_costs_exactly_one_seam(self) -> None:
        # Edge failing halfway and Piper taking over at another rate: a running
        # PortAudio stream cannot change samplerate, so this is the one case in
        # which a reply legitimately has a join in it.
        self.tones["δύο"] = b"\x02\x00" * 16000
        self.rates["δύο"] = 16000

        result = self.run_stream(["ένα", "δύο"])

        self.assertEqual([sink.samplerate for sink in self.players], [RATE, 16000])
        self.assertTrue(all(sink.closed for sink in self.players))
        self.assertEqual(result.spoken, "ένα δύο")


class AccountingTests(SpeakStreamTestCase):
    """What the user heard, which is what goes into the history."""

    def cut_after(self, seconds: float) -> None:
        """Abort the reply once `seconds` of it have been handed over."""

        def hook(sink: _FakePlayer) -> None:
            if len(sink.written) / SECOND >= seconds:
                sink.cap_seconds = seconds
                speaker.stop()

        self.on_write = hook

    def test_an_interrupted_reply_commits_only_the_chunks_that_were_heard(self) -> None:
        self.cut_after(1.0)
        result = self.run_stream(["ένα", "δύο", "τρία"])

        self.assertTrue(result.aborted)
        self.assertEqual(result.spoken, "ένα")

    def test_a_chunk_more_than_half_played_still_counts(self) -> None:
        # 1.6s of three 1.0s chunks: the second was 60% said, and the user
        # heard it. Dropping it would make the next reply repeat it.
        self.cut_after(1.6)
        result = self.run_stream(["ένα", "δύο", "τρία"])

        self.assertEqual(result.spoken, "ένα δύο")

    def test_a_chunk_barely_started_does_not_count(self) -> None:
        self.cut_after(1.2)
        result = self.run_stream(["ένα", "δύο", "τρία"])

        self.assertEqual(result.spoken, "ένα")

    def test_an_interruption_before_the_first_word_commits_nothing(self) -> None:
        # brain.Turn treats an empty string as "this turn never happened", so
        # it must really be empty rather than the first chunk optimistically.
        self.cut_after(0.0)
        result = self.run_stream(["ένα", "δύο"])

        self.assertTrue(result.aborted)
        self.assertEqual(result.spoken, "")

    def test_an_abort_stops_synthesizing_the_rest(self) -> None:
        # Synthesis is a network round trip per chunk and the generator behind
        # it is still generating tokens. Both have to stop, or a barged reply
        # costs as much as one nobody interrupted.
        self.cut_after(1.0)
        self.run_stream(["ένα", "δύο", "τρία", "τέσσερα"])

        self.assertLess(len(self.synthesized), 4)

    def test_no_further_piece_is_pulled_after_an_abort(self) -> None:
        # Pulling is what waits on the model, so the check has to come before
        # it. It used to come after, which cost one more chunk of generation
        # after the reply was already over -- and printed a sentence nobody
        # heard, since main.py showed the reply from the generator.
        pulled: list[str] = []

        def chunks():
            for text in ("ένα", "δύο", "τρία"):
                pulled.append(text)
                yield text

        self.cut_after(1.0)
        self.run_stream(chunks())

        self.assertEqual(pulled, ["ένα"])

    def test_on_chunk_reports_only_what_reached_the_device(self) -> None:
        # main.py prints from this, so it must not announce a chunk that the
        # interruption means was never handed over.
        shown: list[str] = []
        self.cut_after(1.0)

        with contextlib.redirect_stdout(io.StringIO()):
            speaker.speak_stream(["ένα", "δύο", "τρία"], on_chunk=shown.append)

        self.assertEqual(shown, ["ένα"])


class FallbackTests(SpeakStreamTestCase):
    def test_no_output_device_still_says_the_whole_reply(self) -> None:
        # Uninterruptible and with a join between chunks, but losing barge-in
        # is cheaper than losing the reply.
        with mock.patch.object(speaker, "_open_sink", return_value=None):
            result = self.run_stream(["ένα", "δύο"])

        self.assertEqual(self.winsound.PlaySound.call_count, 2)
        self.assertEqual(result.spoken, "ένα δύο")
        self.assertFalse(result.aborted)

    def test_edge_failing_falls_back_to_piper_for_that_chunk(self) -> None:
        # _synthesize's own dispatch, which every other test here patches out.
        # Same Edge-then-Piper contract speak() has, since it is the same
        # failure -- only the audio comes back instead of a sound.
        with mock.patch.dict(speaker._SYNTH_PROVIDERS, {"edge": lambda text: None}), \
                mock.patch.object(speaker, "TTS_ENGINE", "edge"), \
                mock.patch.object(
                    speaker, "_synth_piper", return_value=(b"\x01\x00" * RATE, RATE)
                ) as piper:
            with contextlib.redirect_stdout(io.StringIO()):
                pcm, rate = _real_synthesize("γεια")

        self.assertEqual(rate, RATE)
        self.assertEqual(len(pcm), RATE * 2)
        piper.assert_called_once_with("γεια")

    def test_a_synthesis_that_raises_costs_only_that_chunk(self) -> None:
        with mock.patch.object(speaker, "TTS_ENGINE", "piper"), \
                mock.patch.object(
                    speaker, "_synth_piper", side_effect=RuntimeError("no voice")
                ):
            with contextlib.redirect_stdout(io.StringIO()):
                pcm, rate = _real_synthesize("γεια")

        self.assertEqual(pcm, b"")
        self.assertEqual(rate, 0)


class LockAndIntervalTests(SpeakStreamTestCase):
    def test_the_lock_is_held_for_the_whole_reply(self) -> None:
        # is_speaking() is _lock.locked(), and the scheduler's announcements
        # wait on it: two replies cannot share one output device.
        observed: list[bool] = []

        def chunks():
            observed.append(speaker.is_speaking())
            yield "ένα"

        self.run_stream(chunks())

        self.assertEqual(observed, [True])
        self.assertFalse(speaker.is_speaking())

    def test_the_audible_interval_closes_even_on_an_abort(self) -> None:
        # A leaked open interval would mute the microphone for the rest of the
        # run: was_speaking() answers True for anything at or after its start.
        self.on_write = lambda sink: speaker.stop()
        self.run_stream(["ένα", "δύο"])

        self.assertIsNone(speaker._playback_start)
        self.assertEqual(len(speaker._intervals), 1)

    def test_a_stale_stop_does_not_silence_the_next_reply(self) -> None:
        speaker.stop()  # aimed at a reply that has already ended
        result = self.run_stream(["ένα"])

        self.assertFalse(result.aborted)
        self.assertFalse(self.players[0].aborted)

    def test_a_stop_during_synthesis_is_honoured_by_the_first_player(self) -> None:
        # The measured hole (see speaker._stop_pending): Edge took 1.53s, the
        # stop landed at 1.20s, and nothing was playing yet to receive it.
        def chunks():
            speaker.stop()
            yield "ένα"

        result = self.run_stream(chunks())

        self.assertTrue(self.players[0].aborted)
        self.assertTrue(result.aborted)
        self.assertEqual(result.spoken, "")


if __name__ == "__main__":
    unittest.main()
