"""Simple Greek voice commands, handled locally without the LLM.

Matching is loose on purpose: accent-insensitive, case-insensitive, and
tolerant of extra words, since the input text comes from speech recognition
and rarely matches a command phrase exactly. See CLAUDE.md "Skills".
"""

from __future__ import annotations

import subprocess
import threading
import time
import webbrowser
from datetime import datetime

from jarvis import db, memory, speaker, text
from jarvis.config import SKILL_APPS, SKILL_SITES

# Re-exported from jarvis/text.py, which owns them now so memory.py and
# policy.py can share them without importing this module back. Kept under
# their original names here for existing callers and tests.
SHUTDOWN_PHRASES = text.SHUTDOWN_PHRASES
_NUMBER_WORDS = text.NUMBER_WORDS

# Leaving conversation mode (see main.py) — distinct from SHUTDOWN_PHRASES,
# which quit Jarvis entirely. These only send it back to waiting for the
# wake word.
# Every phrase list here is built with text.phrases(), which normalizes each
# entry at import time. That is not optional bookkeeping: normalize() folds
# the final ς, strips accents *and* folds the iotacism vowels, so a phrase
# written out in its normalized form by hand ("τελοσ", "ανοιξε") would stop
# matching the moment normalize() changed. Spell them naturally instead.
CONVERSATION_END_PHRASES = text.phrases("τέλος Τζάρβις", "αντίο Τζάρβις")

# Matched only as the whole utterance, unlike everything else in this file:
# as a substring, "τέλος" would end the conversation on an ordinary sentence
# like "στο τέλος της μέρας".
CONVERSATION_END_EXACT = text.phrases("τέλος")

_PUNCTUATION = text.PUNCTUATION
TIME_PHRASES = text.phrases("τι ώρα", "ποια ώρα", "πες μου την ώρα")
DATE_PHRASES = text.phrases(
    "τι ημερομηνία",
    "ποια ημερομηνία",
    "τι μέρα είναι",
    "ποια μέρα είναι",
    "τι μέρα έχουμε",
)
OPEN_VERB = text.normalize("άνοιξε")
TIMER_KEYWORD = text.normalize("χρονόμετρο")

# Reading memory back on request (jarvis/memory.py). Saving has no phrase
# list here: memory.parse() owns those triggers, since it has to tell
# "θυμήσου ότι..." apart from "υπενθύμισέ μου σε δύο ώρες...".
# Substring-matched, so an entry that contains another is dead weight:
# bare "θυμάσαι" already covers "τι θυμάσαι" and "θυμάσαι αν", and "τι έχεις"
# covers "τι έχεις για". Only the shortest form of each is listed.
#
# Two deliberate omissions. Bare "ξέρεις" would swallow ordinary questions
# ("ξέρεις τι ώρα είναι"), so only "τι ξέρεις" is here. "θύμισέ μου" is worse
# than it looks: normalized it is "θιμισε μου", a substring of the reminder
# trigger "υπενθύμισέ μου", so every reminder would match it.
#
# Bare "θυμάσαι" does overlap RE_TRIGGER's "να θυμάσαι ότι…" save form. That
# is safe only because handle() runs _handle_memory_save before
# _handle_memory_recall -- an ordering this list now depends on.
MEMORY_RECALL_PHRASES = text.phrases(
    "θυμάσαι",
    "τι ξέρεις",
    "τι έχεις",
    "τι σου είπα",
    "τι μου είπες",
    "σου είχα πει",
)
MEMORY_RECALL_LIMIT = 3  # spoken aloud, so a handful at most

# What Jarvis says back once something is stored, per table.
_SAVE_REPLIES = {
    "reminders": "Εντάξει, θα σου το θυμίσω.",
    "exams": "Το σημείωσα στις εξετάσεις σου.",
    "courses": "Το σημείωσα στα μαθήματά σου.",
    "profile": "Εντάξει, το θυμάμαι.",
    "businesses": "Το σημείωσα για την επιχείρησή σου.",
    "notes": "Το θυμάμαι.",
}

# Stems checked in this order: "δευτερολεπτ" must come before "λεπτ" since
# "δευτερόλεπτο" (second) contains "λεπτ" as a substring. The stems are
# normalized like every other phrase here; the labels are not, since they are
# spoken back rather than matched.
_TIMER_UNITS = [
    (text.normalize("δευτερόλεπτ"), 1, "δευτερόλεπτα"),
    (text.normalize("λεπτ"), 60, "λεπτά"),
    (text.normalize("ωρ"), 3600, "ώρες"),
]

# Set by the shutdown skill; main.py checks this after speaking the reply
# and breaks its loop, since handle() itself only ever returns str | None.
shutdown_requested = False


_normalize = text.normalize


def _find_match(norm_text: str, mapping: dict[str, list[str]] | dict[str, str]):
    """Return (key, value) for the first mapping key whose normalized form
    appears in norm_text, so config.py can spell keys naturally (accents,
    capitals) and still match a normalized transcription."""
    for key, value in mapping.items():
        if _normalize(key) in norm_text:
            return key, value
    return None


def _handle_shutdown(norm_text: str) -> str | None:
    global shutdown_requested
    if not any(p in norm_text for p in SHUTDOWN_PHRASES):
        return None
    shutdown_requested = True
    return "Αντίο!"


def _handle_time(norm_text: str) -> str | None:
    if not any(p in norm_text for p in TIME_PHRASES):
        return None
    return f"Η ώρα είναι {datetime.now().strftime('%H:%M')}."


def _handle_date(norm_text: str) -> str | None:
    if not any(p in norm_text for p in DATE_PHRASES):
        return None
    return f"Σήμερα είναι {datetime.now().strftime('%d/%m/%Y')}."


