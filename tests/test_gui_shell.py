"""Smoke test for the Phase 6 shell (jarvis/gui/main_window.py).

PySide6 is a heavy, platform-specific dependency the rest of the suite has
no reason to need -- skills.py, memory.py and friends must keep importing
cleanly on a machine that's never heard of Qt. So this whole module is
skipped, not failed, when PySide6 isn't installed, the same shape as the
rest of the suite being skippable-by-environment rather than broken by it
(see CLAUDE.md "Config"/"Providers" for the lazy-import idiom this mirrors).

QT_QPA_PLATFORM=offscreen is what lets this construct real widgets with no
display attached -- this container has none, and neither does a CI runner,
but the dev machine running `python -m unittest` by hand doesn't need one
either. It must be set before QApplication is ever constructed, so it's set
at import time, not inside a test method.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    from jarvis.gui.main_window import (
        CPU_LABEL,
        DEBUG_ACTION,
        RAM_LABEL,
        RECORD_BUTTON,
        STATUS_LABEL,
        TASKS_LIST,
        TRANSCRIPT_LIST,
        MainWindow,
        widget,
    )

    # speaker.speak() is mocked in every test below that reaches it, so
    # nothing in this suite ever opens an audio device or a network
    # connection for TTS -- same discipline as listener.listen()/brain.ask()
    # being mocked throughout.

    _PYSIDE6_AVAILABLE = True
except ImportError:
    _PYSIDE6_AVAILABLE = False


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class ShellConstructionTests(unittest.TestCase):
    """Only construction and findability are pinned here -- there is no
    behaviour to test yet (see the module docstring in main_window.py for
    why the shell is deliberately empty at this step)."""

    @classmethod
    def setUpClass(cls) -> None:
        # One QApplication per process is the Qt rule, not per test; building
        # a second one raises. A class-level singleton is the standard way
        # unittest+Qt tests share it across test methods in this file.
        cls.app = QApplication.instance() or QApplication([])

    def test_window_constructs_with_no_backend_calls(self) -> None:
        # The real guarantee this test exists for: importing and
        # constructing the shell must never reach into listener.py,
        # brain.py or speaker.py -- none of which this test configures or
        # mocks. If construction ever starts a microphone stream or a
        # network call, this test is where that would surface first.
        window = MainWindow()
        self.assertEqual(window.windowTitle(), "Jarvis")

    def test_every_named_widget_is_present_and_empty(self) -> None:
        window = MainWindow()

        status = widget(window, STATUS_LABEL)
        self.assertIn("Αδρανές", status.text())

        transcript = widget(window, TRANSCRIPT_LIST)
        self.assertEqual(transcript.count(), 0)

        tasks = widget(window, TASKS_LIST)
        self.assertEqual(tasks.count(), 0)

        cpu = widget(window, CPU_LABEL)
        ram = widget(window, RAM_LABEL)
        self.assertIn("—", cpu.text())
        self.assertIn("—", ram.text())

    def test_debug_action_is_checkable_and_starts_unchecked(self) -> None:
        window = MainWindow()
        # QAction isn't a QWidget, so widget()'s findChild(QWidget, ...)
        # can't see it -- found directly here instead, by the same object
        # name, via QAction's own findChild.
        from PySide6.QtGui import QAction

        debug_action = window.findChild(QAction, DEBUG_ACTION)
        self.assertIsNotNone(debug_action)
        self.assertTrue(debug_action.isCheckable())
        self.assertFalse(debug_action.isChecked())

    def test_unknown_widget_name_raises_rather_than_returning_none(self) -> None:
        window = MainWindow()
        with self.assertRaises(LookupError):
            widget(window, "does_not_exist")


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class MicrophoneWiringTests(unittest.TestCase):
    """Phase 6 step 4's first piece: the record button runs
    listener.listen() on a background thread and reports back onto the UI
    thread. listener.listen() itself is mocked throughout -- these tests
    pin the wiring, not the recording/transcription it was already pinned
    for in test_record_timing.py. skills.handle is also mocked, to a fixed
    reply, so a spoken turn doesn't fall through into a real brain call --
    that path belongs to BrainWiringTests below, not here."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _run_one_turn(self, window: MainWindow, spoken: str | None) -> mock.Mock:
        # _start_listening kicks off a real QThread; .wait() blocks this
        # (test) thread until it finishes, and processEvents() is what
        # delivers the queued finished_with_text signal to the slot on this
        # thread afterwards -- without it the assertions below would read
        # pre-signal state even though the worker thread has already exited.
        # Further wait+processEvents rounds cover the _BrainWorker and
        # _SpeakWorker threads that a transcribed turn triggers in sequence.
        # The speaker.speak mock is returned rather than kept private, so a
        # caller that cares what Jarvis was asked to say can assert on it.
        with mock.patch(
            "jarvis.gui.main_window.listener.listen", return_value=spoken
        ), mock.patch(
            "jarvis.gui.main_window.skills.handle", return_value="Εντάξει."
        ), mock.patch("jarvis.gui.main_window.speaker.speak") as speak:
            window._start_listening()
            self.assertIsNotNone(window._listen_thread)
            self.assertFalse(widget(window, RECORD_BUTTON).isEnabled())
            window._listen_thread.wait(2000)
            self.app.processEvents()
            if window._brain_thread is not None:
                window._brain_thread.wait(2000)
                self.app.processEvents()
            if window._speak_thread is not None:
                window._speak_thread.wait(2000)
        self.app.processEvents()
        return speak

    def test_a_transcript_and_reply_are_appended_and_the_button_recovers(
        self,
    ) -> None:
        window = MainWindow()
        speak = self._run_one_turn(window, "Τι ώρα είναι")
        speak.assert_called_once_with("Εντάξει.")

        transcript = widget(window, TRANSCRIPT_LIST)
        self.assertEqual(transcript.count(), 2)
        self.assertEqual(transcript.item(0).text(), "Εσύ: Τι ώρα είναι")
        self.assertEqual(transcript.item(1).text(), "Jarvis: Εντάξει.")

        self.assertEqual(widget(window, STATUS_LABEL).text(), "Κατάσταση: Αδρανές")
        self.assertTrue(widget(window, RECORD_BUTTON).isEnabled())
        self.assertIsNone(window._listen_thread)
        self.assertIsNone(window._brain_thread)
        self.assertIsNone(window._speak_thread)

    def test_no_speech_appends_nothing_and_never_starts_a_brain_thread(
        self,
    ) -> None:
        # listener.listen() returning None is an ordinary outcome (no
        # speech, a device failure), not an error -- same as main.py's own
        # "if not text: continue". Nothing should land in the transcript,
        # and with nothing transcribed there is nothing to send to the
        # brain either.
        window = MainWindow()
        self._run_one_turn(window, None)

        self.assertEqual(widget(window, TRANSCRIPT_LIST).count(), 0)
        self.assertTrue(widget(window, RECORD_BUTTON).isEnabled())
        self.assertIsNone(window._brain_thread)

    def test_a_second_click_while_listening_is_ignored(self) -> None:
        window = MainWindow()
        with mock.patch(
            "jarvis.gui.main_window.listener.listen", return_value="Γεια"
        ), mock.patch(
            "jarvis.gui.main_window.skills.handle", return_value="Γεια σου."
        ), mock.patch("jarvis.gui.main_window.speaker.speak"):
            window._start_listening()
            first_thread = window._listen_thread
            window._start_listening()  # the guard this pins
            self.assertIs(window._listen_thread, first_thread)
            first_thread.wait(2000)
            self.app.processEvents()
            if window._brain_thread is not None:
                window._brain_thread.wait(2000)
                self.app.processEvents()
            if window._speak_thread is not None:
                window._speak_thread.wait(2000)
        self.app.processEvents()

    def test_a_click_while_the_brain_is_thinking_is_ignored(self) -> None:
        # The guard also has to hold during the second (brain) half of a
        # turn, not just the first -- a click landing after transcription
        # but before the reply comes back must not start a second recording.
        window = MainWindow()
        with mock.patch(
            "jarvis.gui.main_window.listener.listen", return_value="Γεια"
        ), mock.patch(
            "jarvis.gui.main_window.skills.handle", return_value="Γεια σου."
        ), mock.patch("jarvis.gui.main_window.speaker.speak"):
            window._start_listening()
            window._listen_thread.wait(2000)
            self.app.processEvents()

            self.assertIsNotNone(window._brain_thread)
            self.assertFalse(widget(window, RECORD_BUTTON).isEnabled())
            window._start_listening()  # must be a no-op while thinking
            self.assertIsNone(window._listen_thread)

            window._brain_thread.wait(2000)
            self.app.processEvents()
            if window._speak_thread is not None:
                window._speak_thread.wait(2000)
        self.app.processEvents()

    def test_a_click_while_speaking_is_ignored(self) -> None:
        # The guard's third leg: a click landing after the reply is back but
        # while speaker.speak() is still playing it must not start a second
        # recording either.
        window = MainWindow()
        with mock.patch(
            "jarvis.gui.main_window.listener.listen", return_value="Γεια"
        ), mock.patch(
            "jarvis.gui.main_window.skills.handle", return_value="Γεια σου."
        ), mock.patch("jarvis.gui.main_window.speaker.speak"):
            window._start_listening()
            window._listen_thread.wait(2000)
            self.app.processEvents()
            window._brain_thread.wait(2000)
            self.app.processEvents()

            self.assertIsNotNone(window._speak_thread)
            self.assertFalse(widget(window, RECORD_BUTTON).isEnabled())
            window._start_listening()  # must be a no-op while speaking
            self.assertIsNone(window._listen_thread)

            window._speak_thread.wait(2000)
        self.app.processEvents()


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class BrainWiringTests(unittest.TestCase):
    """Phase 6 step 4's second piece: _get_reply()/_BrainWorker, exercised
    directly rather than through a recording -- the microphone half is
    already pinned above. skills.handle, policy and brain are all mocked,
    so these tests touch neither a real skill, a real database nor a real
    model."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _run_brain(self, window: MainWindow, text: str) -> None:
        # A non-empty reply now starts a _SpeakWorker too (see
        # _on_reply_finished), so speaker.speak is mocked here regardless of
        # what the caller's own `with` block patches -- otherwise a test that
        # only cares about the brain half would end up actually
        # synthesizing/playing audio.
        with mock.patch("jarvis.gui.main_window.speaker.speak"):
            window._on_listen_finished(text)  # starts the _BrainWorker
            self.assertIsNotNone(window._brain_thread)
            window._brain_thread.wait(2000)
            self.app.processEvents()
            if window._speak_thread is not None:
                window._speak_thread.wait(2000)
        self.app.processEvents()

    def test_a_matching_skill_answers_without_touching_the_brain(self) -> None:
        window = MainWindow()
        with mock.patch(
            "jarvis.gui.main_window.skills.handle", return_value="Η ώρα είναι 5."
        ), mock.patch("jarvis.gui.main_window.brain.ask") as ask:
            self._run_brain(window, "Τι ώρα είναι")
            ask.assert_not_called()

        transcript = widget(window, TRANSCRIPT_LIST)
        self.assertEqual(transcript.item(1).text(), "Jarvis: Η ώρα είναι 5.")

    def test_no_skill_match_falls_through_to_the_brain(self) -> None:
        window = MainWindow()
        with mock.patch(
            "jarvis.gui.main_window.skills.handle", return_value=None
        ), mock.patch(
            "jarvis.gui.main_window.policy.is_frozen", return_value=False
        ), mock.patch("jarvis.gui.main_window.policy.record"), mock.patch(
            "jarvis.gui.main_window.memory.recall_safe", return_value=None
        ), mock.patch(
            "jarvis.gui.main_window.brain.ask", return_value="Καλησπέρα!"
        ):
            self._run_brain(window, "Γεια σου Τζάρβις")

        transcript = widget(window, TRANSCRIPT_LIST)
        self.assertEqual(transcript.item(1).text(), "Jarvis: Καλησπέρα!")

    def test_frozen_backstop_speaks_nothing(self) -> None:
        # Same case main._answer()'s Answer(spoke=False) exists for: a skill
        # returned None while the kill switch is on. _get_reply() returns ""
        # and no "Jarvis: ..." line should appear.
        window = MainWindow()
        with mock.patch(
            "jarvis.gui.main_window.skills.handle", return_value=None
        ), mock.patch(
            "jarvis.gui.main_window.policy.is_frozen", return_value=True
        ), mock.patch("jarvis.gui.main_window.brain.ask") as ask:
            self._run_brain(window, "οτιδήποτε")
            ask.assert_not_called()

        self.assertEqual(widget(window, TRANSCRIPT_LIST).count(), 1)  # "Εσύ: ..." only

    def test_a_brain_exception_falls_back_to_the_fixed_error_line(self) -> None:
        window = MainWindow()
        with mock.patch(
            "jarvis.gui.main_window.skills.handle", return_value=None
        ), mock.patch(
            "jarvis.gui.main_window.policy.is_frozen", return_value=False
        ), mock.patch("jarvis.gui.main_window.policy.record"), mock.patch(
            "jarvis.gui.main_window.memory.recall_safe", return_value=None
        ), mock.patch(
            "jarvis.gui.main_window.brain.ask", side_effect=RuntimeError("down")
        ):
            self._run_brain(window, "Τι γίνεται")

        transcript = widget(window, TRANSCRIPT_LIST)
        self.assertEqual(
            transcript.item(1).text(),
            "Jarvis: Συγγνώμη, δεν μπορώ να απαντήσω αυτή τη στιγμή.",
        )


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class SpeechWiringTests(unittest.TestCase):
    """Phase 6 step 4's third piece: _on_reply_finished()/_SpeakWorker,
    exercised directly from a reply already in hand -- the recording and
    brain halves are already pinned above. speaker.speak is mocked
    throughout, so nothing here opens an audio device or reaches Edge/Piper.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_a_reply_is_spoken_and_the_button_recovers_after(self) -> None:
        window = MainWindow()
        with mock.patch("jarvis.gui.main_window.speaker.speak") as speak:
            window._on_reply_finished("Καλημέρα.")
            self.assertIsNotNone(window._speak_thread)
            # The transcript line and the "speaking" status appear before
            # speak() has even returned -- the point of showing text as soon
            # as it is decided, not only once it has finished being said.
            self.assertEqual(
                widget(window, TRANSCRIPT_LIST).item(0).text(), "Jarvis: Καλημέρα."
            )
            self.assertEqual(
                widget(window, STATUS_LABEL).text(), "Κατάσταση: Μιλάει..."
            )
            self.assertFalse(widget(window, RECORD_BUTTON).isEnabled())

            window._speak_thread.wait(2000)
            self.app.processEvents()
            speak.assert_called_once_with("Καλημέρα.")

        self.assertEqual(widget(window, STATUS_LABEL).text(), "Κατάσταση: Αδρανές")
        self.assertTrue(widget(window, RECORD_BUTTON).isEnabled())
        self.assertIsNone(window._speak_thread)

    def test_an_empty_reply_never_starts_a_speak_thread(self) -> None:
        # _get_reply()'s frozen-backstop case: "" means say nothing, and
        # nothing here should be handed to speaker.speak either.
        window = MainWindow()
        with mock.patch("jarvis.gui.main_window.speaker.speak") as speak:
            window._on_reply_finished("")
            speak.assert_not_called()

        self.assertIsNone(window._speak_thread)
        self.assertEqual(widget(window, TRANSCRIPT_LIST).count(), 0)
        self.assertEqual(widget(window, STATUS_LABEL).text(), "Κατάσταση: Αδρανές")
        self.assertTrue(widget(window, RECORD_BUTTON).isEnabled())


if __name__ == "__main__":
    unittest.main()
