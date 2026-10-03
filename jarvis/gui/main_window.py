"""The Phase 6 shell. Step 3 of the roadmap's "Steps (do in order)" built the
empty states ("Build a functional shell first"); step 4 -- "wire in mic/
brain/TTS one at a time, testing after each" -- has now wired in the first
of those three: the microphone. Nothing here calls into brain.py or
speaker.py yet; that is the rest of step 4, not this one.

Every widget that something will eventually read or write from outside this
file has a stable `objectName()` set on it (see `_NAMED_WIDGETS` and the
`widget()` helper below), so later steps -- and this file's own smoke test --
can find it without reaching into private attributes or rebuilding the
layout to get a handle on something.

**The microphone listens on a `QThread`, never on the UI thread.**
`listener.listen()` blocks -- it shells out to ffmpeg and then runs Whisper --
for as long as the user is speaking plus however long transcription takes,
and a blocked UI thread in Qt means a frozen, unresponsive window (no
repaint, no click, the OS offers to kill it). `_ListenWorker` runs `listen()`
on its own thread and reports back over a signal, which Qt marshals onto the
UI thread automatically -- the one safe way to touch a widget from work that
started on another thread. `MainWindow` never calls `listener.listen()`
itself for that reason.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from jarvis import listener
from jarvis.config import SKILL_APPS, SKILL_SITES

# Object names for every widget a later step, or a test, needs to find again.
# Centralized here rather than scattered as string literals through the
# layout code below, the same reason policy.py keeps its audit vocabulary in
# one place: one typo in one spot is a NameError at import time if it's ever
# wrong, not a silent miss three files away.
STATUS_LABEL = "status_label"
TRANSCRIPT_LIST = "transcript_list"
TASKS_LIST = "tasks_list"
CPU_LABEL = "cpu_label"
RAM_LABEL = "ram_label"
DEBUG_ACTION = "debug_mode_action"
RECORD_BUTTON = "record_button"

_STATUS_IDLE = "Κατάσταση: Αδρανές"
_STATUS_LISTENING = "Κατάσταση: Ακούω..."


class _ListenWorker(QThread):
    """Runs `listener.listen()` off the UI thread and reports back over a
    signal. `finished_with_text` carries `None` the same way `listen()`
    itself does -- no speech, a device failure, anything that isn't a
    transcript -- so the slot on the other end can treat "say nothing" as an
    ordinary outcome rather than an error."""

    finished_with_text = Signal(object)

    def run(self) -> None:
        text = listener.listen()
        self.finished_with_text.emit(text)


class MainWindow(QMainWindow):
    """Jarvis's main window. Construction starts no timers and no threads --
    `_listen_thread` is only ever created in response to a click on the
    record button, never at construction. `gui_main.py` is the only thing
    that instantiates this."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Jarvis")
        self.resize(900, 600)

        # Holds the in-flight _ListenWorker, if any -- None the rest of the
        # time. Kept as an attribute rather than a local so the thread object
        # isn't garbage-collected out from under itself while it's running,
        # and so _start_listening can refuse a second click while one is
        # already recording.
        self._listen_thread: _ListenWorker | None = None

        self._build_menu_bar()
        self._build_central_widget()
        self._build_status_bar()

    def closeEvent(self, event) -> None:  # noqa: N802 -- Qt's own name
        # A QThread still running when its Python wrapper is destroyed
        # prints a Qt warning and can crash on some platforms. Waiting here
        # blocks the window on close for at most as long as listen() itself
        # already bounds a turn to (MAX_RECORD_SECONDS) -- not ideal, but
        # finite, and simpler than teaching listener.listen() to be
        # cancellable before anything in the GUI actually needs that.
        if self._listen_thread is not None:
            self._listen_thread.wait()
        super().closeEvent(event)

    # --- Menu bar -----------------------------------------------------------

    def _build_menu_bar(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("&Αρχείο")
        exit_action = QAction("Έξοδος", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        settings_menu = menu_bar.addMenu("&Ρυθμίσεις")

        # Placeholder: the real settings surface (mic device, TTS engine,
        # brain provider, etc. -- everything jarvis/config.py reads from
        # .env today) is "settings/debug mode", the last of Phase 6's five
        # steps. For now this just proves the menu structure is right.
        settings_action = QAction("Ρυθμίσεις…", self)
        settings_action.triggered.connect(self._show_settings_placeholder)
        settings_menu.addAction(settings_action)

        # Checkable, but inert: toggling it only updates its own checked
        # state right now. Wiring it to anything (e.g. diag.py's verbosity,
        # or WAKE_DEBUG-style printouts surfaced in the UI instead of the
        # terminal) is also part of that later step.
        debug_action = QAction("Λειτουργία αποσφαλμάτωσης", self)
        debug_action.setObjectName(DEBUG_ACTION)
        debug_action.setCheckable(True)
        settings_menu.addAction(debug_action)

    def _show_settings_placeholder(self) -> None:
        QMessageBox.information(
            self,
            "Ρυθμίσεις",
            "Οι ρυθμίσεις δεν έχουν συνδεθεί ακόμα -- αυτό είναι μόνο το "
            "άδειο κέλυφος (Phase 6, βήμα 3).",
        )

    # --- Central widget: transcript + side panel -----------------------------

    def _build_central_widget(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)

        outer = QVBoxLayout(central)

        # The status strip. A static placeholder today; the state machine
        # step is what makes this track listening/thinking/speaking instead
        # of always reading "Αδρανές" (idle).
        status_label = QLabel("Κατάσταση: Αδρανές")
        status_label.setObjectName(STATUS_LABEL)
        status_label.setStyleSheet("font-weight: bold; padding: 4px;")
        outer.addWidget(status_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter, stretch=1)

        splitter.addWidget(self._build_transcript_panel())
        splitter.addWidget(self._build_side_panel())
        # The transcript gets most of the width; the side panel is fixed-ish.
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

    def _build_transcript_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("Συνομιλία"))

        transcript_list = QListWidget()
        transcript_list.setObjectName(TRANSCRIPT_LIST)
        # Starts empty; a real turn appends to it (see _on_listen_finished).
        # Still nothing invented here -- every line it ever holds came back
        # from listener.listen(), not a placeholder this file wrote.
        layout.addWidget(transcript_list)

        record_button = QPushButton("Εγγραφή")
        record_button.setObjectName(RECORD_BUTTON)
        record_button.clicked.connect(self._start_listening)
        layout.addWidget(record_button)

        return panel

    # --- Microphone (Phase 6 step 4, part 1 of 3) ----------------------------

    def _start_listening(self) -> None:
        # Guards against a second click starting a second recording while
        # one is already in flight -- listener.listen() isn't reentrant (one
        # ffmpeg process, one temp wav path), and the button is disabled for
        # the same reason, so this is a backstop for anything that can still
        # reach here (e.g. a held Enter key) rather than the only guard.
        if self._listen_thread is not None:
            return

        widget(self, STATUS_LABEL).setText(_STATUS_LISTENING)
        widget(self, RECORD_BUTTON).setEnabled(False)

        self._listen_thread = _ListenWorker(self)
        self._listen_thread.finished_with_text.connect(self._on_listen_finished)
        self._listen_thread.start()

    def _on_listen_finished(self, text: object) -> None:
        # listener.listen() returns str | None; Signal(object) is what
        # carries that union across the thread boundary, since Qt's typed
        # signals need one concrete type. text is re-narrowed here, not at
        # the signal, for exactly that reason.
        if isinstance(text, str) and text:
            widget(self, TRANSCRIPT_LIST).addItem(f"Εσύ: {text}")
        # No transcript, same as main.py's own "if not text: continue" --
        # silence, a device failure, or nothing said is not an error here,
        # just a turn with nothing to show. brain.ask()/speaker.speak() are
        # not wired in yet (the rest of step 4), so there is no reply to add
        # beside it.

        widget(self, STATUS_LABEL).setText(_STATUS_IDLE)
        widget(self, RECORD_BUTTON).setEnabled(True)
        self._listen_thread = None

    def _build_side_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        layout.addWidget(self._build_quick_actions_box())
        layout.addWidget(self._build_tasks_box())
        layout.addWidget(self._build_monitoring_box())
        layout.addStretch(1)

        return panel

    def _build_quick_actions_box(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.addWidget(QLabel("Γρήγορες ενέργειες"))

        # One disabled button per site/app Jarvis can already open by voice
        # (config.SKILL_SITES / SKILL_APPS) -- so the panel reflects what's
        # really configured rather than a fixed, separately-maintained list
        # that drifts from it. Disabled rather than wired to a no-op: a
        # button that looks clickable but does nothing is worse than one
        # that's honestly not ready yet. Wiring a click to
        # skills._open_site()/_open_app() is quick-actions' own later step.
        for label in list(SKILL_SITES) + list(SKILL_APPS):
            button = QPushButton(label)
            button.setEnabled(False)
            layout.addWidget(button)

        return box

    def _build_tasks_box(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.addWidget(QLabel("Εργασίες"))

        tasks_list = QListWidget()
        tasks_list.setObjectName(TASKS_LIST)
        # Empty by design -- this is the courses/exams/reminders agenda
        # (memory.agenda(), already built and voice-driven) rendered as a
        # list instead of spoken, which is a later step's wiring, not new
        # data of its own.
        layout.addWidget(tasks_list)

        return box

    def _build_monitoring_box(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.addWidget(QLabel("Σύστημα"))

        row = QHBoxLayout()
        cpu_label = QLabel("CPU: —")
        cpu_label.setObjectName(CPU_LABEL)
        ram_label = QLabel("RAM: —")
        ram_label.setObjectName(RAM_LABEL)
        row.addWidget(cpu_label)
        row.addWidget(ram_label)
        layout.addLayout(row)

        # "—" rather than "0%": a real reading of zero and "nothing has
        # measured this yet" must not look the same on screen. A poller
        # (psutil, most likely) that fills these in on a timer is "real
        # system monitoring", two steps after this one.
        return box

    # --- Status bar -----------------------------------------------------------

    def _build_status_bar(self) -> None:
        bar = QStatusBar()
        bar.showMessage("Jarvis — Phase 6, βήμα 4: μικρόφωνο συνδεδεμένο")
        self.setStatusBar(bar)


def widget(window: MainWindow, object_name: str) -> QWidget:
    """Look up a named widget by the constants above, for callers (and the
    smoke test) that need a handle without reaching into private attributes."""
    found = window.findChild(QWidget, object_name)
    if found is None:
        raise LookupError(f"no widget named {object_name!r}")
    return found
