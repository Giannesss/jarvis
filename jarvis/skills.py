"""Simple Greek voice commands, handled locally without the LLM.

Matching is loose on purpose: accent-insensitive, case-insensitive, and
tolerant of extra words, since the input text comes from speech recognition
and rarely matches a command phrase exactly. See CLAUDE.md "Skills".
"""

from __future__ import annotations

import subprocess
import threading
import time
import unicodedata
import webbrowser
from datetime import datetime

from jarvis import speaker
from jarvis.config import SKILL_APPS, SKILL_SITES

SHUTDOWN_PHRASES = ["κλεισε", "τερματισμος", "τερματισε"]
TIME_PHRASES = ["τι ωρα", "ποια ωρα", "πες μου την ωρα"]
DATE_PHRASES = [
    "τι ημερομηνια",
    "ποια ημερομηνια",
    "τι μερα ειναι",
    "ποια μερα ειναι",
    "τι μερα εχουμε",
]
OPEN_VERB = "ανοιξε"
TIMER_KEYWORD = "χρονομετρο"

# Stems checked in this order: "δευτερολεπτ" must come before "λεπτ" since
# "δευτερόλεπτο" (second) contains "λεπτ" as a substring.
_TIMER_UNITS = [
    ("δευτερολεπτ", 1, "δευτερόλεπτα"),
    ("λεπτ", 60, "λεπτά"),
    ("ωρ", 3600, "ώρες"),
]

_NUMBER_WORDS = {
    "ενα": 1, "μια": 1,
    "δυο": 2, "τρια": 3, "τεσσερα": 4, "πεντε": 5,
    "εξι": 6, "εφτα": 7, "επτα": 7, "οχτω": 8, "οκτω": 8,
    "εννεα": 9, "εννια": 9, "δεκα": 10,
    "εντεκα": 11, "δωδεκα": 12,
    "δεκατρια": 13, "δεκατεσσερα": 14, "δεκαπεντε": 15,
    "δεκαεξι": 16, "δεκαεφτα": 17, "δεκαεπτα": 17,
    "δεκαοχτω": 18, "δεκαοκτω": 18, "δεκαεννεα": 19, "δεκαεννια": 19,
    "εικοσι": 20, "τριαντα": 30, "σαραντα": 40, "πενηντα": 50, "εξηντα": 60,
}

# Set by the shutdown skill; main.py checks this after speaking the reply
# and breaks its loop, since handle() itself only ever returns str | None.
shutdown_requested = False


def _normalize(text: str) -> str:
    text = text.strip().lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = unicodedata.normalize("NFC", text)
    return text.replace("ς", "σ")


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


def handle(text: str) -> str | None:
    norm = _normalize(text)

    t0 = time.perf_counter()
    reply = (
        _handle_shutdown(norm)
        or _handle_timer(norm)
        or _handle_time(norm)
        or _handle_date(norm)
        or _handle_open(norm)
    )

    if reply is not None:
        print(f"[timing] Skill match: {time.perf_counter() - t0:.3f}s")

    return reply
