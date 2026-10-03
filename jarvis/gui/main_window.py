"""The Phase 6 shell, redesigned (2026-10-03) to match a reference mockup the
user supplied: a dark, "premium" dashboard -- a sidebar of pages, a header
with a live clock and a state pill, and a big glowing central avatar on the
Home page instead of a plain status label. This redesign changes layout and
styling only; every piece of actual behaviour built across step 4 and step 5
(the worker threads, the state machine, system monitoring, the tasks list,
quick actions, the settings values, the debug audit line) is unchanged --
see "The GUI shell" in CLAUDE.md for what each of those does and why.

**The central avatar is an original design, not Iron Man's helmet.** The
reference image used Marvel's Iron Man face as its avatar; that's a
recognizable, copyrighted character, so `_Orb` below draws its own thing
instead -- concentric glowing rings and a row of waveform bars, in the same
blue, sci-fi register, but nobody's intellectual property.

Every widget that something will eventually read or write from outside this
file has a stable `objectName()` (see the constants below and the `widget()`
helper), so later steps -- and this file's own tests -- can find it without
reaching into private attributes.

**The microphone, the brain and the voice each still run on their own
`QThread`, never on the UI thread.** `listener.listen()` blocks on ffmpeg +
Whisper, `brain.ask()` blocks on a local model or a network round trip, and
`speaker.speak()` blocks for as long as the reply takes to play -- any one of
them freezes the window if run inline. `_ListenWorker`, `_BrainWorker` and
`_SpeakWorker` each run one blocking call on its own thread and report back
over a signal, which Qt marshals onto the UI thread automatically.
`MainWindow` never calls `listener.listen()`, `_get_reply()` or
`speaker.speak()` directly for that reason.
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from enum import Enum, auto

import psutil
from PySide6.QtCore import (
    QEasingCurve,
    QRectF,
    QThread,
    QTimer,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QPainter, QRadialGradient
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from jarvis import brain, config, db, listener, memory, policy, skills, speaker
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
VRAM_LABEL = "vram_label"
NETWORK_LABEL = "network_label"
CURRENT_TASK_LABEL = "current_task_label"
DEBUG_ACTION = "debug_mode_action"
RECORD_BUTTON = "record_button"
ORB = "orb_widget"
NAV_HOME = "nav_home"
NAV_CHAT = "nav_chat"
NAV_TASKS = "nav_tasks"
NAV_FILES = "nav_files"
NAV_SETTINGS = "nav_settings"
CLOCK_LABEL = "clock_label"
DATE_LABEL = "date_label"
STATUS_DOT = "status_dot"

# How often _poll_system() refreshes the CPU/RAM/VRAM/network labels. 2s is
# frequent enough to look live without polling psutil hard enough to show up
# in its own reading -- cpu_percent(interval=None) is a near-free
# syscall-level read, not a busy-wait, so this could be much shorter, but a
# system monitor updating faster than a person reads it has nothing to show
# for the extra polling.
MONITOR_POLL_MS = 2000

# The orb's own redraw tick -- separate from MONITOR_POLL_MS because a
# visual pulse/waveform needs to look smooth (a handful of frames a second),
# while a CPU percentage updating that often would just be noise nobody can
# read. Purely decorative: the bars move whenever the state isn't IDLE, but
# their heights are not derived from any real microphone or speaker level --
# wiring that up would mean reaching into listener.py/speaker.py's internals
# for a number this redesign doesn't otherwise need, so it's left as a known
# simplification rather than invented data presented as real.
ORB_TICK_MS = 60

# Cached across calls to _read_vram_percent() -- nvmlInit() and the device
# handle only need doing once per process, and a machine with no NVIDIA
# driver should only pay for one failed attempt, not one every
# MONITOR_POLL_MS. Module-level rather than an attribute on MainWindow for
# the same reason speaker._get_voice()'s lazy singleton is module-level: the
# GPU is a property of the machine, not of any one window.
_nvml_handle = None
_nvml_unavailable = False


def _read_vram_percent() -> float | None:
    """Returns VRAM used as a percentage of total on the first NVIDIA GPU,
    or None if there isn't one -- no card, no driver, or the `pynvml`
    bindings (package `nvidia-ml-py`) aren't installed. Imported lazily,
    inside the function, the same idiom as speaker._get_voice()'s Piper
    import: a machine with no NVIDIA GPU (every environment this project's
    test suite runs in, included) never needs this module loaded at all.

    Only ever tried past the first failure if that failure hasn't been seen
    yet -- `_nvml_unavailable` latches to True on the first exception
    (ImportError, no driver, no device) and every later call then skips
    straight to None instead of repeating a failing nvmlInit() on every
    timer tick. Same discipline as diag.py's write-failure flag: a missing
    GPU costs one failed call per process, not one per poll."""
    global _nvml_handle, _nvml_unavailable
    if _nvml_unavailable:
        return None
    try:
        import pynvml

        if _nvml_handle is None:
            pynvml.nvmlInit()
            _nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        info = pynvml.nvmlDeviceGetMemoryInfo(_nvml_handle)
        return info.used / info.total * 100
    except Exception:
        _nvml_unavailable = True
        return None


def _format_rate(bytes_per_second: float) -> str:
    """"1.2 MB/s"-style formatting for the network throughput reading --
    kept to two units (KB/s, MB/s) since this is a glance-at number, not a
    precise one, and anything under 1 KB/s is rendered as "0.0 KB/s" rather
    than switching to bytes, which would be more digits for less meaning."""
    kb = bytes_per_second / 1024
    if kb < 1024:
        return f"{kb:.1f} KB/s"
    return f"{kb / 1024:.1f} MB/s"


def _panel_title(text: str) -> QLabel:
    """A small, letter-spaced, uppercase label for a panel's own heading
    (System Status, Quick Actions, Current Task), visually distinct from the
    plain body labels inside the panel -- styled via the `panelTitle` object
    name in `_STYLESHEET` rather than repeating font/colour calls at every
    call site that builds one of these panels. Upper-cased here rather than
    relying on a stylesheet `text-transform` -- Qt's QSS subset doesn't
    support that CSS property, so the caller writes the label in natural
    case and this is where it becomes the small-caps look."""
    label = QLabel(text.upper())
    label.setObjectName("panelTitle")
    return label


def _page_title(text: str) -> QLabel:
    """A page's own headline label (Συνομιλία, Εργασίες, ...), styled via
    the `pageTitle` object name -- larger and heavier than body text, so a
    page reads as having a real header rather than the same plain QLabel
    every other piece of text on it uses. No underline or rule beneath it
    (the first pass drew one in the accent colour): a page title earns its
    weight from size and spacing alone, the same way a native macOS/iOS
    screen title does -- a coloured rule under every heading in the window
    is exactly the kind of "exists because it looks cool" detail the second
    design pass asks to remove."""
    label = QLabel(text)
    label.setObjectName("pageTitle")
    return label


def _render_agenda_item(kind: str, text: str) -> str:
    """Same phrasing `skills._handle_agenda` already speaks for «τι έχω
    σήμερα» -- an exam prefixed "εξέταση", a class "μάθημα", a reminder
    shown as its own text (its fired/missed suffix, if any, is already
    baked into `text` by `memory.agenda()` itself). Not imported from
    skills.py: that rendering lives inline inside an f-string there, not a
    function of its own, so this is a second copy kept in step by hand
    rather than a shared one -- small enough that duplicating it costs far
    less than coupling the GUI's tasks list to skills.py's private
    internals would."""
    if kind == "exam":
        return f"εξέταση {text}"
    if kind == "class":
        return f"μάθημα {text}"
    return text


class State(Enum):
    """What Jarvis is doing right now -- the one thing `_listen_thread`,
    `_brain_thread` and `_speak_thread` were each separately standing in for.
    Exposed at module level, not nested in `MainWindow`, since the orb widget
    and this file's own tests both need to name it without an instance in
    hand.

    IDLE is the only state a second click is allowed to start a turn from;
    every other state means a worker is already running, and
    `_start_listening()`'s guard is just "state != IDLE" now, not three
    separate `is not None` checks."""

    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()


# One status line, one orb colour and one record-button-enabled bit per
# state -- the things every transition used to set by hand, in the same
# order, at every call site. _set_state() below is the one place that reads
# these tables now.
_STATUS_TEXT = {
    State.IDLE: "Κατάσταση: Αδρανές",
    State.LISTENING: "Κατάσταση: Ακούω...",
    State.THINKING: "Κατάσταση: Σκέφτεται...",
    State.SPEAKING: "Κατάσταση: Μιλάει...",
}

# One accent per state, restrained rather than neon -- idle a quiet grey-blue
# (barely a colour at all; the orb should look almost off), listening/
# speaking the single accent blue a click would also use, thinking a muted
# amber. Three colour families, not four: listening and speaking share one,
# since both are "doing the thing a mic/speaker icon would show" and a
# fourth hue would just be more to visually parse for no new information.
_ORB_COLOR = {
    State.IDLE: QColor(90, 100, 120),
    State.LISTENING: QColor(10, 132, 255),
    State.THINKING: QColor(210, 150, 60),
    State.SPEAKING: QColor(10, 132, 255),
}

# Same per-state colour, as a hex string, for the small status dot in the
# header (see _build_header()) -- a plain QLabel can't take a QColor
# directly in a stylesheet string, so this is the same table in the other
# format rather than converting _ORB_COLOR at every _set_state() call.
_STATUS_DOT_COLOR = {
    State.IDLE: "#5a6478",
    State.LISTENING: "#0a84ff",
    State.THINKING: "#d2963c",
    State.SPEAKING: "#0a84ff",
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
    it -- speaker.speak()/TTS is its own worker, not this function, so the
    caller decides what to do with the string.

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
    tell the UI thread "the turn is over now"."""

    finished_speaking = Signal()

    def __init__(self, text: str, parent: "MainWindow") -> None:
        super().__init__(parent)
        self._text = text

    def run(self) -> None:
        speaker.speak(self._text)
        self.finished_speaking.emit()


