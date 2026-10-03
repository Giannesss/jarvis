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
    QEvent,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    QThread,
    QTimer,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFontMetrics,
    QIcon,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QShortcut,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGraphicsBlurEffect,
    QGraphicsOpacityEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from jarvis import brain, config, db, listener, memory, policy, skills, speaker
from jarvis.config import SKILL_APPS, SKILL_SITES
from jarvis.gui import icons, theme

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
CPU_BAR = "cpu_bar"
RAM_BAR = "ram_bar"
VRAM_BAR = "vram_bar"
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
HOME_GREETING_LABEL = "home_greeting_label"
HOME_STATE_LABEL = "home_state_label"
HOME_PROMPT_LABEL = "home_prompt_label"
OPEN_CONVERSATION_BUTTON = "open_conversation_button"
RECENT_ACTIVITY_LIST = "recent_activity_list"
NAV_AUTOMATIONS_ACTIVE = "nav_automations_active"
NAV_AUTOMATIONS_HISTORY = "nav_automations_history"
TITLE_BAR = "title_bar"
THEME_COMBO = "theme_combo"
ACCENT_COMBO = "accent_combo"
COMMAND_PALETTE_INPUT = "command_palette_input"
COMMAND_PALETTE_LIST = "command_palette_list"
RECENT_ACTIVITY_EMPTY_LABEL = "recent_activity_empty_label"

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


def _panel_title(text: str, accent: str | None = None) -> QLabel:
    """A panel's own heading (Κατάσταση Συστήματος, Γρήγορες Ενέργειες,
    Σήμερα) -- plain text, no leading coloured dot.

    The ninth design pass removes the bullet the eighth pass drew here: a
    small coloured dot in front of every single section heading is exactly
    the kind of templated flourish that reads as generated rather than
    designed -- a real settings/dashboard app (Linear, Things, Raycast)
    tells its sections apart with quiet weight and spacing, not a different
    coloured dot per card. `accent` is still accepted (nothing calling this
    needed to change) but is now unused -- kept only so every existing call
    site stays valid without an editing pass of its own; hierarchy here
    comes from the `panelTitle` object name's own type styling alone."""
    label = QLabel(text)
    label.setObjectName("panelTitle")
    return label


def _dot_icon(color: str, diameter: int = 10) -> QIcon:
    """A small solid-colour circle, used as a quick-action button's icon
    (see `_build_quick_actions_box`). Drawn in code rather than loaded from
    an asset file or a Unicode glyph -- a glyph risks rendering as a tofu box
    on a font that doesn't carry it, which would look more broken than no
    icon at all, while a plain painted circle renders identically everywhere
    PySide6 runs. `QPushButton.setIcon()` doesn't touch `.text()`, so this
    adds visual texture to the quick-action list without changing anything
    `QuickActionWiringTests` already asserts on."""
    pixmap = QPixmap(diameter, diameter)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(0, 0, diameter, diameter)
    painter.end()
    return QIcon(pixmap)


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

# A second accent, used only for decoration -- never for state meaning (that
# stays _ORB_COLOR/_STATUS_DOT_COLOR's job, unchanged above). Pairing the
# existing accent blue with a violet gives the orb's ring/arc, the record
# button and the three Home-page panels a two-tone identity instead of one
# flat hue repeated everywhere, which was a real piece of the "plain and
# boring" feedback -- a single accent colour, however accurately used, still
# reads as one note. _ACCENT_TEAL is the Current Task panel's own tint, kept
# distinct from both so the panel's "mine" colour doesn't fight the
# blue-vs-violet pairing used for System Status vs Quick Actions. None of
# this reuses _ORB_COLOR's SPEAKING blue for anything but SPEAKING -- the
# panels' tints are purely decorative label colour, not state.
# Kept as module-level aliases of theme.py's own constants (the single
# source of truth now that colour lives in a theme file) rather than
# rewriting every `_SECONDARY_ACCENT`/`_ACCENT_TEAL` reference below --
# both still name a fixed decorative pairing, never the user's chosen
# accent (see theme.py's own docstring on why).
_SECONDARY_ACCENT = theme.SECONDARY_ACCENT
_ACCENT_TEAL = theme.ACCENT_TEAL

# The big word under the orb on the Home page -- a second, larger rendering
# of the same state _STATUS_TEXT already names in the header, because the
# redesign brief asks for the centre of the window to carry its own status
# rather than making the user look up at the header to know what Jarvis is
# doing. Deliberately short, all-caps single words (ΕΤΟΙΜΟ/ΑΚΟΥΩ/ΣΚΕΦΤΟΜΑΙ/
# ΜΙΛΑΩ) rather than the header's full sentence -- this is a glanceable
# headline, not a second copy of the same sentence.
_HOME_STATE_TEXT = {
    State.IDLE: "ΕΤΟΙΜΟ",
    State.LISTENING: "ΑΚΟΥΩ",
    State.THINKING: "ΣΚΕΦΤΟΜΑΙ",
    State.SPEAKING: "ΜΙΛΑΩ",
}

# How fast the orb's glow pulses, per state -- idle breathes slowly and
# barely; listening/speaking breathe faster, close to each other since both
# are "something audible is happening right now"; thinking sits between the
# two since it's active but not tied to a live audio stream the way the
# other two are. Replaces the old binary idle/not-idle split with one real
# distinction per state, which is also what lets the four states actually
# read apart from each other rather than "idle" vs. "everything else".
_PULSE_SPEED = {
    State.IDLE: 0.02,
    State.LISTENING: 0.07,
    State.THINKING: 0.05,
    State.SPEAKING: 0.06,
}


