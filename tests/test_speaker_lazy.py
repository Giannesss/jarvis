"""Tests for jarvis/speaker.py's lazy Piper load.

The Piper voice used to be loaded at module scope, so importing jarvis.speaker
-- which jarvis/skills.py does transitively -- read a ~63MB ONNX off disk even
when TTS_ENGINE=edge and Piper was never used. It is now a lazy singleton
(_get_voice), and these tests pin both halves of that: that nothing loads at
import, and that the Edge-then-Piper fallback behaves exactly as it did before.

Nothing here touches real audio, the network, or the real model. `piper` is
replaced with a fake module in sys.modules so the load is observable and free,
winsound is patched so nothing plays, and edge_tts is never reached because
_speak_edge itself is patched.
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

    def setUp(self) -> None:
        self.fake_voice = mock.Mock(name="PiperVoice instance")
        # A real PiperVoice fills in the wave header; without this the
        # `with wave.open(...)` block in _speak_piper raises on close.
        self.fake_voice.synthesize_wav.side_effect = _fake_synthesize

        self.piper_voice_cls = mock.Mock(name="PiperVoice")
        self.piper_voice_cls.load.return_value = self.fake_voice

        fake_piper = mock.Mock(name="piper module")
        fake_piper.PiperVoice = self.piper_voice_cls

        patcher = mock.patch.dict(sys.modules, {"piper": fake_piper})
        patcher.start()
        self.addCleanup(patcher.stop)

        import jarvis.speaker

        self.speaker = importlib.reload(jarvis.speaker)

        # Never play anything, in any test in this file.
        winsound_patcher = mock.patch.object(self.speaker, "winsound")
        self.winsound = winsound_patcher.start()
        self.addCleanup(winsound_patcher.stop)

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
        self.winsound.PlaySound.assert_called_once()

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
        # listener._CaptureGate mutes the mic on is_speaking(), which is
        # _lock.locked() -- so the lock must be held across synthesis, not just
        # playback, or Jarvis records its own voice.
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


if __name__ == "__main__":
    unittest.main()
