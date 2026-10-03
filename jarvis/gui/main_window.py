"""The Phase 6 shell. Step 3 built the empty states ("Build a functional
shell first"); step 4 wired in the microphone, the brain's reply and
`speaker.speak()`/TTS, one at a time, each hand-tested before the next. Step
5 -- "the state machine, real system monitoring, tasks, quick actions and
settings/debug mode" -- is now underway: the state machine (`State`, below)
is one explicit value for what Jarvis is doing right now, read by everything
that used to ask three different thread attributes whether they were
`None`; real system monitoring (`_poll_system()`, below) fills the CPU/RAM
labels from `psutil` on a timer instead of leaving them at "—" forever.

Every widget that something will eventually read or write from outside this
file has a stable `objectName()` set on it (see `_NAMED_WIDGETS` and the
`widget()` helper below), so later steps -- and this file's own smoke test --
can find it without reaching into private attributes or rebuilding the
layout to get a handle on something.

**The microphone, the brain and the voice each run on their own `QThread`,
never on the UI thread.** `listener.listen()` blocks on ffmpeg + Whisper,
`brain.ask()` blocks on a local model or a network round trip, and
`speaker.speak()` blocks for as long as the reply takes to play -- any one
of them freezes the window (no repaint, no click, the OS offers to kill it)
if run inline. `_ListenWorker`, `_BrainWorker` and `_SpeakWorker` each run
one blocking call on its own thread and report back over a signal, which Qt
marshals onto the UI thread automatically -- the one safe way to touch a
widget from work that started elsewhere. `MainWindow` never calls
`listener.listen()`, `_get_reply()` or `speaker.speak()` directly for that
reason.
"""

from __future__ import annotations

from enum import Enum, auto

import psutil
from PySide6.QtCore import QThread, QTimer, Qt, Signal
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

from jarvis import brain, listener, memory, policy, skills, speaker
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

# How often _poll_system() refreshes the CPU/RAM labels. 2s is frequent
# enough to look live without polling psutil hard enough to show up in its
# own reading -- cpu_percent(interval=None) is a near-free syscall-level
# read, not a busy-wait, so this could be much shorter, but a system monitor
# updating faster than a person reads it has nothing to show for the extra
# polling.
MONITOR_POLL_MS = 2000


class State(Enum):
    """What Jarvis is doing right now -- the one thing `_listen_thread`,
    `_brain_thread` and `_speak_thread` were each separately standing in for
    (every guard and every status-label update used to ask "is this
    particular thread attribute `None`?", three times over, instead of
    asking one question once). Exposed at module level, not nested in
    `MainWindow`, because a later step (debug mode's own status surface,
    most likely) and this file's own smoke test both need to name it
    without an instance in hand.

    IDLE is the only state a second click is allowed to start a turn from;
    every other state means a worker is already running, and
    `_start_listening()`'s guard is just "state != IDLE" now, not three
    separate `is not None` checks."""

    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()


# One status line and one record-button-enabled bit per state -- the two
# things every transition used to set by hand, in the same order, at every
# call site. _set_state() below is the one place that reads this table now.
_STATUS_TEXT = {
    State.IDLE: "Κατάσταση: Αδρανές",
    State.LISTENING: "Κατάσταση: Ακούω...",
    State.THINKING: "Κατάσταση: Σκέφτεται...",
    State.SPEAKING: "Κατάσταση: Μιλάει...",
}

# Same text as main.py's own BRAIN_ERROR_REPLY. Duplicated rather than
# imported -- jarvis/gui/ is never imported by main.py and the reverse is
# true here too (see "The GUI shell" in CLAUDE.md), so the two entry points
# each own their copy of the one spoken line a failed brain call falls back
# to, the same way each provider function in brain.py owns its own error
# text rather than reaching across providers for one.
_BRAIN_ERROR_REPLY = "Συγγνώμη, δεν μπορώ να απαντήσω αυτή τη στιγμή."


def _get_reply(text: str) -> str:
    """The reply-generating half of main._answer(), re-derived rather than
    imported (same reason as _BRAIN_ERROR_REPLY above): a skill first, then
    the brain when none matched. Returns the reply text instead of speaking
    it -- speaker.speak()/TTS is step 4 part 3, not this one, so the caller
    decides what to do with the string.

    The kill switch is already checked inside skills.handle() itself
    (policy.intercept(), before the registry loop) -- the is_frozen() check
    here is the same backstop main._answer() keeps for a skill that somehow
    returned None while frozen, not the primary guard.
    """
    reply = skills.handle(text)
    if reply is not None:
        return reply

    if policy.is_frozen():
        return ""

    policy.record("brain", "allowed", "no_skill_matched")
    try:
        # recall_safe() never raises: a broken or locked database means no
        # memory this turn, not a failed reply.
        return brain.ask(text, memory.recall_safe(text))
    except Exception:
        # brain.ask() has already dropped this turn from its own history, so
        # the next question starts clean rather than trailing an unanswered
        # one -- same contract main._whole_reply() relies on.
        return _BRAIN_ERROR_REPLY


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