class _Orb(QWidget):
    """The central avatar -- an original design (a soft glow, a single ring,
    a handful of waveform bars), deliberately not a recreation of the
    reference mockup's Iron Man face, which is a copyrighted character.

    Restrained on purpose, per the second design pass: the first version's
    double ring and nine fast-wobbling bars read as a "sci-fi HUD", busy even
    at idle. This one sits almost still until a state actually calls for
    motion -- one ring, five bars, a slower idle breath -- so attention goes
    to the handful of pixels that are actually telling you something,
    matching the orb's own job: the single place in the window that answers
    "what is Jarvis doing right now" at a glance.

    Colour changes are *animated*, not snapped -- `set_state()` starts a
    ~280ms QVariantAnimation from the orb's current displayed colour to the
    new state's, eased out, so a state change reads as a deliberate
    transition rather than a flicker. "Smooth, physical, intentional" is the
    brief; an instant colour swap is none of those."""

    _COLOR_TRANSITION_MS = 280

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(200, 200)
        self._state = State.IDLE
        self._phase = 0.0
        self._color = QColor(_ORB_COLOR[State.IDLE])
        self._color_anim: QVariantAnimation | None = None

    def set_state(self, state: State) -> None:
        self._state = state
        target = _ORB_COLOR[state]
        if target == self._color:
            return
        anim = QVariantAnimation(self)
        anim.setStartValue(QColor(self._color))
        anim.setEndValue(QColor(target))
        anim.setDuration(self._COLOR_TRANSITION_MS)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(self._apply_color)
        anim.start()
        # Kept as an attribute, not a local, so Python doesn't garbage
        # collect it mid-flight -- the same reason MainWindow holds its
        # worker threads as attributes rather than locals.
        self._color_anim = anim

    def _apply_color(self, value: QColor) -> None:
        self._color = value
        self.update()

    def tick(self) -> None:
        self._phase += 1.0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 -- Qt's own name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        side = min(self.width(), self.height())
        cx, cy = self.width() / 2, self.height() / 2
        color = self._color

        # A slow breathing pulse on the glow radius -- idle breathes gently
        # and slowly; an active state breathes a little faster, but nothing
        # here is meant to be eye-catching on its own. "Almost still when
        # idle, alive only when something is actually happening" is the
        # brief, so even an active state's pulse stays subtle.
        speed = 0.02 if self._state is State.IDLE else 0.045
        pulse = (math.sin(self._phase * speed) + 1) / 2  # 0..1

        glow_radius = side * (0.40 + 0.03 * pulse)
        gradient = QRadialGradient(cx, cy, glow_radius)
        glow = QColor(color)
        glow.setAlpha(60)
        gradient.setColorAt(0.0, glow)
        transparent = QColor(color)
        transparent.setAlpha(0)
        gradient.setColorAt(1.0, transparent)
        painter.setBrush(gradient)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(
            QRectF(cx - glow_radius, cy - glow_radius, glow_radius * 2, glow_radius * 2)
        )

        # A single ring -- a "core" the glow sits around, without the
        # arc-reactor double-ring the first pass drew.
        ring_radius = side * 0.27
        ring = QColor(color)
        ring.setAlpha(190)
        painter.setPen(ring)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(
            QRectF(cx - ring_radius, cy - ring_radius, ring_radius * 2, ring_radius * 2)
        )

        # Five bars, not nine -- fewer, calmer, each a touch wider. Idle
        # shows them as short flat dashes (a resting state, not "nothing is
        # here"); an active state wobbles them, still gently.
        bar_count = 5
        bar_area_width = side * 0.32
        bar_gap = bar_area_width / bar_count
        base_y = cy
        for i in range(bar_count):
            if self._state is State.IDLE:
                height = side * 0.025
            else:
                # A deterministic pseudo-wave from the phase and the bar's
                # own index -- decorative, not a real audio level (see
                # ORB_TICK_MS above).
                wobble = (math.sin(self._phase * 0.18 + i * 1.1) + 1) / 2
                height = side * (0.03 + 0.08 * wobble)
            x = cx - bar_area_width / 2 + i * bar_gap
            bar_color = QColor(color)
            bar_color.setAlpha(210)
            painter.setBrush(bar_color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(
                QRectF(x, base_y - height / 2, bar_gap * 0.45, height), 2, 2
            )


class MainWindow(QMainWindow):
    """Jarvis's main window. Construction starts no *worker* threads --
    `_listen_thread`/`_brain_thread`/`_speak_thread` are only ever created in
    response to a click on the record button and to a turn progressing,
    never at construction. It does start two repeating `QTimer`s now
    (`_monitor_timer` for CPU/RAM/VRAM/network, `_orb_timer` for the orb's
    animation) -- both non-blocking UI-thread work, nothing like a worker
    thread blocking on ffmpeg/Whisper/a model/TTS, so neither needs the
    "not until asked" discipline those three do. `gui_main.py` is the only
    thing that instantiates this."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Jarvis")
        self.resize(1200, 760)
        self.setStyleSheet(_STYLESHEET)

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
        self._last_net_bytes: int | None = None
        self._last_net_time: float | None = None

        self._build_menu_bar()
        self._build_central_widget()

        # psutil.cpu_percent()'s first-ever call in a process measures usage
        # since the process started, which is not a meaningful snapshot --
        # the docs say to throw it away. Priming it here, once, at
        # construction is what makes the first real _poll_system() tick
        # (after one MONITOR_POLL_MS) measure since *this* moment instead.
        psutil.cpu_percent(interval=None)

        self._monitor_timer = QTimer(self)
        self._monitor_timer.timeout.connect(self._poll_system)
        self._monitor_timer.start(MONITOR_POLL_MS)

        self._orb_timer = QTimer(self)
        self._orb_timer.timeout.connect(self._orb.tick)
        self._orb_timer.start(ORB_TICK_MS)

        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start(1000)
        self._update_clock()

        # Initial fill of the tasks list -- same data _poll_system() above
        # fills CPU/RAM/VRAM with: a read that happens once here so the
        # panel isn't empty for one more startup heartbeat than it needs to
        # be, then refreshed again after every completed turn (see
        # _on_speak_finished()), since a turn can itself have just saved
        # the exam/reminder/class that would change what "today" holds.
        self._load_tasks()

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
        self._orb_timer.stop()
        self._clock_timer.stop()
        super().closeEvent(event)

    def _update_clock(self) -> None:
        now = datetime.now()
        widget(self, CLOCK_LABEL).setText(now.strftime("%H:%M"))
        widget(self, DATE_LABEL).setText(now.strftime("%a %d %b %Y"))

    def _poll_system(self) -> None:
        """Fills in the CPU/RAM/VRAM/network labels with a real reading.
        Called on `_monitor_timer` (every `MONITOR_POLL_MS`) and directly by
        the test suite -- both calls happen on the UI thread, since psutil's
        reads here are near-instant syscall-level reads with no disk or
        network I/O behind them, unlike `listener.listen()`/`brain.ask()`/
        `speaker.speak()` above, which is exactly why this doesn't need a
        worker thread the way those three do. The VRAM read
        (`_read_vram_percent()`) is the same kind of near-instant call when
        an NVIDIA GPU is present, and a cached, already-failed no-op
        otherwise -- never a reason to move this to a worker thread either."""
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        widget(self, CPU_LABEL).setText(f"CPU: {cpu:.0f}%")
        widget(self, RAM_LABEL).setText(f"RAM: {ram:.0f}%")

        vram = _read_vram_percent()
        if vram is None:
            widget(self, VRAM_LABEL).setText("VRAM: μη διαθέσιμο")
        else:
            widget(self, VRAM_LABEL).setText(f"VRAM: {vram:.0f}%")

        # Network throughput isn't a single reading the way CPU/RAM/VRAM
        # are -- psutil only gives a running total of bytes moved since boot,
        # so the rate is a delta between this poll and the last one. The
        # first poll in a process has no "last" to diff against, hence the
        # None check: it primes the baseline and shows nothing yet, the same
        # one-tick blind spot psutil.cpu_percent()'s own priming call has.
        counters = psutil.net_io_counters()
        total_bytes = counters.bytes_sent + counters.bytes_recv
        now = time.monotonic()
        if self._last_net_bytes is not None and self._last_net_time is not None:
            elapsed = now - self._last_net_time
            if elapsed > 0:
                rate = (total_bytes - self._last_net_bytes) / elapsed
                widget(self, NETWORK_LABEL).setText(
                    f"Network: {_format_rate(max(rate, 0))}"
                )
        self._last_net_bytes = total_bytes
        self._last_net_time = now

    def _load_tasks(self) -> None:
        """Fills TASKS_LIST with today's agenda -- `memory.agenda()`, the
        same call the `agenda` skill already makes for «τι έχω σήμερα», just
        rendered as a list instead of spoken as a sentence. Also fills the
        Home page's "Current Task" line with the first item, or a flat
        "waiting for a command" line when there isn't one -- the same data,
        just read twice for two different panels.

        Swallows every error, same discipline as `memory.recall_safe()` and
        `_handle_agenda`'s own try/except: a locked or broken database means
        neither panel refreshes this time, never a crashed GUI or a popup
        the user didn't ask for."""
        tasks_list = widget(self, TASKS_LIST)
        tasks_list.clear()
        try:
            conn = db.connect()
            try:
                items = memory.agenda(conn, datetime.now().date())
            finally:
                conn.close()
        except Exception:
            return
        for kind, text in items:
            tasks_list.addItem(_render_agenda_item(kind, text))

        current_task = widget(self, CURRENT_TASK_LABEL)
        if items:
            current_task.setText(_render_agenda_item(*items[0]))
        else:
            current_task.setText("Έτοιμος — περιμένω εντολή.")

    def _set_state(self, state: State) -> None:
        """The one place that updates the status label, the orb and the
        record button together. Every transition below calls this instead
        of touching any of the three directly, so they can never drift
        apart (a status label reading "Σκέφτεται..." with the orb still
        glowing idle-blue, say) the way separate call sites eventually
        would."""
        self._state = state
        widget(self, STATUS_LABEL).setText(_STATUS_TEXT[state])
        widget(self, RECORD_BUTTON).setEnabled(state is State.IDLE)
        self._orb.set_state(state)
        # The dot's colour is per-instance, not per-class, so it's set here
        # directly rather than through _STYLESHEET -- a plain object-name or
        # property selector can't express "whichever colour this state maps
        # to" the way _STATUS_DOT_COLOR already holds it.
        widget(self, STATUS_DOT).setStyleSheet(
            f"background-color: {_STATUS_DOT_COLOR[state]}; border-radius: 3px;"
        )

    # --- Menu bar -----------------------------------------------------------

    def _build_menu_bar(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("&Αρχείο")
        exit_action = QAction("Έξοδος", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        view_menu = menu_bar.addMenu("&Προβολή")
        settings_action = QAction("Ρυθμίσεις", self)
        settings_action.triggered.connect(lambda: self._go_to_page(NAV_SETTINGS))
        view_menu.addAction(settings_action)

    # --- Central widget: header + sidebar + pages ----------------------------

    def _build_central_widget(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)

        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        outer.addLayout(body, stretch=1)

        body.addWidget(self._build_sidebar())

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_home_page())
        self._stack.addWidget(self._build_chat_page())
        self._stack.addWidget(self._build_tasks_page())
        self._stack.addWidget(self._build_files_page())
        self._stack.addWidget(self._build_settings_page())
        body.addWidget(self._stack, stretch=1)

    def _build_header(self) -> QWidget:
        """One row: the wordmark, a status indicator, the clock. The first
        design pass also put a tagline ("Always here. Ready.") under the
        wordmark -- removed here, since it communicated nothing the window
        title bar doesn't already say and existed only to fill space under
        the logo. Less UI, not more, per the second pass's own brief."""
        header = QFrame()
        header.setObjectName("header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 0, 24, 0)

        title = QLabel("Jarvis")
        title.setObjectName("brandTitle")
        layout.addWidget(title)
        layout.addStretch(1)

        # A small colour-coded dot plus plain text, instead of the first
        # pass's filled "pill" -- the same information (what state Jarvis is
        # in) with less visual weight. The dot's colour is set directly in
        # _set_state() (see STATUS_DOT_COLOR) rather than through the global
        # stylesheet, since it has to change per state, not just on
        # hover/press/disabled the way a QSS pseudo-state can express.
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_dot = QFrame()
        status_dot.setObjectName(STATUS_DOT)
        status_dot.setFixedSize(7, 7)
        status_dot.setStyleSheet(
            f"background-color: {_STATUS_DOT_COLOR[State.IDLE]}; border-radius: 3px;"
        )
        status_row.addWidget(status_dot)
        status_label = QLabel(_STATUS_TEXT[State.IDLE])
        status_label.setObjectName(STATUS_LABEL)
        status_row.addWidget(status_label)
        layout.addLayout(status_row)
        layout.addStretch(1)

        clock_box = QVBoxLayout()
        clock_box.setSpacing(0)
        clock_label = QLabel("--:--")
        clock_label.setObjectName(CLOCK_LABEL)
        clock_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        date_label = QLabel("")
        date_label.setObjectName(DATE_LABEL)
        date_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        clock_box.addWidget(clock_label)
        clock_box.addWidget(date_label)
        layout.addLayout(clock_box)

        return header

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        layout = QVBoxLayout(sidebar)

        nav_items = [
            (NAV_HOME, "Home", 0),
            (NAV_CHAT, "Chat", 1),
            (NAV_TASKS, "Tasks", 2),
            (NAV_FILES, "Files", 3),
            (NAV_SETTINGS, "Settings", 4),
        ]
        self._nav_buttons: dict[str, QPushButton] = {}
        for object_name, label, index in nav_items:
            button = QPushButton(label)
            button.setObjectName(object_name)
            button.setCheckable(True)
            button.setProperty("navButton", True)
            button.clicked.connect(
                lambda _checked=False, i=index, name=object_name: self._go_to_page(
                    name, index=i
                )
            )
            layout.addWidget(button)
            self._nav_buttons[object_name] = button
        self._nav_buttons[NAV_HOME].setChecked(True)

        layout.addStretch(1)
        return sidebar

    def _go_to_page(self, object_name: str, index: int | None = None) -> None:
        """Switches the stacked widget to the named page and keeps exactly
        one sidebar button checked -- Qt doesn't auto-exclude checkable
        buttons that aren't in a QButtonGroup, so this does it by hand."""
        order = [NAV_HOME, NAV_CHAT, NAV_TASKS, NAV_FILES, NAV_SETTINGS]
        if index is None:
            index = order.index(object_name)
        self._stack.setCurrentIndex(index)
        for name, button in self._nav_buttons.items():
            button.setChecked(name == object_name)

    # --- Home page: the orb, quick actions, system status, current task -----

    def _build_home_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        center = QVBoxLayout()
        center.setSpacing(28)
        center.addStretch(1)

        self._orb = _Orb()
        center.addWidget(self._orb, alignment=Qt.AlignmentFlag.AlignCenter)
        # No caption or quote under the orb -- the first pass had a
        # decorative quote here that named nothing and did nothing; the
        # second pass's brief is explicit that an element earns its place by
        # communicating information, enabling interaction, or giving
        # feedback, not by filling empty space. The orb's own state (colour,
        # motion) and the status row in the header already say what Jarvis
        # is doing; a quote said nothing further.

        record_button = QPushButton("Εγγραφή")
        record_button.setObjectName(RECORD_BUTTON)
        record_button.clicked.connect(self._start_listening)
        center.addWidget(record_button, alignment=Qt.AlignmentFlag.AlignCenter)
        center.addStretch(1)

        layout.addLayout(center, stretch=3)
        layout.addWidget(self._build_home_sidebar(), stretch=1)

        return page

    def _build_home_sidebar(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 24, 24, 24)
        layout.setSpacing(16)

        layout.addWidget(self._build_monitoring_box())
        layout.addWidget(self._build_quick_actions_box())
        layout.addWidget(self._build_current_task_box())
        layout.addStretch(1)

        return panel

    def _build_monitoring_box(self) -> QWidget:
        box = QFrame()
        box.setObjectName("panel")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        layout.addWidget(_panel_title("System Status"))

        for object_name, placeholder in (
            (CPU_LABEL, "CPU: —"),
            (RAM_LABEL, "RAM: —"),
            (VRAM_LABEL, "VRAM: —"),
            (NETWORK_LABEL, "Network: —"),
        ):
            label = QLabel(placeholder)
            label.setObjectName(object_name)
            label.setProperty("metric", True)
            layout.addWidget(label)

        # "—" is the construction-time placeholder only; _poll_system()
        # overwrites all four labels with an actual reading on the first
        # timer tick. VRAM can still land on "μη διαθέσιμο" afterwards
        # rather than a percentage -- that is _read_vram_percent() reporting
        # no NVIDIA GPU found, not a stuck placeholder.
        return box

    def _build_quick_actions_box(self) -> QWidget:
        box = QFrame()
        box.setObjectName("panel")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)
        layout.addWidget(_panel_title("Quick Actions"))

        # One button per site/app Jarvis can already open by voice
        # (config.SKILL_SITES / SKILL_APPS) -- so the panel reflects what's
        # really configured rather than a fixed, separately-maintained list
        # that drifts from it. Each one calls exactly the function the
        # voice skill itself calls (skills._open_site()/_open_app()), via
        # _quick_action_site()/_quick_action_app() below.
        for label, url in SKILL_SITES.items():
            button = QPushButton(label)
            button.setProperty("quickAction", True)
            button.clicked.connect(
                lambda _checked=False, label=label, url=url: self._quick_action_site(
                    label, url
                )
            )
            layout.addWidget(button)

        for label, argv in SKILL_APPS.items():
            button = QPushButton(label)
            button.setProperty("quickAction", True)
            button.clicked.connect(
                lambda _checked=False, label=label, argv=argv: self._quick_action_app(
                    label, argv
                )
            )
            layout.addWidget(button)

        return box

    def _build_current_task_box(self) -> QWidget:
        box = QFrame()
        box.setObjectName("panel")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)
        layout.addWidget(_panel_title("Current Task"))

        label = QLabel("Έτοιμος — περιμένω εντολή.")
        label.setObjectName(CURRENT_TASK_LABEL)
        label.setWordWrap(True)
        layout.addWidget(label)

        return box

    def _quick_action_site(self, label: str, url: str) -> None:
        """A quick-action button's click, for a configured site. Calls the
        exact function the voice skill calls (skills._open_site()) rather
        than a second copy of "how to open a site" -- and reports the result
        in the transcript the same way a spoken «άνοιξε ...» command would,
        so the two feel like the same feature from two different inputs."""
        try:
            skills._open_site(url)
        except Exception:
            widget(self, TRANSCRIPT_LIST).addItem(
                f"Jarvis: Δεν μπόρεσα να ανοίξω το {label}."
            )
            return
        widget(self, TRANSCRIPT_LIST).addItem(f"Jarvis: Άνοιξα το {label}.")

    def _quick_action_app(self, label: str, argv: list) -> None:
        """Same as _quick_action_site(), for a configured local app."""
        try:
            skills._open_app(argv)
        except Exception:
            widget(self, TRANSCRIPT_LIST).addItem(
                f"Jarvis: Δεν μπόρεσα να ανοίξω το {label}."
            )
            return
        widget(self, TRANSCRIPT_LIST).addItem(f"Jarvis: Άνοιξα το {label}.")

    # --- Chat page ------------------------------------------------------------

    def _build_chat_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(_page_title("Συνομιλία"))

        transcript_list = QListWidget()
        transcript_list.setObjectName(TRANSCRIPT_LIST)
        # Starts empty; a real turn appends to it (see _on_listen_finished).
        # Still nothing invented here -- every line it ever holds came back
        # from listener.listen(), not a placeholder this file wrote.
        layout.addWidget(transcript_list)

        return page

    # --- Tasks page -------------------------------------------------------------

    def _build_tasks_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(_page_title("Εργασίες"))

        tasks_list = QListWidget()
        tasks_list.setObjectName(TASKS_LIST)
        # Empty here at construction only -- _load_tasks() (called at the
        # end of __init__, and again after every completed turn) fills it
        # from memory.agenda(), the same courses/exams/reminders data
        # «τι έχω σήμερα» already speaks, rendered as a list instead.
        layout.addWidget(tasks_list)

        return page

    # --- Files page (stub -- Phase 7 builds the real thing) --------------------

    def _build_files_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(_page_title("Αρχεία"))
        placeholder = QLabel(
            "Η διαχείριση αρχείων (αναζήτηση, άνοιγμα, οργάνωση) είναι το "
            "Phase 7 του roadmap -- δεν έχει χτιστεί ακόμα."
        )
        placeholder.setObjectName("mutedText")
        placeholder.setWordWrap(True)
        layout.addWidget(placeholder)
        layout.addStretch(1)
        return page

    # --- Settings page ----------------------------------------------------------

    def _build_settings_page(self) -> QWidget:
        """A read-only view over what jarvis/config.py actually read from
        .env for this run, plus the debug-mode toggle. Read-only is not a
        shortcut taken for lack of time -- it's the only shape consistent
        with the project's own working rule "never edit .env": a field here
        that wrote back to it would mean Claude code editing .env by another
        name. Seeing a wrong value here still tells you to go fix .env by
        hand and restart, which is the whole point of a settings surface
        over a config that's only ever read at startup."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(_page_title("Ρυθμίσεις"))

        form_box = QFrame()
        form_box.setObjectName("panel")
        form_box_layout = QVBoxLayout(form_box)
        form_box_layout.setContentsMargins(18, 16, 18, 16)

        form = QFormLayout()
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(10)
        # Grouped the same way CLAUDE.md's own "Config"/"Providers" sections
        # are: brain, voice, wake word/conversation, logging -- a developer
        # reading this page and reading those docs sees the same shape.
        rows: list[tuple[str, object]] = [
            ("Brain provider", config.BRAIN_PROVIDER),
            (
                "Brain model",
                config.CLAUDE_MODEL
                if config.BRAIN_PROVIDER == "claude"
                else config.OLLAMA_MODEL,
            ),
            ("TTS engine", config.TTS_ENGINE),
            ("TTS voice", config.TTS_VOICE),
            ("Whisper model", config.WHISPER_MODEL),
            ("Wake word", "on" if config.WAKE_WORD_ENABLED else "off"),
            ("Barge-in", "on" if config.BARGE_IN_ENABLED else "off"),
            ("Conversation mode", "on" if config.CONVERSATION_MODE else "off"),
            ("Scheduler", "on" if config.SCHEDULER_ENABLED else "off"),
            ("Diagnostic log", "on" if config.LOG_ENABLED else "off"),
        ]
        for label, value in rows:
            key_label = QLabel(label)
            key_label.setObjectName("settingKey")
            value_label = QLabel(str(value))
            value_label.setObjectName("settingValue")
            form.addRow(key_label, value_label)
        form_box_layout.addLayout(form)
        layout.addWidget(form_box)

        # Checkable, and wired to something real: when checked, every
        # completed turn appends one extra transcript line showing the audit
        # row policy.py already wrote for that turn (which skill matched, or
        # that the brain answered, and why) -- see _append_debug_line(). That
        # reuses the audit log Phase 4 already built rather than inventing a
        # second logging path; nothing is computed here that wasn't already
        # being recorded.
        debug_checkbox = QCheckBox("Λειτουργία αποσφαλμάτωσης")
        debug_checkbox.setObjectName(DEBUG_ACTION)
        layout.addWidget(debug_checkbox)

        layout.addStretch(1)
        return page

    def _debug_mode_enabled(self) -> bool:
        """Reads DEBUG_ACTION's own checked state -- the single source of
        truth for whether debug lines are appended, rather than a second
        flag that could drift from what the settings page shows is
        checked."""
        checkbox = self.findChild(QCheckBox, DEBUG_ACTION)
        return checkbox is not None and checkbox.isChecked()

    def _append_debug_line(self) -> None:
        """Appends one transcript line describing the audit row policy.py
        just wrote for this turn (_get_reply() always writes exactly one,
        either from a matched skill's policy.dispatch() or the explicit
        "brain" row in _get_reply() itself) -- the same information
        `:mem list audit` already shows in the terminal, surfaced here
        instead so the debug toggle means something real.

        Swallows every error, same discipline as _load_tasks(): a locked or
        broken database costs this one debug line, never a crashed GUI."""
        try:
            conn = db.connect()
            try:
                row = conn.execute(
                    "SELECT action, decision, reason FROM audit "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
        except Exception:
            return
        if row is None:
            return
        action, decision, reason = row
        widget(self, TRANSCRIPT_LIST).addItem(
            f"[debug] {action} → {decision} ({reason})"
        )

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
        if self._debug_mode_enabled():
            self._append_debug_line()
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
        # A turn that just finished could itself have saved the exam,
        # reminder or class that changes what today's agenda holds (e.g.
        # "θυμήσου ότι έχω εξέταση σήμερα..."), so the tasks list is
        # refreshed at the one point a turn is fully over, not on its own
        # timer -- there's no reason to poll a database that only changes
        # when a turn changes it.
        self._load_tasks()

    # No status bar. The first pass had one showing a developer-facing
    # "Jarvis — Phase 6 complete" string -- meant nothing to the person using
    # the app, and a window chrome element that exists to report the
    # project's own build status is exactly the "developer dashboard" feel
    # the second design pass asks to move away from. The header's status row
    # (see _build_header()) is where Jarvis's own state actually belongs.


def widget(window: MainWindow, object_name: str) -> QWidget:
    """Look up a named widget by the constants above, for callers (and the
    smoke test) that need a handle without reaching into private attributes."""
    found = window.findChild(QWidget, object_name)
    if found is None:
        raise LookupError(f"no widget named {object_name!r}")
    return found


# A restrained, Apple-influenced palette -- the third version of this
# stylesheet. The first pass was flat and "γραφικό" (plain); the second
# pass's answer to that was gradients, a 3px accent border on every panel
# and a glowing gradient record button -- more decoration, not more design,
# and it read as a gaming/RGB dashboard rather than a calm product. This
# pass instead narrows the palette to a handful of tokens (documented below)
# and spends restraint, not colour, on communicating hierarchy: one accent,
# used sparingly (the record button, the active nav item, the status dot);
# everything else is tone, weight and spacing. No gradients anywhere in this
# version -- every fill is a single flat colour.
#
# Tokens (hand-kept here rather than computed, since Qt's own QSS subset has
# no variables):
#   background        #0a0a0c   the window itself -- near-black, not navy
#   surface            #141417   one elevation up: header, sidebar, panels,
#                                 lists -- a single flat tone, not a gradient
#   surface-raised      #1c1c1f   hover/pressed states one step up again
#   hairline           rgba(255,255,255,0.08)   every border in this sheet
#   text-primary        #f5f5f7   headings, values, anything that matters
#   text-secondary      rgba(245,245,247,0.55)  labels, captions, metadata
#   text-tertiary       rgba(245,245,247,0.32)  placeholders, disabled text
#   accent              #0a84ff   the one accent colour in the whole app
#   accent-soft        rgba(10,132,255,0.14)    accent used as a fill, not text
_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #0a0a0c;
    color: #f5f5f7;
    font-family: "Segoe UI", sans-serif;
    font-size: 13px;
}

/* --- Header ----------------------------------------------------------- */
QFrame#header {
    background-color: #0a0a0c;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    min-height: 56px;
    max-height: 56px;
}
QLabel#brandTitle {
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 0.4px;
    color: #f5f5f7;
}
QLabel#status_label {
    font-size: 12px;
    font-weight: 500;
    color: rgba(245, 245, 247, 0.55);
}
QLabel#clock_label {
    font-size: 13px;
    font-weight: 600;
    color: #f5f5f7;
}
QLabel#date_label {
    color: rgba(245, 245, 247, 0.4);
    font-size: 11px;
}

/* --- Sidebar ------------------------------------------------------------ */
QFrame#sidebar {
    background-color: #0a0a0c;
    border-right: 1px solid rgba(255, 255, 255, 0.08);
    min-width: 168px;
    max-width: 168px;
}
QPushButton[navButton="true"] {
    text-align: left;
    padding: 9px 14px;
    margin: 1px 12px;
    border: none;
    border-radius: 7px;
    background-color: transparent;
    color: rgba(245, 245, 247, 0.5);
    font-weight: 500;
    font-size: 13px;
}
QPushButton[navButton="true"]:checked {
    background-color: rgba(10, 132, 255, 0.14);
    color: #0a84ff;
    font-weight: 600;
}
QPushButton[navButton="true"]:hover:!checked {
    background-color: rgba(255, 255, 255, 0.05);
    color: #f5f5f7;
}

/* --- Panels (System Status / Quick Actions / Current Task / Settings) -- */
QFrame#panel {
    background-color: #141417;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 12px;
}
QLabel#panelTitle {
    color: rgba(245, 245, 247, 0.4);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.6px;
}
QLabel[metric="true"] {
    font-size: 13px;
    font-weight: 500;
    color: #f5f5f7;
    padding: 1px 0;
}
QLabel#pageTitle {
    font-size: 20px;
    font-weight: 600;
    color: #f5f5f7;
}
QLabel#mutedText {
    color: rgba(245, 245, 247, 0.45);
}
QLabel#current_task_label {
    color: #f5f5f7;
    font-size: 13px;
}
QLabel#settingKey {
    color: rgba(245, 245, 247, 0.45);
    font-weight: 500;
}
QLabel#settingValue {
    color: #f5f5f7;
    font-weight: 500;
}

/* --- Lists -------------------------------------------------------------- */
QListWidget {
    background-color: #141417;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 12px;
    padding: 4px;
    outline: none;
}
QListWidget::item {
    padding: 8px 10px;
    border-radius: 7px;
    color: #f5f5f7;
}
QListWidget::item:selected {
    background-color: rgba(10, 132, 255, 0.14);
    color: #f5f5f7;
}
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: rgba(255, 255, 255, 0.14);
    border-radius: 4px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background: rgba(255, 255, 255, 0.22);
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

/* --- Buttons -------------------------------------------------------------- */
QPushButton {
    background-color: #1c1c1f;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 9px;
    padding: 8px 14px;
    color: #f5f5f7;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #242428;
}
QPushButton:pressed {
    background-color: #18181b;
}
QPushButton:disabled {
    color: rgba(245, 245, 247, 0.28);
    background-color: #141417;
    border: 1px solid rgba(255, 255, 255, 0.05);
}
QPushButton[quickAction="true"] {
    text-align: left;
    padding: 9px 12px;
    background-color: transparent;
    border: none;
}
QPushButton[quickAction="true"]:hover {
    background-color: rgba(255, 255, 255, 0.06);
}
QPushButton[quickAction="true"]:pressed {
    background-color: rgba(255, 255, 255, 0.03);
}

/* record_button is the one place in the window the accent colour fills a
   whole control, rather than tinting one -- the single primary action
   earns the single accent; everything else stays neutral tone. */
QPushButton#record_button {
    background-color: #0a84ff;
    border: none;
    border-radius: 22px;
    padding: 12px 40px;
    font-size: 14px;
    font-weight: 600;
    color: #ffffff;
}
QPushButton#record_button:hover {
    background-color: #2894ff;
}
QPushButton#record_button:pressed {
    background-color: #0870d6;
}
QPushButton#record_button:disabled {
    background-color: #1c1c1f;
    color: rgba(245, 245, 247, 0.3);
}

/* --- Form (Settings page) ------------------------------------------------- */
QCheckBox {
    font-weight: 500;
    spacing: 8px;
    padding: 4px 0;
    color: #f5f5f7;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid rgba(255, 255, 255, 0.2);
    border-radius: 4px;
    background-color: #141417;
}
QCheckBox::indicator:checked {
    background-color: #0a84ff;
    border: 1px solid #0a84ff;
}
"""
