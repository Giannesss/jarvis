"""The Phase 6 shell -- step 3 of the roadmap's "Steps (do in order)":
"Build a functional shell first (empty states, no logic)."

Nothing in this file calls into listener.py, brain.py, speaker.py, or any
other backend module. Every widget here shows a placeholder value rather
than a live one. That is deliberate, not an oversight: the roadmap's next
step wires the microphone/brain/TTS in one at a time, each tested before the
next, and doing that against a screen that already renders correctly is a
much smaller change than building the screen and the wiring at once. The
step after that adds the state machine, real system monitoring, tasks, quick
actions and settings/debug mode -- in that order, never all together.

Every widget that something will eventually read or write from outside this
file has a stable `objectName()` set on it (see `_NAMED_WIDGETS` and the
`widget()` helper below), so later steps -- and this file's own smoke test --
can find it without reaching into private attributes or rebuilding the
layout to get a handle on something.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
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


class MainWindow(QMainWindow):
    """Jarvis's main window. Construction only -- no timers, no threads, no
    backend calls. `gui_main.py` is the only thing that instantiates this."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Jarvis")
        self.resize(900, 600)

        self._build_menu_bar()
        self._build_central_widget()
        self._build_status_bar()

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
        # Empty by design (see module docstring) -- step 4 is what appends a
        # line here per turn, read back from listener.py/brain.py, not this
        # file inventing placeholder conversation text.
        layout.addWidget(transcript_list)

        return panel

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
        bar.showMessage("Jarvis — κέλυφος (Phase 6, βήμα 3: χωρίς λογική ακόμα)")
        self.setStatusBar(bar)


def widget(window: MainWindow, object_name: str) -> QWidget:
    """Look up a named widget by the constants above, for callers (and the
    smoke test) that need a handle without reaching into private attributes."""
    found = window.findChild(QWidget, object_name)
    if found is None:
        raise LookupError(f"no widget named {object_name!r}")
    return found
