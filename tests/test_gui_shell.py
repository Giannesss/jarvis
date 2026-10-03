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
    for in test_record_timing.py."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _run_one_turn(self, window: MainWindow, spoken: str | None) -> None:
        # _start_listening kicks off a real QThread; .wait() blocks this
        # (test) thread until it finishes, and processEvents() is what
        # delivers the queued finished_with_text signal to the slot on this
        # thread afterwards -- without it the assertions below would read
        # pre-signal state even though the worker thread has already exited.
        with mock.patch(
            "jarvis.gui.main_window.listener.listen", return_value=spoken
        ):
            window._start_listening()
            self.assertIsNotNone(window._listen_thread)
            self.assertFalse(widget(window, RECORD_BUTTON).isEnabled())
            window._listen_thread.wait(2000)
        self.app.processEvents()

    def test_a_transcript_is_appended_and_the_button_recovers(self) -> None:
        window = MainWindow()
        self._run_one_turn(window, "Τι ώρα είναι")

        transcript = widget(window, TRANSCRIPT_LIST)
        self.assertEqual(transcript.count(), 1)
        self.assertEqual(transcript.item(0).text(), "Εσύ: Τι ώρα είναι")

        self.assertEqual(widget(window, STATUS_LABEL).text(), "Κατάσταση: Αδρανές")
        self.assertTrue(widget(window, RECORD_BUTTON).isEnabled())
        self.assertIsNone(window._listen_thread)

    def test_no_speech_appends_nothing(self) -> None:
        # listener.listen() returning None is an ordinary outcome (no
        # speech, a device failure), not an error -- same as main.py's own
        # "if not text: continue". Nothing should land in the transcript.
        window = MainWindow()
        self._run_one_turn(window, None)

        self.assertEqual(widget(window, TRANSCRIPT_LIST).count(), 0)
        self.assertTrue(widget(window, RECORD_BUTTON).isEnabled())

    def test_a_second_click_while_listening_is_ignored(self) -> None:
        window = MainWindow()
        with mock.patch(
            "jarvis.gui.main_window.listener.listen", return_value="Γεια"
        ):
            window._start_listening()
            first_thread = window._listen_thread
            window._start_listening()  # the guard this pins
            self.assertIs(window._listen_thread, first_thread)
            first_thread.wait(2000)
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
