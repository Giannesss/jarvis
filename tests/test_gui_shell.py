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
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton

    from jarvis.gui import main_window as _main_window_module
    from jarvis.gui.main_window import (
        CLOCK_LABEL,
        CPU_LABEL,
        DATE_LABEL,
        DEBUG_ACTION,
        MONITOR_POLL_MS,
        NAV_CHAT,
        NAV_FILES,
        NAV_HOME,
        NAV_SETTINGS,
        NAV_TASKS,
        NETWORK_LABEL,
        RAM_LABEL,
        RECORD_BUTTON,
        STATUS_LABEL,
        TASKS_LIST,
        TRANSCRIPT_LIST,
        VRAM_LABEL,
        MainWindow,
        State,
        _format_rate,
        widget,
    )

    # speaker.speak() is mocked in every test below that reaches it, so
    # nothing in this suite ever opens an audio device or a network
    # connection for TTS -- same discipline as listener.listen()/brain.ask()
    # being mocked throughout.

    _PYSIDE6_AVAILABLE = True
except ImportError:
    _PYSIDE6_AVAILABLE = False

# Every completed turn now also calls _load_tasks() (see
# _on_speak_finished()), which calls db.connect() -- patched globally for
# this whole module, the same reason listener.listen()/brain.ask()/
# speaker.speak() are mocked in every individual test, so that no test here
# ever opens the real data/jarvis.db file. A bare MagicMock's default
# __iter__ (an empty iterator) is what makes memory.agenda() come back []
# against it without needing its own try/except -- construction-time
# assertions elsewhere in this file that the tasks list starts empty rely
# on exactly that. Tests that care what the tasks list actually shows
# (TasksWiringTests, below) separately mock memory.agenda() itself to
# supply real items.
_db_connect_patcher: mock._patch | None = None


def setUpModule() -> None:
    global _db_connect_patcher
    if not _PYSIDE6_AVAILABLE:
        return
    _db_connect_patcher = mock.patch("jarvis.gui.main_window.db.connect")
    _db_connect_patcher.start()


