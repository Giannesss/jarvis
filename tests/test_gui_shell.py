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

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    from jarvis.gui.main_window import (
        CPU_LABEL,
        DEBUG_ACTION,
        RAM_LABEL,
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


if __name__ == "__main__":
    unittest.main()
