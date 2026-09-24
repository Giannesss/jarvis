"""Tests for jarvis/speaker.py's lazy Piper load.

The Piper voice used to be loaded at module scope, so importing jarvis.speaker
-- which jarvis/skills.py does transitively -- read a ~63MB ONNX off disk even
when TTS_ENGINE=edge and Piper was never used. It is now a lazy singleton
(_get_voice), and these tests pin both halves of that: that nothing loads at
import, and that the Edge-then-Piper fallback behaves exactly as it did before.

Nothing here touches real audio, the network, or the real model. `piper` is
replaced with a fake module in sys.modules so the load is observable and free,
winsound and _play_pcm are patched so nothing reaches an output device, and
edge_tts is never reached because _speak_edge itself is patched.

_play_pcm is patched rather than jarvis.player: what these tests are about is
which *provider* speaker.py routes to, and the player has its own file
(tests/test_player.py). Without the patch they would open a real PortAudio
stream and play a few frames of silence on every run.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from unittest import mock


def _fake_synthesize(text: str, wav_file) -> None:
    """Stand-in for PiperVoice.synthesize_wav: writes a valid (silent) WAV so
    _speak_piper's `with wave.open(...)` block closes cleanly."""
    wav_file.setnchannels(1)
    wav_file.setsampwidth(2)
    wav_file.setframerate(22050)
    wav_file.writeframes(b"\x00\x00" * 8)


class SpeakerLazyTestCase(unittest.TestCase):
    """Reloads jarvis.speaker against a fake `piper` module for each test, so
    every test sees a module whose _voice has never been populated."""

    @staticmethod
    def _restore_module(name: str, original) -> None:
        """Put sys.modules[name] back exactly as it was -- absent if it was."""
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original

    def setUp(self) -> None:
        self.fake_voice = mock.Mock(name="PiperVoice instance")
        # A real PiperVoice fills in the wave header; without this the
        # `with wave.open(...)` block in _speak_piper raises on close.
        self.fake_voice.synthesize_wav.side_effect = _fake_synthesize

        self.piper_voice_cls = mock.Mock(name="PiperVoice")
        self.piper_voice_cls.load.return_value = self.fake_voice

        fake_piper = mock.Mock(name="piper module")
        fake_piper.PiperVoice = self.piper_voice_cls

        # Injected by key, deliberately not with mock.patch.dict(sys.modules,
        # ...). patch.dict restores by clearing the dict and repopulating it
        # from a snapshot taken at start(), which evicts every module imported
        # *during* the test -- here numpy, pulled in below by jarvis.speaker ->
        # jarvis.listener -> faster_whisper. The next setUp re-imported it, and
        # numpy refuses to load its C extension twice in one process, so
        # running this file on its own failed every test after the first.
        # Under `discover` it passed by luck: an earlier file had already
        # imported numpy, so the snapshot kept it.
        original_piper = sys.modules.get("piper")
        sys.modules["piper"] = fake_piper
        self.addCleanup(self._restore_module, "piper", original_piper)

        import jarvis.speaker

        self.speaker = importlib.reload(jarvis.speaker)

        # Never play anything, in any test in this file.
        winsound_patcher = mock.patch.object(self.speaker, "winsound")
        self.winsound = winsound_patcher.start()
        self.addCleanup(winsound_patcher.stop)

        self.play_patcher = mock.patch.object(self.speaker, "_play_pcm")
        self.play = self.play_patcher.start()
        self.addCleanup(self.play_patcher.stop)

    def tearDown(self) -> None:
        # Leave the real module in place for whatever runs next, since
        # reload() rebound the module object the rest of the suite imports.
        import jarvis.speaker

        importlib.reload(jarvis.speaker)


class ImportTimeTests(SpeakerLazyTestCase):
    def test_importing_speaker_does_not_load_the_voice(self) -> None:
        # The whole point of the change: skills.py imports speaker.py, and the
        # test suite imports skills.py.
        self.assertIsNone(self.speaker._voice)
        self.piper_voice_cls.load.assert_not_called()

    def test_get_voice_loads_once_and_caches(self) -> None:
        first = self.speaker._get_voice()
        second = self.speaker._get_voice()

        self.assertIs(first, self.fake_voice)
        self.assertIs(second, self.fake_voice)
        self.piper_voice_cls.load.assert_called_once()