def tearDownModule() -> None:
    if _db_connect_patcher is not None:
        _db_connect_patcher.stop()


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
        self.assertIs(window._state, State.IDLE)

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
        # DEBUG_ACTION is a QCheckBox on the Settings page now (it used to
        # be a menu QAction before the redesign) -- an ordinary QWidget, so
        # widget()'s findChild(QWidget, ...) sees it directly.
        debug_checkbox = widget(window, DEBUG_ACTION)
        self.assertFalse(debug_checkbox.isChecked())

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
        self.assertIs(window._state, State.IDLE)
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
            self.assertIs(window._state, State.LISTENING)
            window._start_listening()  # the guard this pins
            self.assertIs(window._listen_thread, first_thread)
            self.assertIs(window._state, State.LISTENING)
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
            self.assertIs(window._state, State.THINKING)
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
            self.assertIs(window._state, State.SPEAKING)
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
            self.assertIs(window._state, State.SPEAKING)

            window._speak_thread.wait(2000)
            self.app.processEvents()
            speak.assert_called_once_with("Καλημέρα.")

        self.assertEqual(widget(window, STATUS_LABEL).text(), "Κατάσταση: Αδρανές")
        self.assertTrue(widget(window, RECORD_BUTTON).isEnabled())
        self.assertIs(window._state, State.IDLE)
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
        self.assertIs(window._state, State.IDLE)


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class DebugModeWiringTests(unittest.TestCase):
    """Phase 6 step 5's fifth and final piece: the debug-mode toggle, wired
    to append one line per turn describing the audit row policy.py already
    wrote for it -- db.connect() is mocked per-test here (overriding the
    module-wide patch from setUpModule) so each test controls exactly what
    the fake "SELECT ... FROM audit" cursor returns."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _enable_debug_mode(window: MainWindow) -> None:
        widget(window, DEBUG_ACTION).setChecked(True)

    def test_debug_mode_is_off_by_default(self) -> None:
        window = MainWindow()
        self.assertFalse(window._debug_mode_enabled())

    def test_a_reply_with_debug_mode_off_appends_no_debug_line(self) -> None:
        window = MainWindow()
        with mock.patch("jarvis.gui.main_window.speaker.speak"):
            window._on_reply_finished("Καλημέρα.")
            window._speak_thread.wait(2000)
            self.app.processEvents()

        transcript = widget(window, TRANSCRIPT_LIST)
        self.assertEqual(transcript.count(), 1)
        self.assertEqual(transcript.item(0).text(), "Jarvis: Καλημέρα.")

    def test_a_reply_with_debug_mode_on_appends_the_last_audit_row(self) -> None:
        window = MainWindow()
        self._enable_debug_mode(window)

        fake_conn = mock.MagicMock()
        fake_conn.execute.return_value.fetchone.return_value = (
            "brain",
            "allowed",
            "no_skill_matched",
        )
        with (
            mock.patch("jarvis.gui.main_window.speaker.speak"),
            mock.patch(
                "jarvis.gui.main_window.db.connect", return_value=fake_conn
            ),
        ):
            window._on_reply_finished("Καλημέρα.")
            window._speak_thread.wait(2000)
            self.app.processEvents()

        transcript = widget(window, TRANSCRIPT_LIST)
        self.assertEqual(transcript.count(), 2)
        self.assertEqual(transcript.item(0).text(), "Jarvis: Καλημέρα.")
        self.assertEqual(
            transcript.item(1).text(),
            "[debug] brain → allowed (no_skill_matched)",
        )

    def test_a_database_error_leaves_the_debug_line_out_without_crashing(
        self,
    ) -> None:
        window = MainWindow()
        self._enable_debug_mode(window)

        with (
            mock.patch("jarvis.gui.main_window.speaker.speak"),
            mock.patch(
                "jarvis.gui.main_window.db.connect",
                side_effect=RuntimeError("database is locked"),
            ),
        ):
            window._on_reply_finished("Καλημέρα.")  # must not raise
            window._speak_thread.wait(2000)
            self.app.processEvents()

        transcript = widget(window, TRANSCRIPT_LIST)
        self.assertEqual(transcript.count(), 1)
        self.assertEqual(transcript.item(0).text(), "Jarvis: Καλημέρα.")


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class SettingsPageTests(unittest.TestCase):
    """The settings surface's real home after the redesign: a page in the
    sidebar's QStackedWidget (NAV_SETTINGS), not a modal QDialog -- it's
    built once at construction like every other page, so there's no
    exec()/blocking concern here the way the old dialog needed one mocked."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_the_settings_page_is_reachable_from_the_sidebar(self) -> None:
        window = MainWindow()
        nav_button = window.findChild(QPushButton, _main_window_module.NAV_SETTINGS)
        self.assertIsNotNone(nav_button)

        nav_button.click()

        self.assertEqual(window._stack.currentWidget(), window._stack.widget(4))
        self.assertTrue(nav_button.isChecked())

    def test_the_settings_page_shows_the_real_brain_provider(self) -> None:
        with mock.patch("jarvis.gui.main_window.config.BRAIN_PROVIDER", "ollama"):
            window = MainWindow()  # must not raise, whatever BRAIN_PROVIDER is

        # QFormLayout rows aren't addressed by objectName, so this checks the
        # page's visible text directly rather than a specific widget -- the
        # real guarantee is that building the page from the live config
        # values never raises and the value actually appears somewhere on it.
        settings_page = window._stack.widget(4)
        labels = [
            child.text()
            for child in settings_page.findChildren(QLabel)
        ]
        self.assertTrue(any("ollama" in text for text in labels))


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class StateMachineTests(unittest.TestCase):
    """Phase 6 step 5's first piece: _set_state()/State, exercised directly
    rather than through a full turn -- the transitions a real turn drives it
    through are already pinned above (Microphone/Brain/SpeechWiringTests all
    assert on window._state at the relevant point now). This class pins
    _set_state() itself: every state's status text, and that only IDLE
    leaves the record button enabled."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_every_state_sets_its_own_status_text(self) -> None:
        window = MainWindow()
        expected = {
            State.IDLE: "Κατάσταση: Αδρανές",
            State.LISTENING: "Κατάσταση: Ακούω...",
            State.THINKING: "Κατάσταση: Σκέφτεται...",
            State.SPEAKING: "Κατάσταση: Μιλάει...",
        }
        for state, text in expected.items():
            window._set_state(state)
            self.assertEqual(widget(window, STATUS_LABEL).text(), text)

    def test_only_idle_leaves_the_record_button_enabled(self) -> None:
        window = MainWindow()
        for state in State:
            window._set_state(state)
            self.assertEqual(
                widget(window, RECORD_BUTTON).isEnabled(), state is State.IDLE
            )


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class MonitoringTests(unittest.TestCase):
    """Phase 6 step 5's second piece: _poll_system(), exercised directly
    rather than by waiting on the real MONITOR_POLL_MS timer -- psutil is
    mocked throughout, so nothing here reads this machine's actual CPU/RAM
    (which would make the assertions below flaky by definition)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_construction_starts_an_active_timer_at_the_right_interval(
        self,
    ) -> None:
        # Construction itself isn't mocked here -- a real (short, cheap)
        # psutil call during the priming step and during this timer's first
        # tick (which never fires inside a test; only a real wait would
        # trigger it) is harmless and exactly what the real app does too.
        window = MainWindow()
        self.assertTrue(window._monitor_timer.isActive())
        self.assertEqual(window._monitor_timer.interval(), MONITOR_POLL_MS)

    def test_poll_system_fills_in_cpu_and_ram_from_psutil(self) -> None:
        window = MainWindow()
        memory_reading = mock.Mock(percent=42.0)
        with mock.patch(
            "jarvis.gui.main_window.psutil.cpu_percent", return_value=17.0
        ), mock.patch(
            "jarvis.gui.main_window.psutil.virtual_memory",
            return_value=memory_reading,
        ), mock.patch(
            "jarvis.gui.main_window._read_vram_percent", return_value=None
        ):
            window._poll_system()

        self.assertEqual(widget(window, CPU_LABEL).text(), "CPU: 17%")
        self.assertEqual(widget(window, RAM_LABEL).text(), "RAM: 42%")

    def test_poll_system_fills_in_vram_when_an_nvidia_gpu_is_present(
        self,
    ) -> None:
        # _read_vram_percent() itself is mocked here rather than pynvml --
        # this pins _poll_system()'s own behaviour (what it does with a
        # reading it's handed), while GpuMonitoringTests below pins
        # _read_vram_percent()'s own fallback logic against a faked pynvml.
        window = MainWindow()
        with mock.patch(
            "jarvis.gui.main_window.psutil.cpu_percent", return_value=17.0
        ), mock.patch(
            "jarvis.gui.main_window.psutil.virtual_memory",
            return_value=mock.Mock(percent=42.0),
        ), mock.patch(
            "jarvis.gui.main_window._read_vram_percent", return_value=63.0
        ):
            window._poll_system()

        self.assertEqual(widget(window, VRAM_LABEL).text(), "VRAM: 63%")

    def test_poll_system_reports_vram_unavailable_with_no_nvidia_gpu(
        self,
    ) -> None:
        window = MainWindow()
        with mock.patch(
            "jarvis.gui.main_window.psutil.cpu_percent", return_value=17.0
        ), mock.patch(
            "jarvis.gui.main_window.psutil.virtual_memory",
            return_value=mock.Mock(percent=42.0),
        ), mock.patch(
            "jarvis.gui.main_window._read_vram_percent", return_value=None
        ):
            window._poll_system()

        self.assertEqual(widget(window, VRAM_LABEL).text(), "VRAM: μη διαθέσιμο")

    def test_closing_the_window_stops_the_monitor_timer(self) -> None:
        window = MainWindow()
        window.close()
        self.assertFalse(window._monitor_timer.isActive())


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class GpuMonitoringTests(unittest.TestCase):
    """_read_vram_percent() itself, exercised against a faked `pynvml`
    module in sys.modules -- the same idiom test_player.py uses for
    `sounddevice`, since this sandbox has no NVIDIA driver (and no real one
    should be required to run the suite). MonitoringTests above mocks
    _read_vram_percent() wholesale instead, which is right for pinning
    _poll_system()'s own behaviour; this class pins the function those
    mocks stand in for.

    `_nvml_handle`/`_nvml_unavailable` are module-level and cached across
    calls by design (see _read_vram_percent()'s docstring), which means
    they persist across tests unless reset -- setUp()/tearDown() do that
    here so one test's result can never leak into the next."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        _main_window_module._nvml_handle = None
        _main_window_module._nvml_unavailable = False

    def tearDown(self) -> None:
        _main_window_module._nvml_handle = None
        _main_window_module._nvml_unavailable = False
        sys.modules.pop("pynvml", None)

    def test_reads_a_percentage_from_a_present_nvidia_gpu(self) -> None:
        fake_pynvml = mock.Mock()
        fake_pynvml.nvmlDeviceGetHandleByIndex.return_value = "handle-0"
        fake_pynvml.nvmlDeviceGetMemoryInfo.return_value = mock.Mock(
            used=2_000, total=8_000
        )
        sys.modules["pynvml"] = fake_pynvml

        result = _main_window_module._read_vram_percent()

        self.assertEqual(result, 25.0)
        fake_pynvml.nvmlInit.assert_called_once()

    def test_a_missing_pynvml_package_reports_unavailable(self) -> None:
        # No fake installed at all -- "import pynvml" raises ImportError
        # exactly as it does on a machine that never installed
        # nvidia-ml-py, which is every environment this suite runs in.
        sys.modules.pop("pynvml", None)

        self.assertIsNone(_main_window_module._read_vram_percent())

    def test_a_present_package_with_no_gpu_also_reports_unavailable(self) -> None:
        fake_pynvml = mock.Mock()
        fake_pynvml.nvmlInit.side_effect = RuntimeError("NVML Shared Library Not Found")
        sys.modules["pynvml"] = fake_pynvml

        self.assertIsNone(_main_window_module._read_vram_percent())

    def test_failure_is_cached_and_never_retried(self) -> None:
        fake_pynvml = mock.Mock()
        fake_pynvml.nvmlInit.side_effect = RuntimeError("no driver")
        sys.modules["pynvml"] = fake_pynvml

        self.assertIsNone(_main_window_module._read_vram_percent())
        self.assertTrue(_main_window_module._nvml_unavailable)

        # Even if a driver now existed, _read_vram_percent() short-circuits
        # on the cached flag rather than trying nvmlInit() again.
        fake_pynvml.nvmlInit.side_effect = None
        fake_pynvml.nvmlInit.return_value = None
        self.assertIsNone(_main_window_module._read_vram_percent())
        fake_pynvml.nvmlInit.assert_called_once()

    def test_the_handle_is_only_fetched_once_across_calls(self) -> None:
        fake_pynvml = mock.Mock()
        fake_pynvml.nvmlDeviceGetHandleByIndex.return_value = "handle-0"
        fake_pynvml.nvmlDeviceGetMemoryInfo.return_value = mock.Mock(
            used=1_000, total=4_000
        )
        sys.modules["pynvml"] = fake_pynvml

        _main_window_module._read_vram_percent()
        _main_window_module._read_vram_percent()

        fake_pynvml.nvmlInit.assert_called_once()
        fake_pynvml.nvmlDeviceGetHandleByIndex.assert_called_once()
        self.assertEqual(fake_pynvml.nvmlDeviceGetMemoryInfo.call_count, 2)


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class TasksWiringTests(unittest.TestCase):
    """Phase 6 step 5's third piece: _load_tasks(), rendering
    memory.agenda() -- the same data «τι έχω σήμερα» already speaks -- as a
    list instead. memory.agenda() is mocked directly in every test here
    (the module-level db.connect() patch above only keeps construction
    itself from touching a real database; these tests go further and
    control exactly what agenda() hands back)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_construction_loads_todays_agenda(self) -> None:
        items = [
            ("class", "Φυσική 10:00"),
            ("exam", "Μαθηματικά"),
            ("reminder", "Πιες νερό"),
        ]
        with mock.patch(
            "jarvis.gui.main_window.memory.agenda", return_value=items
        ):
            window = MainWindow()

        tasks = widget(window, TASKS_LIST)
        self.assertEqual(tasks.count(), 3)
        self.assertEqual(tasks.item(0).text(), "μάθημα Φυσική 10:00")
        self.assertEqual(tasks.item(1).text(), "εξέταση Μαθηματικά")
        self.assertEqual(tasks.item(2).text(), "Πιες νερό")

    def test_a_database_error_leaves_the_tasks_list_empty_without_crashing(
        self,
    ) -> None:
        # Same discipline as memory.recall_safe(): a locked or broken
        # database costs this one refresh, never a crashed GUI.
        with mock.patch(
            "jarvis.gui.main_window.memory.agenda",
            side_effect=RuntimeError("database is locked"),
        ):
            window = MainWindow()

        self.assertEqual(widget(window, TASKS_LIST).count(), 0)

    def test_a_completed_turn_refreshes_the_tasks_list(self) -> None:
        # _on_speak_finished() is exercised directly (as SpeechWiringTests
        # does for the rest of its behaviour) rather than through a full
        # recording -- the microphone/brain halves are already pinned
        # elsewhere, and the tasks-list refresh is the one piece of
        # _on_speak_finished() this class cares about.
        with mock.patch(
            "jarvis.gui.main_window.memory.agenda", return_value=[]
        ):
            window = MainWindow()
        self.assertEqual(widget(window, TASKS_LIST).count(), 0)

        with mock.patch(
            "jarvis.gui.main_window.memory.agenda",
            return_value=[("reminder", "Νέα υπενθύμιση")],
        ):
            window._on_speak_finished()

        tasks = widget(window, TASKS_LIST)
        self.assertEqual(tasks.count(), 1)
        self.assertEqual(tasks.item(0).text(), "Νέα υπενθύμιση")


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class RenderAgendaItemTests(unittest.TestCase):
    """_render_agenda_item() is a pure function and needs no QApplication --
    but it still lives in main_window.py, which imports PySide6 at module
    level, so it's gated the same as every other test here rather than
    actually running in this sandbox."""

    def test_an_exam_is_prefixed(self) -> None:
        self.assertEqual(
            _main_window_module._render_agenda_item("exam", "Φυσική"),
            "εξέταση Φυσική",
        )

    def test_a_class_is_prefixed(self) -> None:
        self.assertEqual(
            _main_window_module._render_agenda_item("class", "Χημεία 09:00"),
            "μάθημα Χημεία 09:00",
        )

    def test_a_reminder_is_shown_as_is(self) -> None:
        self.assertEqual(
            _main_window_module._render_agenda_item("reminder", "Πιες νερό"),
            "Πιες νερό",
        )


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class QuickActionWiringTests(unittest.TestCase):
    """Phase 6 step 5's fourth piece: the quick-action buttons, wired to
    exactly the functions the voice skill itself calls --
    skills._open_site()/_open_app() -- rather than a second copy of "how to
    open a site/app". skills._open_site/_open_app are mocked directly in
    every test here, so no test ever opens a real browser or launches a real
    process."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _button(window: MainWindow, label: str):
        from PySide6.QtWidgets import QPushButton

        for button in window.findChildren(QPushButton):
            if button.text() == label:
                return button
        raise LookupError(f"no quick-action button labelled {label!r}")

    def test_a_site_button_is_enabled_and_calls_open_site(self) -> None:
        window = MainWindow()
        button = self._button(window, "YouTube")
        self.assertTrue(button.isEnabled())

        with mock.patch("jarvis.gui.main_window.skills._open_site") as open_site:
            button.click()

        open_site.assert_called_once_with("https://www.youtube.com")
        transcript = widget(window, TRANSCRIPT_LIST)
        self.assertEqual(transcript.item(transcript.count() - 1).text(), "Jarvis: Άνοιξα το YouTube.")

    def test_an_app_button_is_enabled_and_calls_open_app(self) -> None:
        window = MainWindow()
        button = self._button(window, "Notepad")
        self.assertTrue(button.isEnabled())

        with mock.patch("jarvis.gui.main_window.skills._open_app") as open_app:
            button.click()

        open_app.assert_called_once_with(["notepad.exe"])
        transcript = widget(window, TRANSCRIPT_LIST)
        self.assertEqual(transcript.item(transcript.count() - 1).text(), "Jarvis: Άνοιξα το Notepad.")

    def test_a_failed_launch_reports_failure_without_raising(self) -> None:
        window = MainWindow()
        button = self._button(window, "Gmail")

        with mock.patch(
            "jarvis.gui.main_window.skills._open_site",
            side_effect=OSError("no browser"),
        ):
            button.click()  # must not raise

        transcript = widget(window, TRANSCRIPT_LIST)
        self.assertEqual(
            transcript.item(transcript.count() - 1).text(),
            "Jarvis: Δεν μπόρεσα να ανοίξω το Gmail.",
        )


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class SidebarNavigationTests(unittest.TestCase):
    """The redesign's own new navigation structure: a QStackedWidget with
    one page per sidebar button. Only NAV_SETTINGS is covered above
    (SettingsPageTests); this pins the other three and that exactly one
    sidebar button stays checked at a time, whichever one was clicked."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _nav(self, window: MainWindow, object_name: str) -> QPushButton:
        button = window.findChild(QPushButton, object_name)
        self.assertIsNotNone(button, f"no nav button named {object_name!r}")
        return button

    def test_home_is_the_page_shown_at_construction(self) -> None:
        window = MainWindow()
        self.assertEqual(window._stack.currentWidget(), window._stack.widget(0))
        self.assertTrue(self._nav(window, NAV_HOME).isChecked())

    def test_each_nav_button_switches_to_its_own_page_and_checks_itself(
        self,
    ) -> None:
        window = MainWindow()
        for object_name, index in (
            (NAV_CHAT, 1),
            (NAV_TASKS, 2),
            (NAV_FILES, 3),
            (NAV_SETTINGS, 4),
            (NAV_HOME, 0),
        ):
            self._nav(window, object_name).click()
            self.assertEqual(window._stack.currentWidget(), window._stack.widget(index))
            for other_name in (NAV_HOME, NAV_CHAT, NAV_TASKS, NAV_FILES, NAV_SETTINGS):
                self.assertEqual(
                    self._nav(window, other_name).isChecked(),
                    other_name == object_name,
                )


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class OrbTests(unittest.TestCase):
    """_Orb is purely decorative (see its docstring and ORB_TICK_MS's own
    comment on why the waveform isn't real audio), so there's nothing to pin
    about what it draws -- only that it tracks the state machine the same
    way the status label and record button do, via _set_state()."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_set_state_updates_the_orbs_own_state(self) -> None:
        window = MainWindow()
        for state in State:
            window._set_state(state)
            self.assertIs(window._orb._state, state)

    def test_tick_advances_the_phase_without_raising(self) -> None:
        window = MainWindow()
        before = window._orb._phase
        window._orb.tick()
        self.assertGreater(window._orb._phase, before)


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class FormatRateTests(unittest.TestCase):
    """_format_rate() is a pure function -- no QApplication needed to call
    it, but it lives in main_window.py, so it's gated like every other test
    here rather than actually running where PySide6 isn't installed."""

    def test_under_one_kb_still_reads_in_kb(self) -> None:
        self.assertEqual(_format_rate(500), "0.5 KB/s")

    def test_kilobytes_per_second(self) -> None:
        self.assertEqual(_format_rate(2048), "2.0 KB/s")

    def test_rolls_over_to_megabytes_per_second(self) -> None:
        self.assertEqual(_format_rate(1_258_291), "1.2 MB/s")


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class NetworkMonitoringTests(unittest.TestCase):
    """_poll_system()'s network-rate half: psutil only gives a cumulative
    byte count, so the first poll in a process has no "last" to diff
    against and must show nothing yet, exactly like cpu_percent()'s own
    priming call -- MonitoringTests above already pins the CPU/RAM/VRAM
    side of the same method."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _poll(self, window: MainWindow, total_bytes: int, now: float) -> None:
        counters = mock.Mock(bytes_sent=total_bytes, bytes_recv=0)
        with mock.patch(
            "jarvis.gui.main_window.psutil.cpu_percent", return_value=0.0
        ), mock.patch(
            "jarvis.gui.main_window.psutil.virtual_memory",
            return_value=mock.Mock(percent=0.0),
        ), mock.patch(
            "jarvis.gui.main_window._read_vram_percent", return_value=None
        ), mock.patch(
            "jarvis.gui.main_window.psutil.net_io_counters",
            return_value=counters,
        ), mock.patch(
            "jarvis.gui.main_window.time.monotonic", return_value=now
        ):
            window._poll_system()

    def test_the_first_poll_shows_no_rate_yet(self) -> None:
        window = MainWindow()
        placeholder = widget(window, NETWORK_LABEL).text()
        self._poll(window, total_bytes=1_000_000, now=10.0)
        # No "last" reading existed yet, so the label is left exactly as
        # _poll_system() found it -- unchanged from construction.
        self.assertEqual(widget(window, NETWORK_LABEL).text(), placeholder)

    def test_the_second_poll_computes_a_real_rate(self) -> None:
        window = MainWindow()
        self._poll(window, total_bytes=1_000_000, now=10.0)
        self._poll(window, total_bytes=1_000_000 + 2048, now=11.0)  # +2KB in 1s

        self.assertEqual(widget(window, NETWORK_LABEL).text(), "Network: 2.0 KB/s")


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed here")
class ClockTests(unittest.TestCase):
    """_update_clock() -- called once at construction and then every second
    by _clock_timer. A fixed datetime is patched in rather than asserting
    against whatever time the test happens to run at."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_the_clock_and_date_labels_reflect_the_current_time(self) -> None:
        import datetime as _datetime_module

        fixed = _datetime_module.datetime(2026, 9, 18, 18, 24)
        with mock.patch(
            "jarvis.gui.main_window.datetime"
        ) as fake_datetime:
            fake_datetime.now.return_value = fixed
            window = MainWindow()

        self.assertEqual(widget(window, CLOCK_LABEL).text(), "18:24")
        self.assertEqual(widget(window, DATE_LABEL).text(), fixed.strftime("%a %d %b %Y"))

    def test_closing_the_window_stops_the_clock_timer(self) -> None:
        window = MainWindow()
        window.close()
        self.assertFalse(window._clock_timer.isActive())


if __name__ == "__main__":
    unittest.main()