class _BrainWorker(QThread):
    """Runs _get_reply() off the UI thread, for the same reason
    _ListenWorker exists: brain.ask() can block for a network round trip or
    a local model generating, and either one on the UI thread freezes the
    window."""

    finished_with_reply = Signal(str)

    def __init__(self, text: str, parent: "MainWindow") -> None:
        super().__init__(parent)
        self._text = text

    def run(self) -> None:
        self.finished_with_reply.emit(_get_reply(self._text))


class _SpeakWorker(QThread):
    """Runs speaker.speak() off the UI thread, for the same reason the other
    two workers exist: speak() blocks for the full length of the reply (Edge
    TTS's network round trip plus however long the audio takes to play), and
    that on the UI thread freezes the window for the same duration.

    Carries no payload back -- speak() returns nothing and raises nothing a
    caller here needs to react to (its own Edge/Piper fallback is handled
    inside speaker.py, same as the CLI). `finished_speaking` exists only to
    tell the UI thread "the turn is over now", the same role Qt's own
    QThread.finished could play, but a dedicated signal keeps this worker's
    contract explicit and matches the other two workers' shape."""

    finished_speaking = Signal()

    def __init__(self, text: str, parent: "MainWindow") -> None:
        super().__init__(parent)
        self._text = text

    def run(self) -> None:
        speaker.speak(self._text)
        self.finished_speaking.emit()