def _time_of_day_greeting(hour: int) -> str:
    """A plain Greek time-of-day greeting -- no profile data involved, just
    the clock. Paired with the stored name (if any) by the caller
    (MainWindow._update_clock) into "Καλησπέρα, Γιάννη." or, with nothing
    saved yet, the bare greeting alone -- never a guessed or placeholder
    name, per the project's own rule against presenting invented data as
    real (see "Grounding" in CLAUDE.md)."""
    if hour < 12:
        return "Καλημέρα"
    if hour < 20:
        return "Καλησπέρα"
    return "Καλό βράδυ"

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

    Restrained on purpose, and more so again as of the ninth design pass:
    a glowing orb with a rotating arc and an orbiting particle swarm is one
    of the most recognisable "AI assistant" visual cliches there is -- it's
    close to the first thing an image generator draws for the prompt "AI
    voice assistant UI", which is exactly what makes it read as generated
    rather than designed, independent of how original the individual shapes
    are. The arc and the particle swarm the seventh/eighth passes added are
    gone; what's left is one ring, a handful of same-colour bars, and a
    slow breathing glow -- enough to answer "what is Jarvis doing right
    now" at a glance, without three independent layers of motion competing
    for attention to do it.

    Colour changes are *animated*, not snapped -- `set_state()` starts a
    ~280ms QVariantAnimation from the orb's current displayed colour to the
    new state's, eased out, so a state change reads as a deliberate
    transition rather than a flicker. "Smooth, physical, intentional" is the
    brief; an instant colour swap is none of those."""

    _COLOR_TRANSITION_MS = 280

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Down from the eighth pass's 320 -- a giant glowing circle filling
        # most of the centre column is itself a cliche ("the primary focus
        # of the dashboard" was answered with sheer size, not with better
        # content around it). 260 is still clearly the page's focal object
        # without dominating it the way 320 did.
        self.setMinimumSize(260, 260)
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
        speed = _PULSE_SPEED[self._state]
        pulse = (math.sin(self._phase * speed) + 1) / 2  # 0..1

        glow_radius = side * (0.38 + 0.02 * pulse)
        gradient = QRadialGradient(cx, cy, glow_radius)
        glow = QColor(color)
        glow.setAlpha(42)
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

        # No rotating arc and no orbiting particles here any more (the
        # seventh/eighth passes drew both). A continuously spinning ring
        # plus a glowing core plus a particle swarm is the single most
        # recognisable "AI assistant" cliche there is -- it's the first
        # thing most image generators draw when asked for one, which makes
        # it read as generated rather than designed, however original the
        # individual shapes are. What is left is deliberately plain: one
        # ring, a handful of bars, one colour, a slow breathing glow -- the
        # orb's job is to answer "what is Jarvis doing" in one glance, and
        # a glance doesn't need three independent layers of motion to do
        # that.
        if self._state is State.THINKING:
            # A single short dash sweeping the ring, rather than a
            # continuous arc or an orbiting swarm -- enough motion to read
            # as "working", restrained enough not to be the loudest thing
            # on the page.
            angle_deg = (self._phase * 1.4) % 360
            dash_pen = QPen(QBrush(color), max(side * 0.012, 2.0))
            dash_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(dash_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            ring_rect = QRectF(
                cx - ring_radius, cy - ring_radius, ring_radius * 2, ring_radius * 2
            )
            painter.drawArc(ring_rect, int(angle_deg * 16), int(34 * 16))
        else:
            # Five bars, one colour, idle resting as short flat dashes --
            # quieter than the seventh pass's seven alternating-colour bars.
            # SPEAKING wobbles with a bit more amplitude than LISTENING, so
            # the one state the brief calls a "waveform" still reads as the
            # most active one, without needing a second hue to say so.
            bar_count = 5
            bar_area_width = side * 0.3
            bar_gap = bar_area_width / bar_count
            base_y = cy
            amplitude = 0.08 if self._state is State.SPEAKING else 0.045
            bar_color = QColor(color)
            bar_color.setAlpha(205)
            painter.setBrush(bar_color)
            painter.setPen(Qt.PenStyle.NoPen)
            for i in range(bar_count):
                if self._state is State.IDLE:
                    height = side * 0.025
                else:
                    # A deterministic pseudo-wave from the phase and the
                    # bar's own index -- decorative, not a real audio level
                    # (see ORB_TICK_MS above).
                    wobble = (math.sin(self._phase * 0.18 + i * 1.1) + 1) / 2
                    height = side * (0.03 + amplitude * wobble)
                x = cx - bar_area_width / 2 + i * bar_gap
                painter.drawRoundedRect(
                    QRectF(x, base_y - height / 2, bar_gap * 0.45, height), 2, 2
                )


class _Background(QWidget):
    """The window's own canvas -- an eleventh-pass rewrite of the tenth
    pass's night-sky-over-mountains scene, after direct feedback that the
    first version ("too childish") read as a flat cartoon rather than the
    moody, photographic mountain landscape the user's reference photo
    actually shows. Nothing here is a copy of that photo, or any photo --
    there is still no image asset in this repo and no network fetch to get
    one (the same discipline every other original visual in this file
    follows, from `_Orb` to `icons.py`); what changed is how convincingly
    the procedural shapes read as a real place rather than a vector
    illustration of one.

    Three things make the difference, in order of how much they matter:

    1. **The ridgelines are no longer clean sine waves.** A pure sum of 2-3
       sine harmonics (the tenth pass's `_ridge_path`) repeats with visible
       periodicity and has no fine detail -- it reads as a wavy line, not a
       mountain. `_hash_noise()` is a deterministic "sin-hash" (classic
       shader trick: `frac(sin(x) * big_number)`) layered on top of the
       broad sine shape at a much finer scale, breaking the silhouette into
       small, irregular, non-repeating jags the way a real skyline never
       lines up into a clean period. No `random` module is used anywhere --
       the jaggedness is exactly as reproducible as the tenth pass's sine
       waves were, just no longer visibly made of them.
    2. **Three depth layers with real atmospheric perspective, not two flat
       silhouettes.** A real hazy mountain photo's far ridge is desaturated
       and blued toward the sky colour (haze scatters blue light), and gets
       almost no fine detail (haze smooths out texture at a distance); the
       near ridge is darkest, most saturated toward black, and carries the
       most jagged detail. `_LAYERS` encodes exactly that gradient -- each
       layer's own colour is already most of the way mixed toward the sky,
       and `jaggedness` shrinks with distance -- rather than three ridges
       that only differ in how dark they are.
    3. **A soft haze band sits across the lower third of the frame**,
       painted after the mountains, blending their silhouettes into the
       sky right at the horizon the way real atmospheric haze does --
       without it, even a well-shaped mountain silhouette reads as a flat
       cutout pasted in front of a gradient, because real air is never
       perfectly transparent down to a hard horizon line.

    A small, soft moon (or a pale sun-disc in light mode) replaces the
    tenth pass's accent-tinted glow sitting centred in the sky for no
    reason -- a single real light source the rest of the scene can be read
    as being lit by, rather than an ambient wash. The accent colour still
    shows through, now as a faint warm/cool tint low in the haze band
    (the way a city's light pollution or a coloured accent wash would
    actually tint a horizon), so "Χρώμα έμφασης" in Settings still visibly
    affects this canvas.

    Stars are more numerous and far less uniform: a wide spread of sizes
    weighted toward many small, dim points and a few brighter ones (a real
    sky has far more faint stars than bright ones), and each twinkles on
    its own independent phase exactly as the tenth pass's did -- stars
    still never drift, since real ones don't.

    **The ridge paths are cached, not recomputed every 60ms tick.** The
    tenth pass rebuilt its (simpler) ridge paths from scratch on every
    `paintEvent`, which was cheap enough at that detail level to not
    matter; multi-octave noise at the detail level this pass needs is not
    free to recompute 16+ times a second for shapes that never move once
    the window is a given size. `_rebuild_ridges()` runs once per actual
    size change (tracked by `_ridge_size`, checked at the top of
    `paintEvent`) and the three cached `QPainterPath`s are reused on every
    other tick -- the terrain is static; only the stars' twinkle and the
    moon's faint shimmer actually need to redraw each frame.

    Every child widget (header, sidebar, pages) is still added via the
    ordinary QVBoxLayout in _build_central_widget() -- Qt paints children
    after their parent in the same cycle, so nothing about how the rest of
    the window is built changes; this still replaces only how the canvas
    itself is drawn.

    tick() is still driven by MainWindow's existing _orb_timer rather than
    a timer of its own, for the same reason the tenth pass gave: one more
    QTimer purely to nudge star twinkle doesn't earn its own object when
    one is already ticking at the right cadence."""

    _STAR_COUNT = 160

    # (base_y_frac relative to the horizon, amplitude_frac of height,
    # seed, jaggedness 0-1, haze_mix 0-1 toward the sky colour) -- far to
    # near. haze_mix is what gives atmospheric perspective: the far layer
    # is mixed heavily toward the sky colour (desaturated, bluish), the
    # near layer almost not at all (reads as near-black, full contrast).
    _LAYERS = (
        (0.90, 0.075, 1.3, 0.15, 0.55),
        (0.97, 0.095, 3.6, 0.45, 0.28),
        (1.05, 0.11, 6.4, 1.0, 0.0),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._phase = 0.0
        self._mode = "dark"
        self._accent = QColor(theme.ACCENTS["blue"])
        # Each star is (x_frac, y_frac, size, twinkle_seed, twinkle_speed,
        # brightness_tier) -- fixed at construction (a plain, seeded
        # pattern, not re-rolled every resize) so the sky doesn't visibly
        # "reshuffle" the first time the window changes size. The sixth
        # element skews the field toward many small, dim stars and only a
        # few bright ones, the way a real sky actually looks -- the tenth
        # pass gave every star the same handful of sizes with no such
        # weighting, which is part of why it read as a flat pattern.
        self._stars = []
        for i in range(self._STAR_COUNT):
            tier_roll = (i * 0.1546 + i * i * 0.0021) % 1.0
            if tier_roll > 0.93:
                size, brightness = 2.0, 1.0
            elif tier_roll > 0.72:
                size, brightness = 1.4, 0.7
            else:
                size, brightness = 0.9, 0.4
            self._stars.append(
                (
                    (i * 0.6180339887 + i * i * 0.013) % 1.0,
                    ((i * 0.3312469 + 0.05) % 0.60),
                    size,
                    i * 11.7,
                    0.015 + (i % 5) * 0.008,
                    brightness,
                )
            )
        self._ridge_size: tuple[int, int] | None = None
        self._ridge_paths: list[QPainterPath] = []

    def set_theme(self, mode: str, accent_hex: str) -> None:
        """Ties the horizon haze tint to the active theme/accent -- the
        canvas's own colour now comes from theme.py the same way every QSS
        rule in the stylesheet does, rather than a literal baked into this
        class."""
        self._mode = mode
        self._accent = QColor(accent_hex)
        self.update()

    @staticmethod
    def _mix(base: QColor, tint: QColor, amount: float) -> QColor:
        return QColor(
            int(base.red() + (tint.red() - base.red()) * amount),
            int(base.green() + (tint.green() - base.green()) * amount),
            int(base.blue() + (tint.blue() - base.blue()) * amount),
        )

    def tick(self) -> None:
        self._phase += 1.0
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802 -- Qt's own name
        super().resizeEvent(event)
        # Invalidate the cached ridge paths rather than rebuild them here --
        # a resize can fire several times in a row while a window is being
        # dragged, and paintEvent only needs the final size, not every
        # intermediate one.
        self._ridge_size = None

    @staticmethod
    def _hash_noise(x: float, seed: float) -> float:
        """A deterministic pseudo-random value in [-1, 1] from `x` and
        `seed` -- the classic shader "sin-hash" (`frac(sin(x) * k)`), with
        no `random` module and no state: the same `x`/`seed` always
        produces the same value, so a layer's fine jagged detail is exactly
        as reproducible as the tenth pass's clean sine waves were, just no
        longer visibly periodic the way a plain sine sum is."""
        v = math.sin(x * 127.1 + seed * 311.7) * 43758.5453
        return 2.0 * (v - math.floor(v)) - 1.0

    def _ridge_path(
        self, w: int, h: int, base_y: float, amplitude: float, seed: float, jaggedness: float
    ) -> QPainterPath:
        """A broad, slow silhouette (three summed sine harmonics, same idea
        as the tenth pass) with a finer, non-periodic jagged layer added on
        top via `_hash_noise()`, scaled by `jaggedness` -- 0 gives back
        something close to the tenth pass's smooth wave (used for the
        haziest, farthest layer), 1 gives the near layer its full jagged,
        rocky-skyline detail."""
        path = QPainterPath()
        path.moveTo(0, h)
        steps = 96
        for i in range(steps + 1):
            x = w * i / steps
            t = i / steps
            y = base_y + amplitude * (
                math.sin(t * 4.3 + seed) * 0.46
                + math.sin(t * 9.1 + seed * 1.7) * 0.26
                + math.sin(t * 17.3 + seed * 2.9) * 0.14
            )
            y += amplitude * jaggedness * 0.16 * self._hash_noise(t * 26.0, seed)
            y += amplitude * jaggedness * 0.07 * self._hash_noise(t * 71.0, seed * 1.9)
            path.lineTo(x, y)
        path.lineTo(w, h)
        path.closeSubpath()
        return path

    def _rebuild_ridges(self, w: int, h: int, horizon: float) -> None:
        self._ridge_paths = [
            self._ridge_path(w, h, horizon * base_y, h * amp, seed, jag)
            for base_y, amp, seed, jag, _haze in self._LAYERS
        ]
        self._ridge_size = (w, h)

    def paintEvent(self, event) -> None:  # noqa: N802 -- Qt's own name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        horizon = h * 0.62

        if self._mode == "light":
            sky_top, sky_mid, sky_horizon = (
                QColor(198, 210, 224),
                QColor(188, 200, 216),
                QColor(224, 214, 206),
            )
            ridge_base = QColor(58, 64, 76)
            haze_color = QColor(210, 206, 204)
            moon_color = QColor(255, 250, 240)
            star_color = QColor(90, 100, 120)
            star_max_alpha = 26
        else:
            sky_top, sky_mid, sky_horizon = (
                QColor(5, 6, 10),
                QColor(9, 12, 19),
                QColor(16, 20, 30),
            )
            ridge_base = QColor(7, 8, 11)
            haze_color = QColor(32, 38, 52)
            moon_color = QColor(232, 236, 245)
            star_color = QColor(235, 240, 255)
            star_max_alpha = 190

        if self._ridge_size != (w, h):
            self._rebuild_ridges(w, h, horizon)

        # The sky: a vertical gradient, darkest at the zenith, lifting
        # toward the horizon -- no accent tint here anymore (the tenth
        # pass's centred accent-coloured glow read as a stage light, not a
        # sky); the accent now only tints the haze band low in the frame,
        # the way a real light source would actually colour the air near
        # it rather than the whole dome of sky.
        sky = QLinearGradient(0, 0, 0, horizon * 1.08)
        sky.setColorAt(0.0, sky_top)
        sky.setColorAt(0.55, sky_mid)
        sky.setColorAt(1.0, sky_horizon)
        painter.fillRect(QRectF(0, 0, w, horizon * 1.08), sky)
        painter.fillRect(QRectF(0, horizon * 1.08, w, h - horizon * 1.08), sky_horizon)

        # A small, soft moon -- one real light source the rest of the
        # scene reads as lit by, rather than the tenth pass's ambient
        # centred glow. Off-centre and high, the way a moon actually sits
        # rather than hovering dead-centre over the horizon.
        moon_x, moon_y, moon_r = w * 0.74, horizon * 0.22, min(w, h) * 0.028
        moon_glow = QRadialGradient(moon_x, moon_y, moon_r * 7)
        moon_glow.setColorAt(0.0, QColor(moon_color.red(), moon_color.green(), moon_color.blue(), 46))
        moon_glow.setColorAt(1.0, QColor(moon_color.red(), moon_color.green(), moon_color.blue(), 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(moon_glow)
        painter.drawEllipse(QRectF(moon_x - moon_r * 7, moon_y - moon_r * 7, moon_r * 14, moon_r * 14))
        painter.setBrush(moon_color)
        painter.drawEllipse(QRectF(moon_x - moon_r, moon_y - moon_r, moon_r * 2, moon_r * 2))

        # Stars: fixed positions, each twinkling independently and slowly
        # on its own phase -- never drifting, since real stars don't. Most
        # are small and dim; a few are brighter, per the brightness tier
        # baked in at construction.
        painter.setPen(Qt.PenStyle.NoPen)
        for x_frac, y_frac, size, seed, speed, brightness in self._stars:
            twinkle = (math.sin(self._phase * speed + seed) + 1) / 2
            alpha = int(star_max_alpha * brightness * (0.35 + 0.65 * twinkle))
            painter.setBrush(
                QColor(star_color.red(), star_color.green(), star_color.blue(), alpha)
            )
            painter.drawEllipse(QRectF(x_frac * w, y_frac * horizon, size, size))

        # The mountain layers, far to near -- each already mixed toward the
        # sky colour per its own haze_mix, which is what gives atmospheric
        # perspective instead of three ridges that only differ in flat
        # darkness.
        painter.setPen(Qt.PenStyle.NoPen)
        for (_, _, _, _, haze_mix), path in zip(self._LAYERS, self._ridge_paths):
            painter.setBrush(self._mix(ridge_base, sky_mid, haze_mix))
            painter.drawPath(path)

        # A soft haze band across the lower frame, blending the nearest
        # ridge's base into the sky colour the way real atmospheric haze
        # softens a horizon rather than cutting it with a hard edge --
        # tinted faintly by the active accent low in the band, which is
        # this canvas's one remaining nod to "Χρώμα έμφασης" actually
        # doing something visible.
        haze_top = horizon * 0.80
        haze = QLinearGradient(0, haze_top, 0, h)
        tinted_haze = self._mix(haze_color, self._accent, 0.22)
        haze.setColorAt(0.0, QColor(tinted_haze.red(), tinted_haze.green(), tinted_haze.blue(), 0))
        haze.setColorAt(0.65, QColor(tinted_haze.red(), tinted_haze.green(), tinted_haze.blue(), 60))
        haze.setColorAt(1.0, QColor(tinted_haze.red(), tinted_haze.green(), tinted_haze.blue(), 95))
        painter.fillRect(QRectF(0, haze_top, w, h - haze_top), haze)


def _parse_rgba(value: str) -> QColor:
    """"rgba(20, 20, 24, 0.55)" -> QColor(20, 20, 24, 140) -- theme.py's
    palette dict stores colours as CSS-style rgba() strings (so they drop
    straight into QSS), but _GlassPanel paints by hand rather than through
    QSS, so it needs an actual QColor. Parses only the shape theme.py
    itself ever produces; not a general CSS colour parser."""
    inner = value.strip()
    inner = inner[inner.index("(") + 1 : inner.rindex(")")]
    r, g, b, alpha = (part.strip() for part in inner.split(","))
    return QColor(int(r), int(g), int(b), int(float(alpha) * 255))


def _blur_pixmap(source: QPixmap, radius: float) -> QPixmap:
    """A real Gaussian-style blur of `source`, via Qt's own
    `QGraphicsBlurEffect` rendered through an offscreen `QGraphicsScene` --
    the standard Qt recipe for blurring a pixmap, since `QPainter` has no
    blur primitive of its own. Used by `_GlassPanel` below for actual
    backdrop blur (the user's explicit choice over a cheaper faux-glass
    tint, see CLAUDE.md's eighth-pass section) rather than a `box-shadow`-
    style approximation."""
    if source.isNull():
        return source
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(source)
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius)
    item.setGraphicsEffect(effect)
    scene.addItem(item)
    result = QPixmap(source.size())
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    scene.render(painter, QRectF(result.rect()), QRectF(source.rect()))
    painter.end()
    return result


class _GlassPanel(QFrame):
    """A card with a *real* blurred backdrop behind it, not a flat tint --
    the redesign brief's own glassmorphism ask, and specifically "real
    backdrop blur" over a cheaper faux-glass alternative once the trade-off
    against "keep CPU usage low" was spelled out (see CLAUDE.md's eighth
    pass).

    Deliberately narrow in scope, which is what keeps that trade-off
    honest: only the three Home-page side cards (System Status/Quick
    Actions/Σήμερα, see _build_monitoring_box() and its two siblings) are
    `_GlassPanel`s -- nothing that repaints every animation frame (the orb,
    `_Background` itself) ever needs one, and the blurred pixmap is
    refreshed on a throttled cadence (every fourth tick of the shared
    `_orb_timer`, ~240ms) rather than on every paint, which is what a live
    blur-behind would otherwise cost on every single frame.

    The blur source is `_Background` specifically, not the whole window --
    these cards sit directly over the aurora canvas with nothing else
    behind them, so capturing just that widget is the complete picture
    without walking the rest of the widget tree per refresh."""

    _REFRESH_EVERY_N_TICKS = 4
    _BLUR_RADIUS = 28

    def __init__(self, background: "_Background", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._background_widget = background
        self._blurred: QPixmap | None = None
        self._tick_count = 0
        self._fill = QColor(20, 20, 24, 140)
        self._border = QColor(255, 255, 255, 20)

    def set_palette(self, fill: QColor, border: QColor) -> None:
        self._fill = fill
        self._border = border
        self.update()

    def refresh_blur(self, force: bool = False) -> None:
        self._tick_count += 1
        if not force and self._tick_count % self._REFRESH_EVERY_N_TICKS:
            return
        if not self.isVisible() or self.width() <= 0 or self.height() <= 0:
            return
        top_left = self.mapTo(self._background_widget, self.rect().topLeft())
        region = QRect(top_left, self.size()).intersected(self._background_widget.rect())
        if region.isEmpty():
            return
        source = self._background_widget.grab(region)
        self._blurred = _blur_pixmap(source, self._BLUR_RADIUS)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 -- Qt's own name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, 14, 14)
        painter.setClipPath(path)
        if self._blurred is not None:
            painter.drawPixmap(self.rect(), self._blurred)
        painter.fillPath(path, self._fill)
        painter.setClipping(False)
        painter.setPen(QPen(self._border, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        # No per-card accent line here any more (the eighth pass drew one
        # in a different colour per card -- blue/violet/teal). Three boxes
        # on the same page each wearing their own hue reads as a dashboard
        # template colour-coding itself for no reason a user asked for; a
        # real app leaves colour for things that mean something (the record
        # button, a selected state) and lets plain hairline borders do the
        # quiet work of separating one card from the next.


# Which sender a transcript line belongs to, and the text to actually show
# inside its bubble -- every line still starts with the fixed "Εσύ: "/
# "Jarvis: "/"[debug] " prefix every call site already writes (unchanged, on
# purpose: tests/test_gui_shell.py asserts on QListWidgetItem.text() exactly,
# and nothing here touches what's stored in the item -- only how
# _ChatBubbleDelegate paints it). Kept as a free function so the delegate's
# paint() and sizeHint() read the same classification rather than each
# re-deriving it.
def _bubble_kind(text: str) -> tuple[str, str]:
    if text.startswith("Εσύ: "):
        return "user", text[len("Εσύ: ") :]
    if text.startswith("Jarvis: "):
        return "jarvis", text[len("Jarvis: ") :]
    if text.startswith("[debug] "):
        return "debug", text
    return "jarvis", text


class _ChatBubbleDelegate(QStyledItemDelegate):
    """Paints TRANSCRIPT_LIST's rows as chat bubbles -- the user's turns
    right-aligned in the accent colour, Jarvis's left-aligned in a neutral
    surface tone, the way every messaging app (Instagram DMs included) tells
    two speakers apart, instead of one flat list of "Εσύ: .../Jarvis: ..."
    rows stacked on top of each other.

    Deliberately a delegate, not a per-row custom QWidget via
    `QListWidget.setItemWidget()`: a delegate paints against
    `QListWidgetItem.text()` and leaves the item's actual text untouched, so
    every existing `transcript.item(n).text()` assertion in
    `tests/test_gui_shell.py` keeps working unchanged -- this changes how a
    line is drawn, never what is stored for it. A debug line ("[debug] ...")
    gets neither alignment: it's metadata about the turn, not a turn in the
    conversation, so it renders as a small muted centred caption with no
    bubble at all, visually subordinate to the two speakers' bubbles."""

    _MAX_BUBBLE_FRACTION = 0.68
    _H_PADDING = 14
    _V_PADDING = 10
    _ROW_GAP = 10
    _RADIUS = 16
    _SIDE_MARGIN = 6

    _COLORS = {
        "user": (QColor("#0a84ff"), QColor("#ffffff")),
        "jarvis": (QColor("#1c1c1f"), QColor("#f5f5f7")),
    }

    def _wrap_flags(self) -> int:
        return int(Qt.AlignmentFlag.AlignLeft) | int(Qt.TextFlag.TextWordWrap)

    def _bubble_rect(self, option, text: str) -> tuple[QRectF, str, str]:
        """Returns the bubble's rect plus which side/kind it belongs to --
        shared by paint() and sizeHint() so the two can never disagree about
        how tall a row is."""
        kind, content = _bubble_kind(text)
        available = max(option.rect.width(), 1)
        max_width = max(int(available * self._MAX_BUBBLE_FRACTION) - 2 * self._H_PADDING, 40)
        fm = QFontMetrics(option.font)
        bounds = fm.boundingRect(
            QRect(0, 0, max_width, 0), self._wrap_flags(), content
        )
        bubble_w = min(bounds.width() + 2 * self._H_PADDING, available - 2 * self._SIDE_MARGIN)
        bubble_h = bounds.height() + 2 * self._V_PADDING
        if kind == "user":
            x = option.rect.right() - bubble_w - self._SIDE_MARGIN
        else:
            x = option.rect.left() + self._SIDE_MARGIN
        y = option.rect.top() + self._ROW_GAP / 2
        return QRectF(x, y, bubble_w, bubble_h), kind, content

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 -- Qt's own name
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        kind, content = _bubble_kind(text)
        available = max(option.rect.width(), 1)
        if kind == "debug":
            fm = QFontMetrics(option.font)
            bounds = fm.boundingRect(
                QRect(0, 0, available - 2 * self._H_PADDING, 0),
                self._wrap_flags(),
                content,
            )
            return QSize(available, bounds.height() + self._ROW_GAP)
        bubble_rect, _, _ = self._bubble_rect(option, text)
        return QSize(available, int(bubble_rect.height() + self._ROW_GAP))

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        kind, content = _bubble_kind(text)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if kind == "debug":
            painter.setPen(QColor(245, 245, 247, 110))
            font = option.font
            font.setPointSizeF(max(font.pointSizeF() - 1, 8))
            font.setItalic(True)
            painter.setFont(font)
            text_rect = option.rect.adjusted(
                self._H_PADDING, 2, -self._H_PADDING, -2
            )
            painter.drawText(
                text_rect,
                int(Qt.AlignmentFlag.AlignHCenter) | int(Qt.TextFlag.TextWordWrap),
                content,
            )
            painter.restore()
            return

        bubble_rect, kind, content = self._bubble_rect(option, text)
        fill, text_color = self._COLORS[kind]
        path = QPainterPath()
        path.addRoundedRect(bubble_rect, self._RADIUS, self._RADIUS)
        painter.fillPath(path, fill)

        painter.setPen(text_color)
        painter.setFont(option.font)
        text_rect = bubble_rect.adjusted(
            self._H_PADDING, self._V_PADDING, -self._H_PADDING, -self._V_PADDING
        )
        painter.drawText(text_rect, self._wrap_flags(), content)
        painter.restore()


class _ChatTranscriptList(QListWidget):
    """TRANSCRIPT_LIST's own class -- identical to a plain QListWidget
    except that resizing it re-lays-out its rows. A word-wrapping delegate's
    `sizeHint()` depends on the viewport's current width (how wide a bubble
    is allowed to be before it wraps), but `QListView` doesn't recompute row
    heights on its own just because the widget was resized -- without this,
    resizing the window would leave old rows wrapped for the old width while
    new rows use the new one. `doItemsLayout()` is the public method Qt
    itself documents for forcing exactly that recomputation."""

    def resizeEvent(self, event) -> None:  # noqa: N802 -- Qt's own name
        super().resizeEvent(event)
        self.doItemsLayout()


class _CommandPalette(QDialog):
    """Ctrl+K (redesign brief, "Extras") -- a small, frameless, filterable
    list of actions: every sidebar page plus every configured quick action
    (config.SKILL_SITES/SKILL_APPS), read fresh each time the palette opens
    rather than cached, so it can never show a site/app that was since
    removed from config.py. Deliberately plain: a QDialog with a QLineEdit
    and a QListWidget, not a custom overlay widget -- a modal popup is
    exactly what a command palette is everywhere it's been copied from
    (Linear, Raycast, VS Code's own Ctrl+Shift+P), and QDialog gives
    Escape-to-close and click-outside-to-close for free."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(
            window, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Popup
        )
        self.setObjectName("commandPalette")
        self._window = window
        self.setFixedWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._input = QLineEdit()
        self._input.setObjectName(COMMAND_PALETTE_INPUT)
        self._input.setPlaceholderText("Πληκτρολόγησε μια εντολή…")
        layout.addWidget(self._input)

        self._list = QListWidget()
        self._list.setObjectName(COMMAND_PALETTE_LIST)
        self._list.setMaximumHeight(280)
        layout.addWidget(self._list)

        self._commands = self._build_commands()
        self._filter("")

        self._input.textChanged.connect(self._filter)
        self._input.returnPressed.connect(self._run_selected)
        self._list.itemActivated.connect(lambda _item: self._run_selected())
        self._input.setFocus()

    def _build_commands(self) -> list[tuple[str, object]]:
        w = self._window
        commands: list[tuple[str, object]] = [
            ("Μετάβαση: Αρχική", lambda: w._go_to_page(NAV_HOME, index=0)),
            ("Μετάβαση: Συνομιλία", lambda: w._go_to_page(NAV_CHAT, index=1)),
            ("Μετάβαση: Εργασίες", lambda: w._go_to_page(NAV_TASKS, index=2)),
            ("Μετάβαση: Αρχεία", lambda: w._go_to_page(NAV_FILES, index=3)),
            ("Μετάβαση: Ρυθμίσεις", lambda: w._go_to_page(NAV_SETTINGS, index=4)),
            ("Ξεκίνα εγγραφή", w._start_listening),
        ]
        for label, url in SKILL_SITES.items():
            commands.append(
                (
                    f"Άνοιξε {label}",
                    lambda label=label, url=url: w._quick_action_site(label, url),
                )
            )
        for label, argv in SKILL_APPS.items():
            commands.append(
                (
                    f"Άνοιξε {label}",
                    lambda label=label, argv=argv: w._quick_action_app(label, argv),
                )
            )
        return commands

    def _filter(self, text: str) -> None:
        self._list.clear()
        needle = text.strip().lower()
        for label, _action in self._commands:
            if needle in label.lower():
                self._list.addItem(label)
        if self._list.count():
            self._list.setCurrentRow(0)

    def _run_selected(self) -> None:
        item = self._list.currentItem()
        label = item.text() if item is not None else None
        self.close()
        if label is None:
            return
        for cmd_label, action in self._commands:
            if cmd_label == label:
                action()
                return


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

    # How close to an edge (in px) a press has to land for _resize_edges()
    # to treat it as a resize grab rather than an ordinary click -- only
    # meaningful now that the window is frameless and the OS no longer
    # draws its own resize border (see _build_title_bar()).
    _RESIZE_MARGIN = 6

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Jarvis")
        self.resize(1200, 760)

        # A custom title bar (redesign brief, "Extras") replaces the native
        # one -- Qt.FramelessWindowHint removes the OS-drawn bar/buttons,
        # and _build_title_bar() below draws Jarvis's own in the same dark
        # chrome tone as the header beneath it, with minimize/maximize/close
        # wired through startSystemMove()/startSystemResize() (see
        # eventFilter()/mousePressEvent() below) so the window still drags
        # and resizes exactly like an ordinary one -- only the pixels
        # drawing the chrome changed, not the window-manager behaviour
        # underneath it. QMainWindow's menuBar() still renders as an
        # ordinary widget under a frameless top-level window on Windows
        # (only native macOS moves it into the system menu), so the
        # Αρχείο/Προβολή menu is unaffected.
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        # Theme state (eighth design pass): which palette/accent the whole
        # window is painted with right now. Read once here and handed to
        # every stylesheet build and to _Background's own paint code, so
        # there is exactly one place ("Εμφάνιση" on the Settings page, see
        # _build_settings_page) that ever changes it. Not persisted across
        # a restart -- .env still owns every setting that survives one;
        # this is the one setting on the whole page that's deliberately
        # live instead (see _apply_theme()'s own docstring).
        self._theme_mode = "dark"
        self._accent = "blue"

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

        # The Home page's Recent Activity panel -- a small, session-only log
        # of real actions this GUI just took (a quick action opened, a turn
        # answered), newest first, capped at a handful. Not read from the
        # audit table: that log records which skill matched and why, not
        # which site opened or what was said, so this is its own log rather
        # than a reach into audit for detail it was never built to hold. See
        # _log_activity().
        self._recent_activity: list[tuple[str, str]] = []

        # Toast notifications (redesign brief, "Extras") -- a small,
        # transient overlay stack rather than a second logging path:
        # _show_toast() below creates one, parents it to this window so it
        # floats over whichever page is showing, and tears it down itself
        # after a fade. Kept as a list only so a toast isn't garbage
        # collected mid-animation.
        self._active_toasts: list[QFrame] = []

        # Cards that draw a real blurred backdrop (System Status/Quick
        # Actions/Σήμερα -- see _GlassPanel) rather than a flat tint. Filled
        # by _build_monitoring_box()/_build_quick_actions_box()/
        # _build_current_task_box() as each is constructed; _orb_timer
        # refreshes all three on the same tick the orb/background already
        # animate on (each panel throttles its own refresh further
        # internally -- see _GlassPanel._REFRESH_EVERY_N_TICKS), rather than
        # inventing a fourth timer for one more thing that only needs to
        # look alive, not instantaneous.
        self._glass_panels: list[_GlassPanel] = []

        self._build_menu_bar()
        self._build_central_widget()
        self._apply_theme()

        # The stored name, if any -- read once at construction rather than
        # on every clock tick, since it only ever changes from a voice save
        # ("θυμήσου ότι με λένε...") mid-session, which a restart (not a
        # tick) is what would pick up. None when nothing is saved yet, which
        # _update_clock() below renders as a bare greeting with no name --
        # never a guessed one, per "Grounding" in CLAUDE.md.
        self._profile_name = self._read_profile_name()

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
        self._orb_timer.timeout.connect(self._background.tick)
        self._orb_timer.timeout.connect(self._refresh_glass_panels)
        self._orb_timer.start(ORB_TICK_MS)

        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start(1000)
        self._update_clock()

        # Ctrl+K command palette (redesign brief, "Extras") -- a global
        # shortcut rather than a button, since its whole point is "summon it
        # from anywhere without reaching for the mouse", the same as every
        # Linear/Raycast-style palette it's modelled on.
        self._command_palette_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self._command_palette_shortcut.activated.connect(self._open_command_palette)

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

    def keyPressEvent(self, event) -> None:  # noqa: N802 -- Qt's own name
        """"Hold Space to Talk" from the redesign brief, answered honestly
        rather than literally: listener.listen() blocks until ffmpeg's own
        silence detection decides the recording is over, with no notion of
        "stop when a key is released" -- there is no press/release gesture
        this pipeline can actually implement without changing how recording
        itself works, which is well outside this pass. Pressing Space is
        instead wired to exactly what clicking "Εγγραφή" already does,
        which is the honest version of the same idea: a keyboard shortcut
        for starting a turn, not a hold-to-talk control. Guarded the same
        way the record button already guards a second click -- only when
        idle, so it never queues a conflicting turn -- and ignores key
        auto-repeat so holding Space down doesn't fire it over and over."""
        if (
            event.key() == Qt.Key.Key_Space
            and not event.isAutoRepeat()
            and self._state is State.IDLE
        ):
            self._start_listening()
            return
        super().keyPressEvent(event)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 -- Qt's own name
        """The title bar's own drag-to-move: a press anywhere on it (outside
        its three buttons, which consume their own clicks before this ever
        sees them) starts the OS's native window move via
        `QWindow.startSystemMove()` -- the Qt6-documented way to get normal
        window-dragging behaviour back once `Qt.FramelessWindowHint` has
        taken the OS's own title bar away. A double-click toggles maximize,
        the same gesture a native title bar already gives for free."""
        if obj is getattr(self, "_title_bar", None):
            if event.type() == QEvent.Type.MouseButtonDblClick:
                self._toggle_maximize()
                return True
            if (
                event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                handle = self.windowHandle()
                if handle is not None:
                    handle.startSystemMove()
                return True
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 -- Qt's own name
        """A frameless window has no OS-drawn border to grab for resizing
        either, so a press within `_RESIZE_MARGIN` px of an edge starts the
        OS's native resize via `QWindow.startSystemResize()` -- same idiom
        as the title bar's `startSystemMove()` above, just for the other
        half of "a window behaves normally" that `FramelessWindowHint` took
        away. Everywhere else, this defers to Qt's own handling exactly as
        before this pass, so no existing click target anywhere in the
        window is affected."""
        if event.button() == Qt.MouseButton.LeftButton:
            edges = self._resize_edges(event.position().toPoint())
            if edges:
                handle = self.windowHandle()
                if handle is not None:
                    handle.startSystemResize(edges)
                    return
        super().mousePressEvent(event)

    def _resize_edges(self, pos: QPoint) -> Qt.Edges:
        margin = self._RESIZE_MARGIN
        edges = Qt.Edges()
        if pos.x() <= margin:
            edges |= Qt.Edge.LeftEdge
        elif pos.x() >= self.width() - margin:
            edges |= Qt.Edge.RightEdge
        if pos.y() <= margin:
            edges |= Qt.Edge.TopEdge
        elif pos.y() >= self.height() - margin:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _text_color(self) -> str:
        """The current theme's primary text colour, as a hex string -- used
        to re-tint the handful of baked-pixmap icons (icons.py) a theme
        change can't reach through the stylesheet alone, since a QIcon's
        colour is burned into its pixmap at render time, not a stylesheet
        property the way a QLabel's `color` is."""
        return theme.PALETTES[self._theme_mode]["text_primary"]

    def _apply_theme(self, mode: str | None = None, accent: str | None = None) -> None:
        """Rebuilds and reapplies the whole window's stylesheet from
        `theme.build_stylesheet()` -- the one live setting on the Settings
        page (see _build_settings_page's "Εμφάνιση" box), unlike every other
        row there, which only ever reports what `.env` already fixed at
        startup. There is nothing to desync by letting this take effect
        immediately: it changes none of `config.py`'s own values, only how
        this window paints itself, so "never edit .env" (CLAUDE.md's own
        working rule) is untouched -- a restart still shows the same .env-
        driven rows it always has, just repainted in whichever theme was
        left selected.

        Called once at construction with no arguments (locking in the
        "dark"/"blue" defaults set in __init__), and again from the two
        Settings-page combo boxes whenever either changes."""
        if mode is not None:
            self._theme_mode = mode
        if accent is not None:
            self._accent = accent
        self.setStyleSheet(theme.build_stylesheet(self._theme_mode, self._accent))
        self._background.set_theme(self._theme_mode, theme.ACCENTS[self._accent])
        palette = theme.PALETTES[self._theme_mode]
        fill = _parse_rgba(palette["glass_fill"])
        border = _parse_rgba(palette["hairline"])
        for panel in self._glass_panels:
            panel.set_palette(fill, border)
        text_color = self._text_color()
        for object_name, button in self._nav_buttons.items():
            icon_name = self._nav_icon_names.get(object_name)
            if icon_name:
                button.setIcon(icons.icon(icon_name, text_color))
        self._refresh_glass_panels(force=True)

    def _refresh_glass_panels(self, force: bool = False) -> None:
        for panel in self._glass_panels:
            panel.refresh_blur(force=force)

    def _update_clock(self) -> None:
        now = datetime.now()
        widget(self, CLOCK_LABEL).setText(now.strftime("%H:%M"))
        widget(self, DATE_LABEL).setText(now.strftime("%a %d %b %Y"))

        # Recomputed every tick (the greeting word, not the name, which is
        # read once at construction -- see __init__) so a session left open
        # across, say, the afternoon/evening boundary doesn't keep saying
        # "Καλημέρα" forever. Cheap: no DB read, just an hour comparison.
        greeting = _time_of_day_greeting(now.hour)
        if self._profile_name:
            text = f"{greeting}, {self._profile_name}."
        else:
            text = f"{greeting}."
        widget(self, HOME_GREETING_LABEL).setText(text)

    def _read_profile_name(self) -> str | None:
        """The stored name, or None -- swallows every error, same discipline
        as _load_tasks()/memory.recall_safe(): a locked or broken database
        means the greeting falls back to the bare time-of-day phrase, never
        a crashed GUI. Also guards against a non-string result (a bare
        MagicMock stands in for a real sqlite3.Row in the mocked test suite,
        and would otherwise read as a truthy "name" with no sensible text),
        so this is always exactly a real saved name or nothing."""
        try:
            conn = db.connect()
            try:
                name = memory.profile_name(conn)
            finally:
                conn.close()
        except Exception:
            return None
        return name if isinstance(name, str) and name else None

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
        widget(self, CPU_BAR).setValue(int(cpu))
        widget(self, RAM_BAR).setValue(int(ram))

        vram = _read_vram_percent()
        if vram is None:
            widget(self, VRAM_LABEL).setText("VRAM: μη διαθέσιμο")
            widget(self, VRAM_BAR).setValue(0)
        else:
            widget(self, VRAM_LABEL).setText(f"VRAM: {vram:.0f}%")
            widget(self, VRAM_BAR).setValue(int(vram))

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

        # A count plus the nearest item, not just the nearest item alone --
        # closer to the brief's own "TODAY: 3 tasks completed" summary than
        # a single bare line, without inventing a "completed" count this
        # table doesn't actually distinguish (a fired/missed reminder still
        # carries its own suffix from _render_agenda_item(), so nothing here
        # claims a status the row doesn't already carry).
        current_task = widget(self, CURRENT_TASK_LABEL)
        if items:
            count = len(items)
            noun = "εκκρεμότητα" if count == 1 else "εκκρεμότητες"
            current_task.setText(
                f"{count} {noun} σήμερα — επόμενο: {_render_agenda_item(*items[0])}"
            )
        else:
            current_task.setText("Τίποτα εκκρεμές σήμερα.")

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
        # The Home page's own, larger status word -- a second rendering of
        # the same state, not a second source of truth for it (see
        # _HOME_STATE_TEXT above). The prompt line ("«Πώς μπορώ να
        # βοηθήσω;»") is hidden outside IDLE: it's an invitation to speak,
        # which stops being the useful thing to say the moment a turn is
        # already under way.
        widget(self, HOME_STATE_LABEL).setText(_HOME_STATE_TEXT[state])
        widget(self, HOME_PROMPT_LABEL).setVisible(state is State.IDLE)
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
        # _Background (see its own docstring) replaces the old plain
        # QWidget + QSS-gradient canvas -- it's what lets the faint grid and
        # the drifting particles exist at all, since QSS has no way to
        # express either. Kept as self._background so __init__ can connect
        # its tick() to the same timer the orb's animation already uses.
        central = _Background(self)
        central.setObjectName("appBackground")
        self.setCentralWidget(central)
        self._background = central

        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_title_bar())
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
        self._stack.addWidget(self._build_automations_page())
        body.addWidget(self._stack, stretch=1)

    def _build_title_bar(self) -> QWidget:
        """Replaces the OS-drawn title bar (redesign brief, "Extras: a
        custom title bar") -- Jarvis's own wordmark plus minimize/maximize/
        close buttons, in the same dark chrome tone as the header
        underneath it. Dragging and resizing still work exactly like an
        ordinary window; they're just driven by `startSystemMove()`/
        `startSystemResize()` now instead of the OS's own border (see
        `eventFilter()`/`mousePressEvent()`), so only the pixels drawing
        the chrome changed."""
        bar = QFrame()
        bar.setObjectName(TITLE_BAR)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 0, 0, 0)
        layout.setSpacing(0)

        label = QLabel("JARVIS")
        label.setObjectName("titleBarLabel")
        layout.addWidget(label)
        layout.addStretch(1)

        for object_name, icon_name, slot in (
            ("titleBarMinimize", "minimize", self.showMinimized),
            ("titleBarMaximize", "maximize", self._toggle_maximize),
            ("titleBarClose", "close", self.close),
        ):
            button = QPushButton()
            button.setObjectName(object_name)
            button.setProperty("titleBarButton", True)
            button.setIcon(icons.icon(icon_name, "#9a9aa2"))
            button.setIconSize(QSize(11, 11))
            button.clicked.connect(slot)
            layout.addWidget(button)

        bar.installEventFilter(self)
        self._title_bar = bar
        return bar

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
        """A refined navigation hierarchy per the redesign brief: a brand
        caption, four plain pages, a grouped "ΑΥΤΟΜΑΤΟΠΟΙΗΣΕΙΣ" section, and
        Settings on its own below a stretch -- rather than five flat,
        identical buttons. Each label carries a small glyph instead of
        relying on text alone (the brief's own "use icons rather than
        relying on text alone"), drawn as a plain leading character rather
        than a loaded icon asset -- same reasoning as _dot_icon(): a plain
        character renders identically everywhere PySide6 runs, with no
        tofu-box risk. None of this touches button.text() in a way that
        would collide with another test's exact-label lookup -- nothing
        asserts on a *nav* button's text, only on its object name (see
        SidebarNavigationTests), and QuickActionWiringTests looks up quick-
        action buttons specifically, which these aren't."""
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 20, 12, 16)
        layout.setSpacing(2)

        self._nav_buttons: dict[str, QPushButton] = {}
        self._nav_icon_names: dict[str, str] = {}

        brand = QLabel("JARVIS")
        brand.setObjectName("sidebarBrand")
        layout.addWidget(brand)
        layout.addWidget(self._build_sidebar_divider())

        # Greek, matching each page's own _page_title() exactly -- these used
        # to be English ("Chat") while the page you landed on said
        # "Συνομιλία", a mismatch nobody asked for and nothing in the app
        # otherwise has: Jarvis speaks Greek throughout (see "Language" in
        # CLAUDE.md), and the sidebar was the one place still in English.
        # A real vector icon (icons.py) replaces the Unicode glyph that used
        # to be the first two characters of the label -- the redesign
        # brief's own "add proper icons instead of text symbols" -- so the
        # label text itself is now plain Greek with no leading symbol.
        for object_name, icon_name, label, index in (
            (NAV_HOME, "home", "Αρχική", 0),
            (NAV_CHAT, "chat", "Συνομιλία", 1),
            (NAV_TASKS, "tasks", "Εργασίες", 2),
            (NAV_FILES, "files", "Αρχεία", 3),
        ):
            layout.addWidget(
                self._build_nav_button(object_name, icon_name, label, index)
            )

        layout.addWidget(self._build_sidebar_divider())
        automations_label = QLabel("ΑΥΤΟΜΑΤΟΠΟΙΗΣΕΙΣ")
        automations_label.setObjectName("sidebarSection")
        layout.addWidget(automations_label)
        # Both route to the same stub page (index 5, see
        # _build_automations_page) -- Jarvis has no concept of a
        # schedulable "automation" distinct from an ordinary skill match
        # today, so "Active"/"History" can't honestly show two different
        # real views yet. Two buttons rather than one is still the right
        # call: it's what the brief's own navigation hierarchy asks for,
        # and the stub page says plainly why there's only one view behind
        # them, rather than silently collapsing the ask down to nothing.
        layout.addWidget(
            self._build_nav_button(
                NAV_AUTOMATIONS_ACTIVE, "automation-active", "Ενεργές", 5
            )
        )
        layout.addWidget(
            self._build_nav_button(
                NAV_AUTOMATIONS_HISTORY, "automation-history", "Ιστορικό", 5
            )
        )

        layout.addStretch(1)
        layout.addWidget(self._build_sidebar_divider())
        layout.addWidget(
            self._build_nav_button(NAV_SETTINGS, "settings", "Ρυθμίσεις", 4)
        )

        self._nav_buttons[NAV_HOME].setChecked(True)
        return sidebar

    def _build_nav_button(
        self, object_name: str, icon_name: str, label: str, index: int
    ) -> QPushButton:
        button = QPushButton(label)
        button.setObjectName(object_name)
        button.setCheckable(True)
        button.setProperty("navButton", True)
        button.setIcon(icons.icon(icon_name, self._text_color()))
        button.setIconSize(QSize(16, 16))
        button.clicked.connect(
            lambda _checked=False, i=index, name=object_name: self._go_to_page(
                name, index=i
            )
        )
        self._nav_buttons[object_name] = button
        self._nav_icon_names[object_name] = icon_name
        return button

    def _build_sidebar_divider(self) -> QFrame:
        divider = QFrame()
        divider.setObjectName("sidebarDivider")
        divider.setFrameShape(QFrame.Shape.HLine)
        return divider

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
        """The redesign brief's central complaint was that this page was too
        much empty floor around a small circle -- "replace the empty center
        with useful information". This version keeps the orb as the page's
        one focal object (bigger now, see _Orb.__init__) but surrounds it
        with real content instead of just a record button: a time-of-day
        greeting at the top, a status word and an idle-only invitation to
        speak directly under the orb, a second "open the full conversation"
        action beside the record button, and a grounded Recent Activity
        panel underneath -- never invented text, only what this session's
        own turns and quick actions actually did (see _log_activity())."""
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        # A QFrame, not a bare QVBoxLayout straight on the page -- a named
        # widget is what the stylesheet's radial-gradient glow (homeGlow,
        # below) needs to paint against; a layout has no rect of its own to
        # paint a background on. The glow is deliberately not drawn by _Orb
        # itself: it needs to extend well past the orb's own bounding box to
        # read as ambient light in the room rather than another ring glued
        # to the avatar, and a QFrame sized to the whole column is what gives
        # it that room.
        center_frame = QFrame()
        center_frame.setObjectName("homeGlow")
        center = QVBoxLayout(center_frame)
        center.setContentsMargins(32, 20, 32, 24)
        center.setSpacing(6)

        greeting = QLabel("")
        greeting.setObjectName(HOME_GREETING_LABEL)
        greeting.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        center.addWidget(greeting)

        center.addStretch(1)

        self._orb = _Orb()
        center.addWidget(self._orb, alignment=Qt.AlignmentFlag.AlignCenter)

        state_label = QLabel(_HOME_STATE_TEXT[State.IDLE])
        state_label.setObjectName(HOME_STATE_LABEL)
        state_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        center.addWidget(state_label)

        # The brief's own example line ("How can I help you?") -- a static
        # invitation, not data, so it carries no risk of inventing anything;
        # hidden outside IDLE by _set_state(), since "how can I help" stops
        # being the useful thing to say once a turn is already under way.
        prompt_label = QLabel("«Πώς μπορώ να βοηθήσω;»")
        prompt_label.setObjectName(HOME_PROMPT_LABEL)
        prompt_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        center.addWidget(prompt_label)

        center.addSpacing(10)

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(12)
        buttons_row.addStretch(1)
        # An icon-only circular button, not a text pill -- "Εγγραφή" next
        # to a microphone glyph was saying the same thing twice. The
        # tooltip keeps the Greek word available on hover (and to a screen
        # reader via its accessible name, which Qt derives from the
        # tooltip when a button has no text), so nothing about what the
        # button *means* is lost by dropping what it *says*.
        record_button = QPushButton()
        record_button.setObjectName(RECORD_BUTTON)
        record_button.setToolTip("Εγγραφή")
        record_button.setIcon(icons.icon("mic", "#ffffff", size=26))
        record_button.setIconSize(QSize(26, 26))
        record_button.setFixedSize(68, 68)
        record_button.clicked.connect(self._start_listening)
        buttons_row.addWidget(record_button)
        # "[ Open Conversation ]" from the brief -- a real action (switches
        # to the already-built Chat page), not a second recording control:
        # this app's architecture has no notion of press-to-start/release-
        # to-stop (listener.listen() blocks until silence is detected, not
        # until a key is released), so "Hold Space to Talk" is answered
        # below as a press-to-start shortcut instead (see keyPressEvent)
        # rather than claimed literally for a gesture this pipeline can't
        # actually do.
        open_chat_button = QPushButton("Άνοιξε Συνομιλία")
        open_chat_button.setObjectName(OPEN_CONVERSATION_BUTTON)
        open_chat_button.setProperty("secondaryAction", True)
        open_chat_button.clicked.connect(
            lambda: self._go_to_page(NAV_CHAT, index=1)
        )
        buttons_row.addWidget(open_chat_button)
        buttons_row.addStretch(1)
        center.addLayout(buttons_row)

        hint_label = QLabel("Πάτησε Space ή κάνε κλικ στην Εγγραφή.")
        hint_label.setObjectName("mutedText")
        hint_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        center.addWidget(hint_label)

        center.addStretch(1)

        center.addWidget(self._build_home_divider())
        center.addWidget(
            _panel_title("Πρόσφατη Δραστηριότητα", accent="#0a84ff")
        )
        activity_list = QListWidget()
        activity_list.setObjectName(RECENT_ACTIVITY_LIST)
        activity_list.setMaximumHeight(120)
        activity_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        activity_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        activity_list.setVisible(False)
        center.addWidget(activity_list)

        # The empty state (redesign brief: "make Recent Activity useful
        # instead of empty -- an empty state with an icon, plus a timeline
        # once there's activity"). Shown at construction (nothing has
        # happened yet this session) and whenever _log_activity() finds the
        # log empty; hidden the moment a real action is logged. An icon
        # plus one muted line, never invented sample data.
        empty_row = QWidget()
        empty_row.setObjectName(RECENT_ACTIVITY_EMPTY_LABEL)
        empty_layout = QHBoxLayout(empty_row)
        empty_layout.setContentsMargins(2, 6, 2, 6)
        empty_layout.setSpacing(8)
        empty_icon = QLabel()
        empty_icon.setPixmap(
            icons.icon("empty-activity", "#8a8a92").pixmap(16, 16)
        )
        empty_layout.addWidget(empty_icon)
        empty_text = QLabel("Καμία δραστηριότητα ακόμα.")
        empty_text.setObjectName("recentActivityEmpty")
        empty_layout.addWidget(empty_text)
        empty_layout.addStretch(1)
        center.addWidget(empty_row)

        # 2:1 rather than the first pass's 3:1 -- that ratio left the side
        # column narrow and the centre column mostly bare floor around a
        # small orb, which is a lot of the "too empty" feedback. Widening the
        # side panels gives the three cards real room instead of a cramped
        # strip, and the orb itself grew (see _Orb.__init__) to fill more of
        # what's left of the centre column.
        layout.addWidget(center_frame, stretch=2)
        layout.addWidget(self._build_home_sidebar(), stretch=1)

        return page

    def _build_home_divider(self) -> QFrame:
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFrameShape(QFrame.Shape.HLine)
        return divider

    def _build_home_sidebar(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 24, 24, 24)
        layout.setSpacing(16)

        # A stretch before, between and after each panel -- not one lump at
        # the bottom -- so the column's empty space is spread evenly across
        # its full height instead of bunching the three cards at the top and
        # leaving one dead zone below Current Task. The panels still size to
        # their own content; only the leftover space moves.
        layout.addStretch(1)
        layout.addWidget(self._build_monitoring_box())
        layout.addStretch(1)
        layout.addWidget(self._build_quick_actions_box())
        layout.addStretch(1)
        layout.addWidget(self._build_current_task_box())
        layout.addStretch(1)

        return panel

    def _build_monitoring_box(self) -> QWidget:
        box = _GlassPanel(self._background)
        box.setObjectName("panel")
        self._glass_panels.append(box)
        # A dynamic property, not a second object name -- `panel` still
        # selects every card's shared shape/surface in `_STYLESHEET`, and
        # `accentColor` layers one more rule on top for the thin top border
        # that gives this card its own identity (blue, here) rather than
        # being identical to Quick Actions/Current Task next to it.
        layout = QVBoxLayout(box)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        layout.addWidget(_panel_title("Κατάσταση Συστήματος", accent="#0a84ff"))

        # A metric label paired with a thin progress bar under it (CPU/RAM/
        # VRAM only -- Network is a rate, not a 0-100 reading, so a bar has
        # nothing honest to show for it and keeps its plain label alone).
        # The bars are the single biggest answer to "boring": a live,
        # moving reading next to the number it describes, instead of four
        # static lines of text that only change value, never shape.
        for object_name, bar_name, placeholder in (
            (CPU_LABEL, CPU_BAR, "CPU: —"),
            (RAM_LABEL, RAM_BAR, "RAM: —"),
            (VRAM_LABEL, VRAM_BAR, "VRAM: —"),
        ):
            label = QLabel(placeholder)
            label.setObjectName(object_name)
            label.setProperty("metric", True)
            layout.addWidget(label)

            bar = QProgressBar()
            bar.setObjectName(bar_name)
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setFixedHeight(4)
            layout.addWidget(bar)

        network_label = QLabel("Network: —")
        network_label.setObjectName(NETWORK_LABEL)
        network_label.setProperty("metric", True)
        layout.addWidget(network_label)

        # "—" is the construction-time placeholder only; _poll_system()
        # overwrites all four labels (and the three bars) with an actual
        # reading on the first timer tick. VRAM can still land on
        # "μη διαθέσιμο" afterwards rather than a percentage -- that is
        # _read_vram_percent() reporting no NVIDIA GPU found, not a stuck
        # placeholder, and its bar is simply left at 0 in that case.
        return box

    def _build_quick_actions_box(self) -> QWidget:
        box = _GlassPanel(self._background)
        box.setObjectName("panel")
        self._glass_panels.append(box)
        outer = QVBoxLayout(box)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(10)
        outer.addWidget(
            _panel_title("Γρήγορες Ενέργειες", accent=_SECONDARY_ACCENT)
        )

        # A two-column grid of compact tiles, not a column of full-width
        # buttons -- the brief's own example lays these out as "YouTube
        # Gmail / Google Calculator" pairs, and a grid is what lets a short
        # panel hold every configured site/app without scrolling. One
        # button per site/app Jarvis can already open by voice
        # (config.SKILL_SITES / SKILL_APPS), so the panel reflects what's
        # really configured rather than a fixed, separately-maintained list
        # that drifts from it -- sites first, then apps, each calling
        # exactly the function the voice skill itself calls
        # (skills._open_site()/_open_app()) via _quick_action_site()/
        # _quick_action_app() below. Neither the grid nor the icon touches
        # button.text(), so QuickActionWiringTests' lookups by exact label
        # and its assertions on what gets clicked are unaffected.
        grid = QGridLayout()
        grid.setSpacing(6)
        icon_size = QSize(10, 10)
        columns = 2
        entries: list[tuple[str, str, object]] = [
            (label, "site", url) for label, url in SKILL_SITES.items()
        ] + [(label, "app", argv) for label, argv in SKILL_APPS.items()]
        for position, (label, kind, payload) in enumerate(entries):
            button = QPushButton(label)
            button.setProperty("quickAction", True)
            button.setProperty("quickActionTile", True)
            button.setIconSize(icon_size)
            # One neutral icon colour for both kinds (not a different
            # accent per button) -- a row of buttons each lit a different
            # colour is a template tell on its own; the icon's *shape*
            # (a globe for a site, a square for an app) already says what
            # kind of action it is, which is what colour was standing in
            # for.
            icon_color = "#8a8a92"
            if kind == "site":
                button.setIcon(icons.icon("site", icon_color))
                button.clicked.connect(
                    lambda _checked=False, label=label, url=payload: (
                        self._quick_action_site(label, url)
                    )
                )
            else:
                button.setIcon(icons.icon("app", icon_color))
                button.clicked.connect(
                    lambda _checked=False, label=label, argv=payload: (
                        self._quick_action_app(label, argv)
                    )
                )
            row, col = divmod(position, columns)
            grid.addWidget(button, row, col)
        outer.addLayout(grid)

        return box

    def _build_current_task_box(self) -> QWidget:
        box = _GlassPanel(self._background)
        box.setObjectName("panel")
        self._glass_panels.append(box)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)
        layout.addWidget(_panel_title("Σήμερα", accent=_ACCENT_TEAL))

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
            self._show_toast(f"Αποτυχία: {label}")
            return
        widget(self, TRANSCRIPT_LIST).addItem(f"Jarvis: Άνοιξα το {label}.")
        self._log_activity(f"Άνοιξε {label}")
        self._show_toast(f"Άνοιξε {label}")

    def _quick_action_app(self, label: str, argv: list) -> None:
        """Same as _quick_action_site(), for a configured local app."""
        try:
            skills._open_app(argv)
        except Exception:
            widget(self, TRANSCRIPT_LIST).addItem(
                f"Jarvis: Δεν μπόρεσα να ανοίξω το {label}."
            )
            self._show_toast(f"Αποτυχία: {label}")
            return
        widget(self, TRANSCRIPT_LIST).addItem(f"Jarvis: Άνοιξα το {label}.")
        self._log_activity(f"Άνοιξε {label}")
        self._show_toast(f"Άνοιξε {label}")

    def _log_activity(self, label: str) -> None:
        """Appends one line (timestamped) to the Home page's Recent Activity
        panel -- grounded in real actions this GUI just took, never
        invented: the audit log already records which skill matched and
        why, but not which site/app or what was actually said, so this is
        its own small, session-only log rather than a reach into audit for
        detail it was never built to hold. Cleared on restart, which is
        honest -- it only ever claims "this happened in this session",
        never a history beyond that."""
        self._recent_activity.insert(0, (datetime.now().strftime("%H:%M"), label))
        del self._recent_activity[5:]
        activity_list = widget(self, RECENT_ACTIVITY_LIST)
        activity_list.clear()
        for stamp, text in self._recent_activity:
            activity_list.addItem(f"{stamp}   {text}")

        # The empty state (an icon plus a muted line) and the populated
        # list are mutually exclusive -- never both, never neither. Per the
        # redesign brief's own "make Recent Activity useful instead of
        # empty (an empty state with an icon, plus a timeline once there's
        # activity)".
        has_activity = bool(self._recent_activity)
        activity_list.setVisible(has_activity)
        widget(self, RECENT_ACTIVITY_EMPTY_LABEL).setVisible(not has_activity)

    def _show_toast(self, message: str, kind: str = "info") -> None:
        """A transient notification card, floating over whichever page is
        showing -- the redesign brief's "toast notifications", used for
        things that already show up elsewhere (the transcript, Recent
        Activity) but are easy to miss if the user isn't looking right at
        that panel: a quick action that opened something, or one that
        failed.

        `kind` is accepted but not yet used to vary styling -- the
        stylesheet's single `#toast` rule (accent-bordered, same surface
        tone as everything else) covers every case so far; a distinct
        "error" treatment is a reasonable next step once there's a second
        kind of toast that actually needs one, not invented ahead of that."""
        toast = QFrame(self)
        toast.setObjectName("toast")
        layout = QHBoxLayout(toast)
        layout.setContentsMargins(14, 10, 14, 10)
        label = QLabel(message)
        label.setObjectName("toastLabel")
        label.setWordWrap(True)
        layout.addWidget(label)
        toast.setMaximumWidth(320)
        toast.adjustSize()

        margin = 20
        stack_offset = sum(t.height() + 8 for t in self._active_toasts)
        toast.move(
            self.width() - toast.width() - margin,
            56 + margin + stack_offset,
        )
        toast.show()
        toast.raise_()

        effect = QGraphicsOpacityEffect(toast)
        toast.setGraphicsEffect(effect)
        effect.setOpacity(0.0)
        fade_in = QPropertyAnimation(effect, b"opacity", toast)
        fade_in.setDuration(200)
        fade_in.setStartValue(0.0)
        fade_in.setEndValue(1.0)
        fade_in.start()
        # Kept as attributes on the toast itself so neither animation is
        # garbage-collected mid-flight -- same reasoning _Orb.set_state()
        # already follows for its own QVariantAnimation.
        toast._fade_in = fade_in  # type: ignore[attr-defined]
        self._active_toasts.append(toast)

        def _dismiss() -> None:
            fade_out = QPropertyAnimation(effect, b"opacity", toast)
            fade_out.setDuration(200)
            fade_out.setStartValue(1.0)
            fade_out.setEndValue(0.0)
            fade_out.finished.connect(lambda: self._remove_toast(toast))
            fade_out.start()
            toast._fade_out = fade_out  # type: ignore[attr-defined]

        QTimer.singleShot(2500, _dismiss)

    def _remove_toast(self, toast: QFrame) -> None:
        if toast in self._active_toasts:
            self._active_toasts.remove(toast)
        toast.deleteLater()

    def _open_command_palette(self) -> None:
        """Ctrl+K (redesign brief, "Extras") -- a lightweight QDialog
        listing every page and every configured quick action, filtered as
        you type. Built fresh on each open rather than kept alive hidden:
        it's a handful of widgets and SKILL_SITES/SKILL_APPS entries, cheap
        enough that there's no reason to manage its lifetime across opens,
        the same reasoning _CommandPalette's own commands are rebuilt from
        the *current* config.SKILL_SITES/SKILL_APPS every time rather than
        cached at construction."""
        palette = _CommandPalette(self)
        center = self.geometry().center()
        palette.move(center.x() - palette.width() // 2, self.geometry().top() + 120)
        palette.show()
        palette.raise_()

    # --- Chat page ------------------------------------------------------------

    def _build_chat_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(_page_title("Συνομιλία"))

        # _ChatTranscriptList + _ChatBubbleDelegate turn the plain stacked
        # list into left/right message bubbles (the user's turns in the
        # accent colour on the right, Jarvis's in a neutral tone on the
        # left) -- the delegate paints against each item's own text, so
        # nothing about what gets stored in a row (see _on_listen_finished(),
        # _on_reply_finished(), _append_debug_line(), the quick-action
        # handlers) changes at all; only how it's drawn does.
        transcript_list = _ChatTranscriptList()
        transcript_list.setObjectName(TRANSCRIPT_LIST)
        transcript_list.setItemDelegate(_ChatBubbleDelegate(transcript_list))
        transcript_list.setSelectionMode(
            QListWidget.SelectionMode.NoSelection
        )
        transcript_list.setVerticalScrollMode(
            QListWidget.ScrollMode.ScrollPerPixel
        )
        transcript_list.setSpacing(0)
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

    # --- Automations page (stub -- see _build_sidebar) -------------------------

    def _build_automations_page(self) -> QWidget:
        """A stub, the same honest shape as _build_files_page() above: the
        redesign brief's own sidebar hierarchy asks for an "Automations"
        section (Active/History), but Jarvis has no concept today of a
        schedulable, start/stoppable "automation" as its own object -- a
        skill match is one audit row, not a thing with a lifecycle. Rather
        than invent fake automation data to match the mockup, this names
        what's missing and points at what already exists that's closest to
        it: the scheduler's reminders and timers, visible today on the
        Tasks page and in the spoken agenda (see "Scheduler" in CLAUDE.md).
        Both sidebar buttons (Ενεργές/Ιστορικό) route here, since there is
        only one honest view to show either way."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(_page_title("Αυτοματοποιήσεις"))
        placeholder = QLabel(
            "Δεν υπάρχει ακόμα μια ξεχωριστή έννοια \"αυτοματοποίησης\" στο "
            "Jarvis -- οι υπενθυμίσεις και τα χρονόμετρα του scheduler "
            "(ορατά στη σελίδα Εργασίες) είναι το πιο κοντινό σημερινό "
            "αντίστοιχο."
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

        # The one live setting on this otherwise read-only page -- see
        # _apply_theme()'s own docstring for why changing this is safe
        # against the project's "never edit .env" rule (it changes nothing
        # .env owns, only how this window paints itself).
        theme_box = QFrame()
        theme_box.setObjectName("panel")
        theme_layout = QVBoxLayout(theme_box)
        theme_layout.setContentsMargins(18, 16, 18, 16)
        theme_layout.setSpacing(10)
        theme_layout.addWidget(_panel_title("Εμφάνιση", accent=theme.ACCENTS["blue"]))

        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("Θέμα"))
        theme_combo = QComboBox()
        theme_combo.setObjectName(THEME_COMBO)
        theme_combo.addItem("Σκοτεινό", "dark")
        theme_combo.addItem("Ανοιχτό", "light")
        theme_combo.setCurrentIndex(0 if self._theme_mode == "dark" else 1)
        theme_combo.currentIndexChanged.connect(
            lambda _i: self._apply_theme(mode=theme_combo.currentData())
        )
        theme_row.addWidget(theme_combo)
        theme_row.addStretch(1)
        theme_layout.addLayout(theme_row)

        accent_row = QHBoxLayout()
        accent_row.addWidget(QLabel("Χρώμα έμφασης"))
        accent_combo = QComboBox()
        accent_combo.setObjectName(ACCENT_COMBO)
        accent_names = {"blue": "Μπλε", "violet": "Μοβ", "teal": "Τιρκουάζ"}
        for choice in theme.ACCENT_CHOICES:
            accent_combo.addItem(accent_names.get(choice, choice), choice)
        accent_combo.setCurrentIndex(list(theme.ACCENT_CHOICES).index(self._accent))
        accent_combo.currentIndexChanged.connect(
            lambda _i: self._apply_theme(accent=accent_combo.currentData())
        )
        accent_row.addWidget(accent_combo)
        accent_row.addStretch(1)
        theme_layout.addLayout(accent_row)

        layout.addWidget(theme_box)

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
        summary = reply if len(reply) <= 40 else reply[:37] + "…"
        self._log_activity(summary)
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


# The window's QSS is built by jarvis/gui/theme.py's build_stylesheet(),
# parametrized on the live theme mode/accent (see MainWindow._apply_theme())
# rather than kept here as a single static string -- see that module's own
# docstring for the full token table and why it replaced this file's old
# hand-kept _STYLESHEET constant.
