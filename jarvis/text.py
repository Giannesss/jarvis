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


def normalize_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """normalize(text), plus where each output character came from.

    `spans[i]` is the `(start, end)` slice of `text` that produced
    `norm[i]`, so a pattern can match against the normalized string and the
    caller can still recover what the user actually said:

        norm, spans = normalize_spans(text)
        start, end = match.span("value")
        raw = text[spans[start][0]:spans[end - 1][1]]     # "Γιάννης"

    That indirection is needed because normalize() is not length-preserving.
    Lower-casing, accent stripping and the final sigma are all one character
    in, one out; fold_iotacism is not, since ει/οι/υι each collapse to a
    single ι. A normalized index is therefore not an index into the original
    text, which is what made every captured value read back folded (see
    memory.Norm).

    Kept separate from normalize() rather than replacing it: normalize() is
    called on every phrase list at import and on every utterance, and has no
    use for the map. test_text.py asserts the two never disagree.
    """
    offset = len(text) - len(text.lstrip())
    stripped = text.strip()

    # Done per character rather than over the whole string so the map stays
    # honest even where a step is not 1:1 -- a decomposed input, where
    # stripping a combining mark deletes a character outright.
    soft: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(stripped):
        piece = strip_accents(char.lower()).replace("ς", "σ")
        soft.extend(piece)
        origin.extend([offset + index] * len(piece))

    source = "".join(soft)
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    position = 0

    for match in _IOTACISM_RE.finditer(source):
        for i in range(position, match.start()):
            out.append(source[i])
            spans.append((origin[i], origin[i] + 1))

        replacement = _IOTACISM_FOLD.get(match.group(), match.group())
        if len(replacement) == 1:
            # A fold: one character now stands for everything the match
            # covered, so its span covers all of it -- that is what lets
            # "Γιάννης" be recovered from the ι that its "η" folded into.
            out.append(replacement)
            spans.append((origin[match.start()], origin[match.end() - 1] + 1))
        else:
            # A kept digraph (ου, αυ, ευ, ηυ) passing through unchanged.
            for shift, char in enumerate(replacement):
                i = match.start() + shift
                out.append(char)
                spans.append((origin[i], origin[i] + 1))
        position = match.end()

    for i in range(position, len(source)):
        out.append(source[i])
        spans.append((origin[i], origin[i] + 1))

    return "".join(out), spans


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance: how many single-character insertions, deletions
    or substitutions separate two strings.

    Here for memory's fuzzy trigger matching, which has to decide whether a
    spoken verb ending is a mangling of one it knows. An edit *count* rather
    than difflib's similarity ratio, because the ratio is length-normalized
    and rewards a long clean prefix -- on the real cases it scores every false
    trigger above the true one:

        κρατάω  vs κράτα     ratio 0.909, distance 1   (false)
        θυμάσαι vs ναθυμάσαι ratio 0.875, distance 2   (false)
        σημειώσεις vs σημείωσε ratio 0.800, distance 2 (false)
        θυμήσω  vs θυμήσου   ratio 0.769, distance 2   (true)

    No ratio cutoff separates those; a per-phrase edit budget does, and reads
    as "one character" rather than as a tuned constant.

    Two rows rather than a full matrix -- these are single Greek words.
    """
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(
                min(
                    previous[j] + 1,                     # drop char_a
                    current[j - 1] + 1,                  # add char_b
                    previous[j - 1] + (char_a != char_b),  # substitute
                )
            )
        previous = current
    return previous[-1]


# --- Fuzzy word matching ---------------------------------------------------
#
# Whisper reproduces the stressed *stem* of a word and improvises the
# unstressed ending, and it inserts word breaks and commas that were never
# spoken. Two callers have to survive both: memory's save triggers
# (memory.fuzzy_trigger) and the kill switch (policy.fuzzy_kill). The shared
# shape is "this stem, exactly, then an ending within a small budget", so it
# lives here beside edit_distance rather than in either of them.

SEPARATORS = " ,.·"


def match_core(norm: str, core: str, start: int = 0) -> int | None:
    """Index just past an exact match of `core` at `start`, or None.

    Separators are free on both sides -- skipped in the input, ignored in the
    core -- so a word the recognizer split ("θυμ ήσου", "στα μάτα") still
    matches, and a multi-word core ("να θυμ", "τα πάντα") needs no separator
    pattern of its own.
    """
    i = start
    for char in core:
        if char in SEPARATORS:
            continue
        while i < len(norm) and norm[i] in SEPARATORS:
            i += 1
        if i >= len(norm) or norm[i] != char:
            return None
        i += 1
    return i


def fuzzy_word(
    norm: str,
    core: str,
    endings: tuple[str, ...],
    budget: int,
    start: int = 0,
) -> int | None:
    """Index just past a word whose stem is `core` exactly and whose ending is
    within `budget` edits of one of `endings`. None if it is neither.

    The stem is the guard and carries no budget at all; only the ending is
    guessed. The returned index sits at the word boundary, never inside a
    word: the ending is taken whole to the next separator rather than capped
    at some length, so a long word simply scores far past its budget instead
    of being sliced in half.
    """
    after_core = match_core(norm, core, start)
    if after_core is None:
        return None

    end = after_core
    while end < len(norm) and norm[end] not in SEPARATORS:
        end += 1

    tail = norm[after_core:end]
    if min(edit_distance(tail, ending) for ending in endings) > budget:
        return None
    return end


def skip_separators(norm: str, start: int) -> int:
    """Index of the next non-separator at or after `start`."""
    i = start
    while i < len(norm) and norm[i] in SEPARATORS:
        i += 1
    return i


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

# Confirming an action, spoken aloud (policy.py) or typed at the CLI (mem.py).
# Deliberately short: each entry must be an answer on its own, since is_yes()
# compares the whole utterance rather than searching inside it. "εντάξει" is
# here because it is how a yes actually sounds in Greek speech; "σωστά" is
# not, since "that's correct" is not the same as "go ahead".
YES_WORDS = set(phrases("ν", "ναι", "y", "yes", "ok", "οκ", "εντάξει"))


def is_yes(answer: str) -> bool:
    """True only for an explicit yes. Anything else -- a mangled word, a whole
    sentence, an empty string where the user said nothing -- is a no.

    The burden is on the yes because the callers are confirming something
    irreversible: a deleted row, or an action the policy layer has decided
    needs asking about first.

    Punctuation is stripped before the comparison. Whisper punctuates what it
    hears, so a spoken "Ναι." arrives with the full stop attached and would
    otherwise miss every entry in YES_WORDS -- the same trap
    CONVERSATION_END_EXACT handles for "Τέλος." (see CLAUDE.md
    "Punctuation is *not* normalized away").
    """
    return strip_punctuation(normalize(answer or "")) in YES_WORDS

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