class EdgePathTests(SpeakerLazyTestCase):
    def test_successful_edge_never_loads_piper(self) -> None:
        with mock.patch.object(self.speaker, "_speak_edge", return_value=True) as edge:
            self.speaker.TTS_ENGINE = "edge"
            self.speaker._VOICE_PROVIDERS["edge"] = edge
            self.speaker.speak("γεια")

        edge.assert_called_once_with("γεια")
        self.assertIsNone(self.speaker._voice)
        self.piper_voice_cls.load.assert_not_called()

    def test_edge_failure_falls_back_to_piper(self) -> None:
        with mock.patch.object(self.speaker, "_speak_edge", return_value=False) as edge:
            self.speaker.TTS_ENGINE = "edge"
            self.speaker._VOICE_PROVIDERS["edge"] = edge
            self.speaker.speak("γεια")

        edge.assert_called_once_with("γεια")
        self.piper_voice_cls.load.assert_called_once()
        self.fake_voice.synthesize_wav.assert_called_once()

        # Piper's PCM now goes to the player, not to winsound -- and at the
        # rate Piper itself wrote, read back off the WAV header rather than
        # assumed, since a different voice can use a different one.
        self.play.assert_called_once()
        pcm, samplerate = self.play.call_args.args
        self.assertEqual(samplerate, 22050)
        self.assertEqual(pcm, b"\x00\x00" * 8)

    def test_repeated_edge_failures_load_the_voice_only_once(self) -> None:
        with mock.patch.object(self.speaker, "_speak_edge", return_value=False) as edge:
            self.speaker.TTS_ENGINE = "edge"
            self.speaker._VOICE_PROVIDERS["edge"] = edge
            self.speaker.speak("πρώτη")
            self.speaker.speak("δεύτερη")

        self.assertEqual(edge.call_count, 2)
        self.piper_voice_cls.load.assert_called_once()
        self.assertEqual(self.fake_voice.synthesize_wav.call_count, 2)


class PiperEngineTests(SpeakerLazyTestCase):
    def test_unlisted_engine_goes_straight_to_piper(self) -> None:
        # "piper" is deliberately absent from _VOICE_PROVIDERS -- it is the
        # always-available fallback, reached via .get() returning None.
        with mock.patch.object(self.speaker, "_speak_edge") as edge:
            self.speaker.TTS_ENGINE = "piper"
            self.speaker.speak("γεια")

        edge.assert_not_called()
        self.piper_voice_cls.load.assert_called_once()
        self.fake_voice.synthesize_wav.assert_called_once()


class LockAndCueTests(SpeakerLazyTestCase):
    def test_speak_holds_the_lock_for_the_whole_call(self) -> None:
        # is_speaking() is _lock.locked(), so the lock must be held across
        # synthesis, not just playback: it serializes two callers (main loop
        # and a skill timer), and listener._should_capture falls back to it
        # before the stream has an origin estimate. The gate's real question
        # is was_speaking() -- see test_record_timing.py.
        observed = []

        def record_then_succeed(text: str) -> bool:
            observed.append(self.speaker.is_speaking())
            return True

        self.speaker.TTS_ENGINE = "edge"
        self.speaker._VOICE_PROVIDERS["edge"] = record_then_succeed
        self.speaker.speak("γεια")

        self.assertEqual(observed, [True])
        self.assertFalse(self.speaker.is_speaking())

    def test_beeps_still_sound_and_release_the_lock(self) -> None:
        self.speaker.beep_ready()
        self.speaker.beep_done()

        self.assertEqual(
            [call.args for call in self.winsound.Beep.call_args_list],
            [self.speaker.READY_BEEP, self.speaker.DONE_BEEP],
        )
        self.assertFalse(self.speaker.is_speaking())

    def test_beep_survives_a_missing_audio_device(self) -> None:
        # A missing cue must never take down the conversation loop.
        self.winsound.Beep.side_effect = RuntimeError("no audio device")
        self.speaker.beep_ready()
        self.assertFalse(self.speaker.is_speaking())


