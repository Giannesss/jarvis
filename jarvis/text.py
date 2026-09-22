"""Shared Greek text handling: normalization and the phrase data several
modules need.

This lives here rather than in skills.py because memory.py and policy.py need
the same normalization, and skills.py imports *them* to register their
handlers -- importing back the other way would be circular. skills.py
re-exports everything here under its old names, so its existing callers and
tests are unaffected.

Normalization is accent-insensitive, case-insensitive, folds the final sigma,
and folds the iotacism vowels (see fold_iotacism), because the input is
speech-to-text output that rarely matches a written phrase exactly.

Because normalize() rewrites letters, no literal compared against its output
may be written by hand in its normalized form -- put it through normalize()
(or phrases()) at import time instead, and spell it naturally. Module-level
constants here and in skills.py and memory.py all do that.
"""

from __future__ import annotations

import re
import unicodedata

# Stripped before whole-utterance comparisons, since Whisper punctuates what
# it transcribes ("Τέλος." / "Τέλος;").
PUNCTUATION = str.maketrans("", "", ".,;:!?…«»\"'")

# --- Iotacism --------------------------------------------------------------
#
# Modern Greek pronounces η, ι, υ, ει, οι and υι identically, as /i/. Whisper
# transcribes what it *hears*, so it picks whichever spelling its language
# model prefers: a real hand test produced "Θυμίσου ότι με λένε Γιάννη" for a
# spoken "Θυμήσου", which missed memory's trigger pattern and fell through to
# the brain, so nothing was saved. Folding them all to ι makes every spelling
# of a sound compare equal, for triggers, phrase lists, stored search keys and
# stems alike.
#
# Three digraphs must survive: ου is /u/, and the υ in αυ/ευ/ηυ is a
# consonant (/v/, /f/). Folding those would merge genuinely different sounds
# -- "που" would become "πι". They are matched first and passed through.
#
# The fold is lossy but pronunciation-preserving by construction, so TTS says
# a folded word exactly as it would say the original.
_IOTACISM_KEEP = ("ου", "αυ", "ευ", "ηυ")
_IOTACISM_FOLD = {"ει": "ι", "οι": "ι", "υι": "ι", "η": "ι", "υ": "ι"}

# Alternation order is the rule: the kept digraphs first, then the folded
# digraphs, then single letters, so the longest match at each position wins
# and "ου" is consumed before its "υ" can be folded on its own.
_IOTACISM_RE = re.compile("|".join((*_IOTACISM_KEEP, *_IOTACISM_FOLD)))


def fold_iotacism(text: str) -> str:
    """Fold the /i/ vowels of accent-stripped Greek onto ι. Idempotent, and
    a no-op on ASCII -- which is what makes it safe to apply to a regular
    expression's source text (see memory._re)."""
    return _IOTACISM_RE.sub(
        lambda m: _IOTACISM_FOLD.get(m.group(), m.group()), text
    )


def strip_accents(text: str) -> str:
    r"""Drop the tonos and the diaeresis, leaving the letters alone.

    Split out of normalize() because memory._re() needs it without the
    lower-casing: a regex source must keep its case ("\S" is not "\s"),
    but an accent written into a Greek pattern would otherwise stop it from
    ever matching normalized input.

    The diaeresis goes too, which is a small, pre-existing loss: it marks a
    vowel pair as two sounds ("θεϊκός"), exactly what fold_iotacism then
    folds as if it were one.
    """
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", text)


def normalize(text: str) -> str:
    text = strip_accents(text.strip().lower())
    return fold_iotacism(text.replace("ς", "σ"))


def phrases(*items: str) -> list[str]:
    """Normalize a literal phrase list at import time.

    Every phrase compared against normalized speech goes through this, so the
    source can spell things naturally ("κλείσε", "Τέλος Τζάρβις") and stay
    correct when normalize() changes.
    """
    return [normalize(item) for item in items]


def strip_punctuation(text: str) -> str:
    """For whole-utterance comparisons, where trailing punctuation from
    Whisper would otherwise defeat an exact match."""
    return text.translate(PUNCTUATION).strip()


# Quitting Jarvis entirely. Here rather than in skills.py because policy.py
# needs them too: "κλείσε" has to keep working while Jarvis is frozen.
SHUTDOWN_PHRASES = phrases("κλείσε", "τερματισμός", "τερμάτισε")

# Spoken numbers, used by the timer skill and by memory's relative-time
# reminders ("σε δύο ώρες"). No composition: "είκοσι πέντε" is not 25.
# Keys are normalized, so "δύο" is looked up as "διο".
NUMBER_WORDS = {
    normalize(word): value
    for word, value in {
        "ένα": 1, "μία": 1,
        "δύο": 2, "τρία": 3, "τέσσερα": 4, "πέντε": 5,
        "έξι": 6, "εφτά": 7, "επτά": 7, "οχτώ": 8, "οκτώ": 8,
        "εννέα": 9, "εννιά": 9, "δέκα": 10,
        "έντεκα": 11, "δώδεκα": 12,
        "δεκατρία": 13, "δεκατέσσερα": 14, "δεκαπέντε": 15,
        "δεκαέξι": 16, "δεκαεφτά": 17, "δεκαεπτά": 17,
        "δεκαοχτώ": 18, "δεκαοκτώ": 18, "δεκαεννέα": 19, "δεκαεννιά": 19,
        "είκοσι": 20, "τριάντα": 30, "σαράντα": 40, "πενήντα": 50,
        "εξήντα": 60,
    }.items()
}