def _extract_number(tokens: list[str], unit_index: int) -> int | None:
    """Pick the number token closest to (and at or before) the unit token,
    e.g. in "βάλε ένα χρονόμετρο για δύο λεπτά" this picks "δύο" over "ένα"
    since "ένα" belongs to "χρονόμετρο", not the duration."""
    best_value = None
    best_distance = None
    for i, token in enumerate(tokens):
        if token.isdigit():
            value = int(token)
        elif token in _NUMBER_WORDS:
            value = _NUMBER_WORDS[token]
        else:
            continue

        distance = unit_index - i
        if distance < 0:
            continue
        if best_distance is None or distance < best_distance:
            best_value, best_distance = value, distance

    return best_value


def _extract_unit(tokens: list[str]) -> tuple[int, int, str] | None:
    for i, token in enumerate(tokens):
        for stem, multiplier, label in _TIMER_UNITS:
            if stem in token:
                return i, multiplier, label
    return None


def _start_timer(seconds: int, number: int, label: str) -> None:
    def _on_finish() -> None:
        message = f"Το χρονόμετρο των {number} {label} τελείωσε!"
        print(f"[timer] {message}")
        try:
            # speaker.speak() is serialized by a lock, so this waits its
            # turn instead of overlapping if Jarvis is already talking.
            speaker.speak(message)
        except Exception as e:
            print(f"Σφάλμα εκφώνησης χρονομέτρου: {e}")

    timer = threading.Timer(seconds, _on_finish)
    # Daemon: quitting Jarvis with a timer still pending shouldn't hang the
    # process waiting for it to fire.
    timer.daemon = True
    timer.start()


def _handle_timer(norm_text: str) -> str | None:
    if TIMER_KEYWORD not in norm_text:
        return None

    tokens = norm_text.split()
    unit = _extract_unit(tokens)
    if unit is None:
        return "Δεν κατάλαβα για πόση ώρα να βάλω το χρονόμετρο."

    unit_index, multiplier, label = unit
    number = _extract_number(tokens, unit_index)
    if number is None:
        return "Δεν κατάλαβα για πόση ώρα να βάλω το χρονόμετρο."

    _start_timer(number * multiplier, number, label)
    return f"Ξεκίνησε το χρονόμετρο για {number} {label}."


def _handle_open(norm_text: str) -> str | None:
    if OPEN_VERB not in norm_text:
        return None

    match = _find_match(norm_text, SKILL_SITES)
    if match is not None:
        key, url = match
        webbrowser.open(url)
        return f"Άνοιξα το {key}."

    match = _find_match(norm_text, SKILL_APPS)
    if match is not None:
        key, argv = match
        subprocess.Popen(argv)
        return f"Άνοιξα το {key}."

    return None


def _handle_memory_save(raw_text: str) -> str | None:
    """Takes the raw utterance, not the normalized one: a note is stored the
    way it was said, accents and capitals included."""
    parsed = memory.parse(raw_text)
    if parsed is None:
        return None  # not a save request; let the brain have it

    if parsed.table == memory.REJECTED:
        return "Δεν αποθηκεύω κωδικούς ή αριθμούς κάρτας."

    try:
        conn = db.connect()
        try:
            memory.save(parsed, conn)
        finally:
            conn.close()
    except Exception as e:
        print(f"Σφάλμα μνήμης: {e}")
        return "Δεν μπόρεσα να το αποθηκεύσω."

    return _SAVE_REPLIES.get(parsed.table, "Το θυμάμαι.")


def _handle_memory_recall(raw_text: str) -> str | None:
    norm = _normalize(raw_text)
    if not any(phrase in norm for phrase in MEMORY_RECALL_PHRASES):
        return None

    try:
        conn = db.connect()
        try:
            # "Τι θυμάσαι;" on its own leaves no searchable token, so there
            # is nothing to match and an empty result would mean "I found
            # nothing" for a search that never ran. Say what is on file.
            if not memory.stems_of(raw_text):
                return memory.spoken_profile(conn)
            hits = memory.search(raw_text, conn)
        finally:
            conn.close()
    except Exception as e:
        print(f"Σφάλμα μνήμης: {e}")
        return "Δεν μπόρεσα να δω τη μνήμη μου."

    if not hits:
        return "Δεν θυμάμαι κάτι σχετικό."

    # Spoken aloud, so a handful at most.
    return " ".join(hits[:MEMORY_RECALL_LIMIT])


def is_conversation_end(text: str) -> bool:
    """True if text asks to leave conversation mode (not to shut down).

    Normalized the same way as every other phrase here, so it's
    accent- and case-insensitive; see CONVERSATION_END_EXACT for why bare
    "τέλος" is treated more strictly than the rest."""
    norm = _normalize(text)

    if any(phrase in norm for phrase in CONVERSATION_END_PHRASES):
        return True

    return norm.translate(_PUNCTUATION).strip() in CONVERSATION_END_EXACT


def handle(text: str) -> str | None:
    norm = _normalize(text)

    t0 = time.perf_counter()
    # Shutdown stays first so "κλείσε" can never be intercepted. The two
    # memory handlers take the raw text, not norm: a note is stored the way
    # it was said, accents and capitals included.
    reply = (
        _handle_shutdown(norm)
        or _handle_memory_save(text)
        or _handle_memory_recall(text)
        or _handle_timer(norm)
        or _handle_time(norm)
        or _handle_date(norm)
        or _handle_open(norm)
    )

    if reply is not None:
        print(f"[timing] Skill match: {time.perf_counter() - t0:.3f}s")

    return reply