class _FakePlayer:
    """Stands in for jarvis.player.Player, which has its own test file."""

    def __init__(self, samplerate, device=None, blocksize=None):
        self.samplerate = samplerate
        self.written = b""
        self.aborted = False
        self.closed = False
        self.underruns = 0
        # Called from inside wait(), which is where a barge-in lands: the
        # only moment at which the player is registered as _current and the
        # speaking thread is blocked.
        self.on_wait = None

    def start(self) -> None:
        pass

    def write(self, pcm: bytes) -> None:
        self.written += pcm

    def done_writing(self) -> None:
        pass

    def wait(self, timeout=None) -> bool:
        if self.on_wait is not None:
            hook, self.on_wait = self.on_wait, None
            hook()
        return True

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        self.closed = True


class StopTests(SpeakerLazyTestCase):
    """speaker.stop() -- the output half of barge-in.

    The case that matters is the one measured live: Edge synthesis took
    1.53s, a stop arrived at 1.20s, and the reply then played out in full
    because nothing was playing yet for stop() to reach. A stop dropped on
    the floor is worse than no stop at all -- the user hears the assistant
    ignore them.
    """

    def setUp(self) -> None:
        super().setUp()
        # These tests are about the player being reached, so the real
        # _play_pcm has to run; only the device underneath it is faked.
        self.play_patcher.stop()
        self.addCleanup(self.play_patcher.start)

        self.players: list[_FakePlayer] = []

        def make_player(samplerate, device=None, blocksize=None):
            sink = _FakePlayer(samplerate)
            self.players.append(sink)
            return sink

        player_patcher = mock.patch.object(self.speaker.player, "Player", make_player)
        player_patcher.start()
        self.addCleanup(player_patcher.stop)

        self.speaker._stop_pending = False
        self.addCleanup(setattr, self.speaker, "_stop_pending", False)

    def _play(self, on_wait=None) -> _FakePlayer:
        """Run one _play_pcm, optionally firing `on_wait` mid-playback."""
        pending = {"hook": on_wait}

        def make_and_arm(samplerate, device=None, blocksize=None):
            sink = _FakePlayer(samplerate)
            sink.on_wait = pending["hook"]
            self.players.append(sink)
            return sink

        with mock.patch.object(self.speaker.player, "Player", make_and_arm):
            self.speaker._play_pcm(b"\x00\x00" * 8, 22050)
        return self.players[-1]

    def test_stop_during_playback_aborts_it(self) -> None:
        # The ordinary barge-in: the player is registered as _current and the
        # speaking thread is inside wait() when the interrupt lands.
        sink = self._play(on_wait=self.speaker.stop)
        self.assertTrue(sink.aborted)

    def test_stop_before_anything_plays_still_lands(self) -> None:
        # The measured hole: stop() during synthesis, player created after.
        self.speaker.stop()
        self.speaker._play_pcm(b"\x00\x00" * 8, 22050)

        self.assertTrue(self.players[0].aborted)

    def test_speak_clears_a_stop_meant_for_the_previous_reply(self) -> None:
        self.speaker.stop()

        self.speaker.TTS_ENGINE = "edge"
        self.speaker._VOICE_PROVIDERS["edge"] = lambda text: True
        self.speaker.speak("γεια")

        self.assertFalse(self.speaker._stop_pending)

    def test_a_stale_stop_does_not_silence_the_next_reply(self) -> None:
        self.speaker.stop()

        self.speaker.TTS_ENGINE = "piper"
        self.speaker.speak("γεια")

        self.assertTrue(self.players)
        self.assertFalse(self.players[-1].aborted)

    def test_the_player_is_always_closed(self) -> None:
        self.speaker._play_pcm(b"\x00\x00" * 8, 22050)
        self.assertTrue(self.players[0].closed)


if __name__ == "__main__":
    unittest.main()