class MainWindow(QMainWindow):
    """Jarvis's main window. Construction starts no *worker* threads --
    `_listen_thread`/`_brain_thread`/`_speak_thread` are only ever created in
    response to a click on the record button and to a turn progressing,
    never at construction. It does start one `QTimer` now (`_monitor_timer`,
    for the CPU/RAM labels) -- a repeating UI-thread timer calling a
    non-blocking `psutil` read is nothing like a worker thread blocking on
    ffmpeg/Whisper/a model/TTS, so it doesn't need the same "not until
    asked" discipline those three do. `gui_main.py` is the only thing that
    instantiates this."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Jarvis")
        self.resize(900, 600)

        # Hold the in-flight worker, if any -- None the rest of the time.
        # Kept as an attribute rather than a local so the thread object isn't
        # garbage-collected out from under itself while it's running, and so
        # closeEvent() has something to wait() on. _state (below) is what
        # decides whether a turn is in flight now -- these three are purely
        # "which worker, if I need to wait for or start one", never read as
        # a None-check anywhere outside closeEvent().
        self._listen_thread: _ListenWorker | None = None
        self._brain_thread: _BrainWorker | None = None
        self._speak_thread: _SpeakWorker | None = None
        self._state = State.IDLE

        self._build_menu_bar()
        self._build_central_widget()
        self._build_status_bar()

        # psutil.cpu_percent()'s first-ever call in a process measures usage
        # since the process started, which is not a meaningful snapshot --
        # the docs say to throw it away. Priming it here, once, at
        # construction is what makes the first real _poll_system() tick
        # (after one MONITOR_POLL_MS) measure since *this* moment instead.
        psutil.cpu_percent(interval=None)

        self._monitor_timer = QTimer(self)
        self._monitor_timer.timeout.connect(self._poll_system)
        self._monitor_timer.start(MONITOR_POLL_MS)

    def closeEvent(self, event) -> None:  # noqa: N802 -- Qt's own name
        # A QThread still running when its Python wrapper is destroyed
        # prints a Qt warning and can crash on some platforms. Waiting here
        # blocks the window on close for at most as long as a turn is
        # already bounded to (MAX_RECORD_SECONDS, plus however long the
        # brain takes) -- not ideal, but finite, and simpler than teaching
        # listener.listen()/brain.ask() to be cancellable before anything in
        # the GUI actually needs that.
        if self._listen_thread is not None:
            self._listen_thread.wait()
        if self._brain_thread is not None:
            self._brain_thread.wait()
        if self._speak_thread is not None:
            self._speak_thread.wait()
        self._monitor_timer.stop()
        super().closeEvent(event)

    def _poll_system(self) -> None:
        """Fills in the CPU/RAM labels with a real reading. Called on
        `_monitor_timer` (every `MONITOR_POLL_MS`) and directly by the test
        suite -- both calls happen on the UI thread, since psutil's reads
        here are a near-instant syscall-level read with no disk or network
        I/O behind them, unlike `listener.listen()`/`brain.ask()`/
        `speaker.speak()` above, which is exactly why this doesn't need a
        worker thread the way those three do."""
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        widget(self, CPU_LABEL).setText(f"CPU: {cpu:.0f}%")
        widget(self, RAM_LABEL).setText(f"RAM: {ram:.0f}%")

    def _set_state(self, state: State) -> None:
        """The one place that updates the status label and the record
        button together. Every transition below calls this instead of
        touching either widget directly, so the two can never drift apart
        (a status label reading "Σκέφτεται..." with the button somehow
        still enabled, say) the way three separate call sites eventually
        would."""
        self._state = state
        widget(self, STATUS_LABEL).setText(_STATUS_TEXT[state])
        widget(self, RECORD_BUTTON).setEnabled(state is State.IDLE)

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

        # The status strip. Its text and the record button's enabled state
        # are both driven from one place now -- _set_state() -- so "Αδρανές"
        # here is only the one-time construction value; every transition
        # afterwards goes through _set_state(), never setText() directly.
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

    # --- Microphone + brain + voice (Phase 6 step 4, all three of 3) --------

    def _start_listening(self) -> None:
        # Guards against a second click starting a second turn while one is
        # already in flight (recording, thinking OR speaking) -- none of
        # listener.listen(), an overlapping brain call or speaker.speak() is
        # meant to run concurrently with another, and the button is disabled
        # for the same reason, so this is a backstop for anything that can
        # still reach here (e.g. a held Enter key) rather than the only guard.
        if self._state is not State.IDLE:
            return

        self._set_state(State.LISTENING)

        self._listen_thread = _ListenWorker(self)
        self._listen_thread.finished_with_text.connect(self._on_listen_finished)
        self._listen_thread.start()

    def _on_listen_finished(self, text: object) -> None:
        # listener.listen() returns str | None; Signal(object) is what
        # carries that union across the thread boundary, since Qt's typed
        # signals need one concrete type. text is re-narrowed here, not at
        # the signal, for exactly that reason.
        self._listen_thread = None

        if isinstance(text, str) and text:
            widget(self, TRANSCRIPT_LIST).addItem(f"Εσύ: {text}")
            # The record button stays disabled (State.THINKING isn't IDLE)
            # until the reply comes back -- a turn isn't over just because
            # the recording half is.
            self._set_state(State.THINKING)
            self._brain_thread = _BrainWorker(text, self)
            self._brain_thread.finished_with_reply.connect(self._on_reply_finished)
            self._brain_thread.start()
            return

        # No transcript, same as main.py's own "if not text: continue" --
        # silence, a device failure, or nothing said is not an error here,
        # just a turn with nothing to show, and nothing to send to the brain.
        self._set_state(State.IDLE)

    def _on_reply_finished(self, reply: str) -> None:
        # _get_reply() returns "" for the frozen backstop (see its
        # docstring) -- "say nothing at all", the same case main.py's
        # Answer(spoke=False) exists for. Nothing to show and nothing to
        # speak, same as a silent recording above.
        self._brain_thread = None

        if not reply:
            self._set_state(State.IDLE)
            return

        widget(self, TRANSCRIPT_LIST).addItem(f"Jarvis: {reply}")
        # The transcript line appears now, before speech starts -- speak()
        # can take a second or more just to synthesize, and there is no
        # reason to make the user wait to see text that is already decided.
        # The button stays disabled (State.SPEAKING isn't IDLE); the turn
        # is not over until speaking is too.
        self._set_state(State.SPEAKING)

        self._speak_thread = _SpeakWorker(reply, self)
        self._speak_thread.finished_speaking.connect(self._on_speak_finished)
        self._speak_thread.start()

    def _on_speak_finished(self) -> None:
        self._speak_thread = None
        self._set_state(State.IDLE)

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

        # "—" is the construction-time placeholder only; _poll_system()
        # (the real-system-monitoring piece of step 5) overwrites both
        # labels with an actual percentage on the first timer tick, so "—"
        # is only ever seen for one MONITOR_POLL_MS at startup.
        return box

    # --- Status bar -----------------------------------------------------------

    def _build_status_bar(self) -> None:
        bar = QStatusBar()
        bar.showMessage(
            "Jarvis — Phase 6, βήμα 5: state machine + παρακολούθηση συστήματος"
        )
        self.setStatusBar(bar)


def widget(window: MainWindow, object_name: str) -> QWidget:
    """Look up a named widget by the constants above, for callers (and the
    smoke test) that need a handle without reaching into private attributes."""
    found = window.findChild(QWidget, object_name)
    if found is None:
        raise LookupError(f"no widget named {object_name!r}")
    return found
