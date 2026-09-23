"""Persistent memory: turning spoken Greek into rows, and rows back into
context for the brain. See CLAUDE.md "Memory".

Parsing is rule-based and entirely offline -- no LLM sits in the write path,
so what gets stored is deterministic and testable. The patterns are ordered
into a ladder (see parse()); the last rung is an unconditional fallback to a
plain note, so a save request is never silently discarded and never guessed
into the wrong table.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from jarvis import db
from jarvis.config import MEMORY_TAGS, MEMORY_TOKEN_BUDGET
from jarvis.text import (
    NUMBER_WORDS,
    fold_iotacism,
    fuzzy_word,
    normalize,
    normalize_spans,
    skip_separators,
    strip_accents,
)


def _re(pattern: str) -> re.Pattern:
    r"""Compile a pattern that will be matched against normalize()d text.

    Nearly every pattern in this module is, so nearly every one goes through
    here. The pattern is written in ordinary Greek and folded the same way
    its input is (see text.fold_iotacism): "θυμήσου" in the source matches
    whichever of θυμήσου / θυμίσου / θυμύσου / θυμείσου Whisper happened to
    transcribe. Folding the *source* rather than hand-writing "θιμισου"
    keeps these readable -- and it is safe, because neither step touches a
    regex metacharacter, all of which are ASCII. Case is deliberately left
    alone, unlike in normalize(): "\S" must not become "\s".
    """
    return re.compile(fold_iotacism(strip_accents(pattern)))


@dataclass(frozen=True)
class Parsed:
    """What parse() decided. `table` is "rejected" when the text contained
    something we refuse to store."""

    table: str
    fields: dict = field(default_factory=dict)


REJECTED = "rejected"


@dataclass(frozen=True)
class Norm:
    """A normalized utterance that remembers where each character came from.

    Patterns match against `.norm`, exactly as they always have. What changed
    is what a capture yields: `.group()` reads the match back out of the
    *original* text, so a display column stores what the user actually said
    rather than the folded form the pattern matched.

    This is needed because normalize() is not length-preserving -- ει/οι/υι
    each fold to a single ι -- so a normalized index is not an index into the
    raw text, and the two cannot just be sliced in parallel. Before this,
    "με λένε Γιάννης" stored the name as "γιαννισ" and recall spoke it that
    way.

    Slicing returns another Norm over the same `raw`, so a capture taken
    after the trigger has been stripped still points into the original.
    """

    raw: str
    norm: str
    spans: tuple[tuple[int, int], ...]

    @classmethod
    def of(cls, text: str) -> "Norm":
        norm, spans = normalize_spans(text)
        return cls(text, norm, tuple(spans))

    def slice(self, start: int, end: int | None = None) -> "Norm":
        end = len(self.norm) if end is None else end
        return Norm(self.raw, self.norm[start:end], self.spans[start:end])

    def strip(self) -> "Norm":
        start = len(self.norm) - len(self.norm.lstrip())
        return self.slice(start, max(start, len(self.norm.rstrip())))

    def original(self, start: int, end: int) -> str:
        """The raw text behind a span of `.norm`."""
        if start < 0 or end <= start:
            return ""
        return self.raw[self.spans[start][0] : self.spans[end - 1][1]]

    def group(self, match: re.Match, name: str) -> str:
        """The raw text behind a named capture, stripped.

        Returns "" for a group that did not participate in the match -- its
        span is (-1, -1) -- so callers keep using `or None` to tell "no
        value" from a value, exactly as they did with match[name].
        """
        return self.original(*match.span(name)).strip()


# --- Things we never store -------------------------------------------------

# Present at all => refuse. These words have no innocent reading in a
# "remember that..." sentence.
_ALWAYS_SENSITIVE = tuple(normalize(word) for word in (
    "κωδικ", "συνθηματικ", "password", "passwd", "pin",
    "api key", "apikey", "μυστικ", "iban", "cvv",
))

# Refuse only alongside a run of digits: "η κάρτα βιβλιοθήκης λήγει τον Μάιο"
# is a perfectly ordinary note, "η κάρτα μου είναι 4532..." is not.
_SENSITIVE_WITH_DIGITS = tuple(
    normalize(word)
    for word in ("κάρτα", "ΑΦΜ", "ταυτότητ", "διαβατήρι", "token")
)

# Matched against the raw text rather than the normalized one, and holding
# nothing Greek, so these stay on plain re.compile().
_SENSITIVE_SHAPES = (
    re.compile(r"\d[\d\s-]{11,22}\d"),   # card-length digit runs, spaced or not
    re.compile(r"\b\d{8,}\b"),           # ID / AFM / other long numeric ids
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),  # API-key shaped tokens
)

_HAS_DIGIT_RUN = _re(r"\d{4,}")


def is_sensitive(text: str) -> bool:
    """True if this must never reach the database, from voice or the CLI."""
    norm = normalize(text)

    if any(word in norm for word in _ALWAYS_SENSITIVE):
        return True

    if _HAS_DIGIT_RUN.search(norm) and any(
        word in norm for word in _SENSITIVE_WITH_DIGITS
    ):
        return True

    return any(shape.search(text) for shape in _SENSITIVE_SHAPES)


# --- Shared vocabulary -----------------------------------------------------
#
# Every table here is keyed by normalize()d Greek and looked up with tokens
# taken from normalized text, so the keys are folded once at import rather
# than written out pre-folded. Same reason as _re(): "παρασκευη" is readable,
# "παρασκευι" is not.


def _folded(mapping: dict[str, int]) -> dict[str, int]:
    return {normalize(key): value for key, value in mapping.items()}


MONTHS = _folded({
    "ιανουαριου": 1, "ιανουαριοσ": 1, "ιανουαριο": 1,
    "φεβρουαριου": 2, "φεβρουαριοσ": 2, "φεβρουαριο": 2,
    "μαρτιου": 3, "μαρτιοσ": 3, "μαρτιο": 3,
    "απριλιου": 4, "απριλιοσ": 4, "απριλιο": 4,
    "μαιου": 5, "μαιοσ": 5, "μαιο": 5,
    "ιουνιου": 6, "ιουνιοσ": 6, "ιουνιο": 6,
    "ιουλιου": 7, "ιουλιοσ": 7, "ιουλιο": 7,
    "αυγουστου": 8, "αυγουστοσ": 8, "αυγουστο": 8,
    "σεπτεμβριου": 9, "σεπτεμβριοσ": 9, "σεπτεμβριο": 9,
    "οκτωβριου": 10, "οκτωβριοσ": 10, "οκτωβριο": 10,
    "νοεμβριου": 11, "νοεμβριοσ": 11, "νοεμβριο": 11,
    "δεκεμβριου": 12, "δεκεμβριοσ": 12, "δεκεμβριο": 12,
})

WEEKDAYS = _folded({
    "δευτερα": 0, "τριτη": 1, "τεταρτη": 2, "πεμπτη": 3,
    "παρασκευη": 4, "σαββατο": 5, "κυριακη": 6,
})

RELDAY = _folded({"σημερα": 0, "αυριο": 1, "μεθαυριο": 2})

# Checked in this order: "δευτερολεπτ" must precede "λεπτ", which it contains.
UNITS = tuple(
    (normalize(unit), seconds)
    for unit, seconds in (
        ("δευτερολεπτ", 1),
        ("λεπτ", 60),
        ("ωρ", 3600),
        ("ημερ", 86400),
        ("μερ", 86400),
        ("εβδομαδ", 604800),
        ("μην", 2592000),
    )
)

_MONTHS_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
_WEEKDAYS_RE = "|".join(WEEKDAYS)
_RELDAY_RE = "|".join(RELDAY)
_NUM_RE = r"\d+|" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
_UNIT_RE = "|".join(stem + r"\w*" for stem, _ in UNITS)

# --- Dates and clock times -------------------------------------------------

RE_DATE_NUM = _re(r"\b(?P<d>\d{1,2})[/\-.](?P<m>\d{1,2})(?:[/\-.](?P<y>\d{2,4}))?\b")
RE_DATE_WORD = _re(rf"\b(?P<d>\d{{1,2}})\s+(?P<month>{_MONTHS_RE})(?:\s+(?P<y>\d{{4}}))?\b")
RE_DATE_REL = _re(rf"\b(?P<rel>{_RELDAY_RE})\b")
RE_DATE_WDAY = _re(rf"\b(?:την\s+|το\s+)?(?P<wday>{_WEEKDAYS_RE})\b")
RE_CLOCK = _re(
    r"\bστισ\s+(?P<h>\d{1,2})(?:[:.](?P<min>\d{2}))?"
    r"(?:\s+(?P<part>το\s+πρωι|το\s+μεσημερι|το\s+απογευμα|το\s+βραδυ))?"
)


def _parse_date(text: str, today: date) -> date | None:
    """First date-shaped thing in text, or None. A bare day/month already
    past rolls forward to next year -- "στις 12 Ιουνίου" said in September
    means next June, not one that has been and gone."""
    if m := RE_DATE_WORD.search(text):
        day, month = int(m["d"]), MONTHS[m["month"]]
        year = int(m["y"]) if m["y"] else today.year
        return _roll_forward(day, month, year, today, explicit_year=bool(m["y"]))

    if m := RE_DATE_NUM.search(text):
        day, month = int(m["d"]), int(m["m"])
        if not (1 <= month <= 12):
            return None
        if m["y"]:
            year = int(m["y"])
            year += 2000 if year < 100 else 0
            return _safe_date(day, month, year)
        return _roll_forward(day, month, today.year, today, explicit_year=False)

    if m := RE_DATE_REL.search(text):
        return today + timedelta(days=RELDAY[m["rel"]])

    if m := RE_DATE_WDAY.search(text):
        ahead = (WEEKDAYS[m["wday"]] - today.weekday()) % 7
        return today + timedelta(days=ahead or 7)

    return None


def _safe_date(day: int, month: int, year: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _roll_forward(
    day: int, month: int, year: int, today: date, explicit_year: bool
) -> date | None:
    parsed = _safe_date(day, month, year)
    if parsed is None:
        return None
    if not explicit_year and parsed < today:
        return _safe_date(day, month, year + 1)
    return parsed


def _parse_clock(text: str, now: datetime) -> datetime | None:
    """A "στις 5 το απόγευμα"-shaped time, resolved against now. An hour that
    has already passed today means tomorrow."""
    m = RE_CLOCK.search(text)
    if m is None:
        return None

    hour = int(m["h"])
    minute = int(m["min"]) if m["min"] else 0
    if hour > 23 or minute > 59:
        return None

    part = (m["part"] or "").strip()
    if part in ("το μεσημερι", "το απογευμα", "το βραδυ") and hour < 12:
        hour += 12

    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    day = _parse_date(text, now.date())
    if day is not None:
        return when.replace(year=day.year, month=day.month, day=day.day)

    return when if when > now else when + timedelta(days=1)


def _number(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token)


def _unit_seconds(token: str) -> int | None:
    for stem, seconds in UNITS:
        if stem in token:
            return seconds
    return None


# --- The trigger, and the pattern families ---------------------------------

# Whisper punctuates what it transcribes, and sometimes splits a word in two.
# A real hand test of "Θυμήσου ότι με λένε Γιάννη" came back as "Θυμί σου,
# ό,τι με λένε Γιάννη": a space dropped into the verb, a comma after it, and
# "ό,τι" for "ότι". Each of those defeated a separator written as a plain
# \s+, so the save fell through to the brain -- which role-played having
# saved it, and only the next recall showed that nothing had been. The
# separators below tolerate what the recognizer actually emits.
#
# Stripping punctuation in normalize() instead would be the wrong fix twice
# over: commas and periods are load-bearing further down this ladder (see
# RE_COURSE_OF, which uses them to find where a course name ends), and every
# stored `norm` would change, needing another migration.
_GAP = r"[\s,.·]+"     # between two words of the trigger phrase
_JOIN = r"[\s,]*"      # inside one word the recognizer may have split
_OTI = rf"ο{_JOIN}τι"  # "ότι" and "ό,τι" both normalize into this

RE_TRIGGER = _re(
    rf"^(?:τζαρβισ{_GAP})?"
    rf"(?:θυμη{_JOIN}σου(?:{_GAP}{_OTI}|{_GAP}πωσ)?"
    rf"|να{_GAP}θυμασαι(?:{_GAP}{_OTI})?"
    rf"|θελω{_GAP}να{_GAP}θυμασαι(?:{_GAP}{_OTI})?"
    rf"|κρατα(?:{_GAP}{_OTI})?"
    rf"|σημειωσε(?:{_GAP}{_OTI})?"
    rf"|μην{_GAP}ξεχασεισ(?:{_GAP}{_OTI})?)"
    rf"{_GAP}"
)

# The particle the trigger may have left behind, stripped from the body in
# parse(). Punctuation-tolerant for the same reason, or "ό,τι" survives into
# a stored note as a stray "ο,τι".
RE_STRANDED_PARTICLE = _re(rf"^(?:{_OTI}|πωσ)\b[\s,.·]*")


# --- The fuzzy trigger, for an ending the recognizer invented --------------
#
# Whisper reproduces the stressed *stem* of the trigger verb and improvises
# the unstressed ending. Three separate hand tests of one spoken "Θυμήσου"
# produced "Θυμίσου" (another spelling of the same sound), "Θυμί σου" (a word
# break that was never spoken) and "Θυμήσω" (a different ending outright).
# The fold and _JOIN handle the first two; enumerating the third would only
# lose to a fourth, so the ending is matched by edit distance instead of by
# spelling -- while the stem stays exact.
#
# Three things keep that from becoming a licence to save anything vaguely
# trigger-shaped, and none of them is a tuned threshold:
#
#   1. **The stem is mandatory and matched exactly.** "να θυμ" keeps its
#      "να", so "Θυμάσαι τι σου είπα;" -- a recall question, and only 2 edits
#      from "να θυμάσαι" -- can never reach the save path however loose the
#      ending budget gets. A single global distance of 2 (which the live miss
#      needs) would turn every such question into a save, so what protects
#      recall here is the shape of the match, not its budget.
#   2. **This runs only when RE_TRIGGER misses**, so nothing that matches
#      today changes path.
#   3. **A fuzzy match may not reach the plain-note rung** -- see `notes_ok`
#      in parse(). A guessed trigger has to be confirmed by a second,
#      independent pattern before anything is written.
#
# The budget is per phrase rather than global, because how much room a verb
# has depends entirely on what lives next to it in the language.
#
# The matching itself is text.fuzzy_word(), shared with policy's kill switch.

_JARVIS = normalize("Τζάρβις")

_FUZZY_TRIGGERS = tuple(
    (normalize(core), tuple(normalize(end) for end in endings), budget)
    for core, endings, budget in (
        # The failure site itself. Nearest negative is "θύμωσα", 3 away.
        ("θυμ", ("ήσου",), 2),
        # The "να" is the guard described above, not decoration.
        ("να θυμ", ("άσαι",), 2),
        # "Θέλω να θυμάσαι ότι..." -- how the phrase is actually spoken, and
        # a live miss on both paths: RE_TRIGGER is anchored, so the leading
        # "θέλω" kept it out, and "να θυμ" does not start the utterance
        # either. The longer core is its own guard.
        ("θέλω να θυμ", ("άσαι",), 2),
        # 1, not 2: "σημειώσεις" (an ordinary noun that opens a sentence) is
        # exactly 2 from "σημείωσε".
        ("σημείωσ", ("ε",), 1),
        ("μην ξεχ", ("άσεις",), 2),
    )
)
# "κράτα" is deliberately absent: at 5 characters it has no room at all --
# "κρατάω" is a single edit away, so even a budget of 1 would claim
# "Κρατάω σημειώσεις στο μάθημα". It stays exact-only, in RE_TRIGGER.


def fuzzy_trigger(norm: str) -> int | None:
    """Where the body starts when the trigger's ending was mangled, or None.

    Matched against normalize()d text like RE_TRIGGER, and returning an index
    into it, so the caller keeps slicing a Norm and reading captures back out
    of the raw utterance exactly as it does on the exact path.
    """
    start = 0
    if norm.startswith(_JARVIS):
        start = skip_separators(norm, len(_JARVIS))

    for core, endings, budget in _FUZZY_TRIGGERS:
        end = fuzzy_word(norm, core, endings, budget, start)
        if end is None:
            continue
        # Consume the gap after the verb, as RE_TRIGGER's trailing _GAP does,
        # so the body starts where RE_STRANDED_PARTICLE expects it to.
        return skip_separators(norm, end)

    return None


_REMIND_VERB = rf"(?:υπενθυμισε|θυμισε){_GAP}μου"

RE_REMIND_REL = _re(
    rf"{_REMIND_VERB}\s+σε\s+(?P<num>{_NUM_RE})\s+(?P<unit>{_UNIT_RE})"
    r"\s*(?:οτι|να|πωσ)?\s*(?P<body>.+)"
)
RE_REMIND_FRAC = _re(
    rf"{_REMIND_VERB}\s+σε\s+(?P<frac>μιαμιση\s+ωρα|μιση\s+ωρα|ενα\s+τεταρτο)"
    r"\s*(?:οτι|να|πωσ)?\s*(?P<body>.+)"
)
RE_REMIND_ABS = _re(
    rf"{_REMIND_VERB}\s+(?P<when>.*?)\s*(?:οτι|να)\s+(?P<body>.+)"
)

# Keyed by what RE_REMIND_FRAC captures, which is normalized text.
_FRAC_SECONDS = _folded(
    {"μισή ώρα": 1800, "ένα τέταρτο": 900, "μιάμιση ώρα": 5400}
)

RE_EXAM_WORD = _re(
    r"(?:εξετασ\w*|διαγωνισμ\w*|τελικ[ηεσ]\w*|προοδ\w*|παραδοση|προθεσμια|deadline)"
)
# (?!\d) matters: "στις" introduces both a date ("στις 12 Ιουνίου") and a
# course ("στα Μαθηματικά"). Refusing a course that starts with a digit makes
# the date position fail to match, so the search moves on to the real course.
RE_COURSE_OF = _re(
    r"(?:στο|στη|στην|στα|στισ|στουσ)\s+(?P<course>(?!\d)[^,.]+?)"
    r"(?=\s*(?:στισ|την|το\s|$|,|\.))"
)
RE_COURSE_OF_ALT = _re(r"μαθημα\s+(?P<course>[^,.]+)")
RE_TOPIC_OF = _re(r"(?:για|πανω\s+σε|με\s+θεμα)\s+(?P<topic>.+)")

RE_COURSE_GATE = _re(r"\bμαθημα\b|\bεξαμηνο\b")
RE_COURSE = _re(
    r"(?:κανω|εχω|περνω|παρακολουθω|γραφτηκα\s+στο)\s+"
    r"(?:το\s+)?(?:μαθημα\s+)?(?P<course>[^,.]+?)"
    r"(?:\s+(?P<sem>αυτο\s+το\s+εξαμηνο"
    r"|το\s+(?:εαρινο|χειμερινο)\s+εξαμηνο"
    r"|στο\s+\d+ο\s+εξαμηνο))?$"
)

# Captured values are stored verbatim, articles included: "η σχολή μου είναι
# το ΕΚΠΑ" stores "το εκπα". Consistent with every other profile pattern.
PROFILE_PATTERNS = (
    (_re(r"(?:με\s+λενε|το\s+ονομα\s+μου\s+ειναι|ειμαι\s+ο|ειμαι\s+η)\s+(?P<value>[^,.]+)"), "ονομα"),
    (_re(r"(?:σπουδαζω|ειμαι\s+φοιτητ(?:ησ|ρια)\s+(?:στο|στη|στην))\s+(?P<value>[^,.]+)"), "σπουδεσ"),
    (_re(r"δουλευω\s+(?:ωσ|σαν|στο|στη|στην|σε)\s+(?P<value>[^,.]+)"), "δουλεια"),
    (_re(r"(?:μενω|ζω)\s+(?:στο|στη|στην|σε)\s+(?P<value>[^,.]+)"), "πολη"),
    (_re(rf"ειμαι\s+(?P<value>{_NUM_RE})\s+(?:χρονων|ετων)"), "ηλικια"),
    (_re(r"(?:η\s+σχολη\s+μου|το\s+πανεπιστημιο\s+μου)\s+(?:ειναι\s+)?(?P<value>[^,.]+)"), "σχολη"),
    (_re(r"(?:μου\s+αρεσει|μου\s+αρεσουν|προτιμω)\s+(?P<value>[^,.]+)"), "προτιμησεισ"),
)

_BIZ_HEAD = (
    r"(?:η\s+επιχειρηση\s+μου|το\s+μαγαζι\s+μου|η\s+εταιρεια\s+μου"
    r"|η\s+δουλεια\s+μου|το\s+καταστημα\s+μου)"
)
RE_BIZ_NEED = _re(
    _BIZ_HEAD + r"\s*(?P<name>[^,.]*?)\s*"
    r"(?:χρειαζεται|θελει|εχει\s+αναγκη|πρεπει\s+να)\s+(?P<note>.+)"
)
RE_BIZ_PLAIN = _re(_BIZ_HEAD + r"\s*(?P<note>.+)$")

# Appended to a row's `norm` so the row stays findable by the generic word
# that classified it, even when the pattern consumed that word. Without this,
# "η επιχείρησή μου χρειάζεται λογιστή" is filed under businesses but cannot
# be found by searching for "επιχείρηση".
_TABLE_KEYWORDS = {
    "businesses": "επιχειρηση μαγαζι εταιρεια καταστημα",
    "courses": "μαθημα εξαμηνο",
}


# --- Tags ------------------------------------------------------------------
#
# A row's life area, so "τι έχω σήμερα" can pull across every table at once
# instead of needing to know which table holds what. Two sources, in order:
# the table itself, which is exact and free (a course is university by
# construction), and then the MEMORY_TAGS keywords matched against the row's
# already-folded `norm`.
#
# This is _TABLE_KEYWORDS' idea done properly. That one keeps a row findable
# by the generic word that filed it, by stuffing the word into the search key;
# a tag is the same classification in a column of its own, where it can be
# selected on rather than only matched.
#
# Matching is exact-substring, deliberately not fuzzy. The asymmetry that
# justified edit distance for the kill switch -- a miss means the safety
# feature did not work -- does not hold here: a missed tag only means the row
# is found the way it was found before tags existed. Tagging is additive and
# never gates a save.
_TAG_KEYWORDS = {
    tag: tuple(normalize(word) for word in words)
    for tag, words in MEMORY_TAGS.items()
}

# Tags a table implies on its own, whatever its text happens to say.
_TABLE_TAGS = {
    "courses": "university",
    "exams": "university",
    "businesses": "business",
}

# Stored comma-delimited *and* comma-terminated: ",cafe,university,". The
# sentinels are what let LIKE '%,cafe,%' match a whole tag rather than a
# prefix of another one -- without them "marketing" matches "ai_marketing".
TAG_SEP = ","


def pack_tags(tags: list[str]) -> str | None:
    """The stored form, or None for a row nothing matched. NULL rather than
    "" says "no tag" unambiguously, the same choice businesses.name makes."""
    if not tags:
        return None
    return TAG_SEP + TAG_SEP.join(tags) + TAG_SEP


def unpack_tags(value: str | None) -> list[str]:
    return [tag for tag in (value or "").split(TAG_SEP) if tag]


def tag_like(tag: str) -> str:
    """The LIKE pattern matching one whole tag inside a packed column."""
    return f"%{TAG_SEP}{tag}{TAG_SEP}%"


def tags_in(norm: str) -> list[str]:
    """Tags the normalized text itself implies, in MEMORY_TAGS order."""
    return [
        tag
        for tag, words in _TAG_KEYWORDS.items()
        if any(word in norm for word in words)
    ]


def tags_for(table: str, norm: str) -> str | None:
    """The packed tags for one row: what its table implies, then what its text
    does. Called from save(), so every write path tags the same way."""
    tags = []
    if implied := _TABLE_TAGS.get(table):
        tags.append(implied)
    for tag in tags_in(norm):
        if tag not in tags:
            tags.append(tag)
    return pack_tags(tags)


def tag_of_query(text: str) -> str | None:
    """The one area a spoken question is about («τι έχω σήμερα για το μαγαζί»).

    None when nothing matched *or* when two areas did: filtering an agenda by
    one of two named areas would answer a question nobody asked, and showing
    everything is the recoverable direction.
    """
    found = tags_in(normalize(text))
    return found[0] if len(found) == 1 else None


def parse(text: str, now: datetime | None = None) -> Parsed | None:
    """Decide what a spoken sentence should become.

    Returns None when this was not a save request at all (no trigger phrase,
    no reminder verb) -- the caller then lets the utterance fall through to
    the brain untouched. Returns Parsed(REJECTED) when it contained something
    we refuse to store.
    """
    now = now or datetime.now()
    view = Norm.of(text)

    if is_sensitive(text):
        return Parsed(REJECTED)

    # Step 0: reminder verbs are their own trigger -- no "θυμήσου" needed.
    if reminder := _parse_reminder(view, now):
        return reminder

    # Step 1: without a trigger this is not a save request.
    #
    # `notes_ok` records how certain that trigger was. An exact match earns
    # the whole ladder, unconditional fallback included. A fuzzy one -- the
    # recognizer having invented an ending -- earns only the structured
    # rungs: see the note above step 7.
    trigger = RE_TRIGGER.match(view.norm)
    notes_ok = trigger is not None
    if trigger is not None:
        start = trigger.end()
    elif (start := fuzzy_trigger(view.norm)) is None:
        return None
    # "Θυμήσου ότι" with nothing after it backtracks: the optional "ότι" in
    # RE_TRIGGER gives way so the mandatory \s+ can match, leaving "οτι" as
    # the body. Strip a stranded particle so that reads as "no body" rather
    # than being stored as a note saying "οτι".
    #
    # Sliced rather than sub()'d because the body has to stay a Norm: the
    # sub-parsers below capture out of it and need the map back into `text`.
    # RE_STRANDED_PARTICLE is ^-anchored, so matching it and slicing past it
    # is the same edit.
    body = view.slice(start).strip()
    if particle := RE_STRANDED_PARTICLE.match(body.norm):
        body = body.slice(particle.end()).strip()
    if not body.norm:
        return None

    # Steps 2-6: first match wins.
    if exam := _parse_exam(body, now):
        return exam
    if course := _parse_course(body):
        return course
    if profile := _parse_profile(body):
        return profile
    if business := _parse_business(body):
        return business

    # Step 7: always taken -- when the trigger was certain.
    #
    # A fuzzy trigger that reaches here was confirmed by nothing: no pattern
    # above recognized the body either, so the only evidence that this was a
    # save request at all is a verb ending the recognizer may simply have
    # invented. Writing a note on that would be guessing twice. Falling
    # through to the brain is what this utterance did before the fuzzy layer
    # existed, so the worst case is unchanged behaviour rather than a wrong
    # row -- and a wrong row is the expensive one, since nobody sees it until
    # a recall reads it back.
    if not notes_ok:
        return None

    # Stored verbatim, not normalized, so it reads back the way it was said.
    return Parsed("notes", {"text": text.strip(), "norm": view.norm})


def _parse_reminder(view: Norm, now: datetime) -> Parsed | None:
    # The body is read back through the view, so a reminder is announced the
    # way it was said. "when" stays normalized: it is parsed, never shown.
    if m := RE_REMIND_REL.search(view.norm):
        number = _number(m["num"])
        seconds = _unit_seconds(m["unit"])
        if number is not None and seconds is not None:
            return _reminder(
                view.group(m, "body"), now + timedelta(seconds=number * seconds)
            )

    if m := RE_REMIND_FRAC.search(view.norm):
        return _reminder(
            view.group(m, "body"),
            now + timedelta(seconds=_FRAC_SECONDS[m["frac"]]),
        )

    if m := RE_REMIND_ABS.search(view.norm):
        when = _parse_clock(m["when"], now)
        if when is None:
            day = _parse_date(m["when"], now.date())
            # A date with no time at all: 9am on the day, a sane default for
            # "remind me on Tuesday to ...".
            when = (
                datetime.combine(day, datetime.min.time()).replace(hour=9)
                if day
                else None
            )
        if when is not None:
            return _reminder(view.group(m, "body"), when)

    return None


def _reminder(body: str, due: datetime) -> Parsed:
    """`body` is raw text now rather than a slice of the normalized string,
    so the stored `norm` is derived from it instead of being it."""
    body = body.strip()
    return Parsed(
        "reminders",
        {
            "text": body,
            "norm": normalize(body),
            "due_at": due.isoformat(timespec="seconds"),
            "kind": "reminder",
            "referent_date": None,
            "status": "pending",
        },
    )


def _parse_exam(body: Norm, now: datetime) -> Parsed | None:
    if not RE_EXAM_WORD.search(body.norm):
        return None
    due = _parse_date(body.norm, now.date())
    if due is None:
        return None

    course = ""
    if m := RE_COURSE_OF_ALT.search(body.norm):
        course = body.group(m, "course")
    elif m := RE_COURSE_OF.search(body.norm):
        course = body.group(m, "course")

    topic = None
    if m := RE_TOPIC_OF.search(body.norm):
        topic = body.group(m, "topic")

    return Parsed(
        "exams",
        {
            "due_date": due.isoformat(),
            "course": course,
            "topic": topic,
            "norm": normalize(f"{course} {topic or ''} {body.norm}"),
        },
    )


def _parse_course(body: Norm) -> Parsed | None:
    # Gated: without "μάθημα" or "εξάμηνο" this pattern would swallow
    # "κάνω γυμναστική" as a university course.
    if not RE_COURSE_GATE.search(body.norm):
        return None
    m = RE_COURSE.search(body.norm)
    if m is None:
        return None

    name = body.group(m, "course")
    if not name:
        return None

    semester = body.group(m, "sem") or None
    return Parsed(
        "courses",
        {
            "name": name,
            "semester": semester,
            "norm": normalize(
                f"{name} {semester or ''} {_TABLE_KEYWORDS['courses']}"
            ),
        },
    )


def _parse_profile(body: Norm) -> Parsed | None:
    for pattern, key in PROFILE_PATTERNS:
        if m := pattern.search(body.norm):
            value = body.group(m, "value")
            if value:
                return Parsed(
                    "profile", {"key": key, "value": value, "norm": normalize(f"{key} {value}")}
                )
    return None


def _parse_business(body: Norm) -> Parsed | None:
    m = RE_BIZ_NEED.search(body.norm)
    if m is not None:
        # An empty capture means no business was named. Stored as None, never
        # "": NULL says "no name given" unambiguously.
        name = body.group(m, "name") or None
        note = body.group(m, "note")
    else:
        m = RE_BIZ_PLAIN.search(body.norm)
        if m is None:
            return None
        name = None
        note = body.group(m, "note")

    if not note:
        return None

    return Parsed(
        "businesses",
        {
            "name": name,
            "note": note,
            # The head phrase ("η επιχείρησή μου") is consumed by the pattern,
            # so without this an unnamed row holds only its note and is
            # unfindable by the very word that filed it there.
            "norm": normalize(f"{name or ''} {note} {_TABLE_KEYWORDS['businesses']}"),
        },
    )


# --- Writing ---------------------------------------------------------------

def save(parsed: Parsed, conn: sqlite3.Connection) -> int:
    """Write a Parsed row. Returns the new (or updated) row id."""
    ts = db.now_iso()
    fields = dict(parsed.fields)

    # Tagged here rather than in parse(), because save() is the one place
    # every write passes through -- and because the tag is derived from the
    # finished `norm`, which parse() is still assembling.
    fields["tags"] = tags_for(parsed.table, fields.get("norm", ""))

    if parsed.table == "profile":
        # One row per key: saying your name twice updates it rather than
        # accumulating contradictory rows.
        conn.execute(
            "INSERT INTO profile (key, value, norm, tags, created_at, updated_at)"
            " VALUES (:key, :value, :norm, :tags, :ts, :ts)"
            " ON CONFLICT(key) DO UPDATE SET"
            " value=excluded.value, norm=excluded.norm, tags=excluded.tags,"
            " updated_at=excluded.updated_at",
            {**fields, "ts": ts},
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM profile WHERE key = ?", (fields["key"],)
        ).fetchone()
        return row["id"]

    fields["created_at"] = ts
    fields["updated_at"] = ts
    columns = ", ".join(fields)
    placeholders = ", ".join(f":{name}" for name in fields)
    cursor = conn.execute(
        f"INSERT INTO {parsed.table} ({columns}) VALUES ({placeholders})", fields
    )
    conn.commit()
    return cursor.lastrowid


# --- Stemming and recall ---------------------------------------------------

# Longest first, so "ματα" is tried before "ατα" before "α". Normalized and
# deduplicated: the iotacism fold collapses εισ/ησ/υσ onto ισ and η/ι/υ/οι
# onto ι, so the written-out list has duplicates that a set removes. Ties are
# broken alphabetically only to keep the order deterministic -- two suffixes
# of the same length can never both match the same ending.
_SUFFIXES = tuple(
    sorted(
        {
            normalize(suffix)
            for suffix in (
                "ματων", "ματοσ", "ματα", "ατων", "ατοσ", "ατα",
                "εων", "εισ", "ουσ", "ιων", "ιου",
                "ων", "ησ", "εσ", "οσ", "ου", "οι", "ασ", "υσ",
                "α", "ε", "η", "ι", "ο", "υ",
            )
        },
        key=lambda suffix: (-len(suffix), suffix),
    )
)

# A suffix that would leave less than this is skipped and the next tried.
# Without it "μαθηματα" loses "ματα" and becomes "μαθη", which then matches
# μάθηση, μαθητής and μαθηματικά indiscriminately.
MIN_STEM = 5

_STOPWORDS = {
    normalize(word)
    for word in (
        "ειναι", "εχω", "εχει", "ημουν", "αυτο", "αυτη", "αυτοσ", "ολα", "ολο",
        "για", "απο", "και", "μου", "σου", "του", "τησ", "των", "στο", "στη",
        "στην", "στα", "στισ", "που", "ποια", "ποιο", "ποιοσ", "τι", "να", "θα",
        "δεν", "μην", "πωσ", "οτι", "καθε", "πολυ", "μετα", "πριν", "παλι",
        "θυμασαι", "θυμησου", "ξερεισ", "πεσ", "μπορεισ", "ειπεσ", "ειπα",
    )
}


def stem(token: str) -> str:
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= MIN_STEM:
            return token[: -len(suffix)]
    return token


def stems_of(text: str) -> list[str]:
    """Search stems for a query, in order, deduplicated."""
    seen: list[str] = []
    for token in normalize(text).split():
        token = token.strip(".,;:!?…«»\"'")
        if len(token) < 4 or token in _STOPWORDS:
            continue
        candidate = stem(token)
        if candidate not in seen:
            seen.append(candidate)
    return seen


def estimate_tokens(text: str) -> int:
    """Deliberately crude and deliberately pessimistic: Greek runs about 3
    characters per token for the tokenizers these models use, so this
    over-counts rather than blowing the budget."""
    return len(text) // 3


def recall(
    text: str, conn: sqlite3.Connection, now: datetime | None = None
) -> str:
    """The memory block injected into a brain call, under a hard token budget.

    Assembled in priority order: the profile digest and anything due today go
    in first and are effectively always present; keyword hits fill whatever
    budget is left. Lines are added whole and dropped whole.
    """
    now = now or datetime.now()
    budget = MEMORY_TOKEN_BUDGET
    lines: list[str] = []

    def add(line: str) -> bool:
        nonlocal budget
        cost = estimate_tokens(line) + 1
        if cost > budget:
            return False
        budget -= cost
        lines.append(line)
        return True

    if digest := _profile_digest(conn):
        add(digest)

    for line in _due_today(conn, now):
        add(line)

    for line in _keyword_hits(text, conn, now):
        if not add(line):
            break

    return "\n".join(lines)


def _profile_digest(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT key, value FROM profile ORDER BY updated_at DESC LIMIT 6"
    ).fetchall()
    if not rows:
        return ""
    pairs = "· ".join(f"{row['key']}={row['value']}" for row in rows)
    return f"Προφίλ: {pairs}"


# How each key PROFILE_PATTERNS writes is spoken, with its stored value.
#
# These used to name the key alone ("το όνομά σου") and deliberately withhold
# the value, because values were captured out of the normalized string and
# came back folded -- "Γιάννης" stored as "γιαννισ", which is wrong out loud.
# Norm.group() fixed that at the source, so the value can be spoken now.
#
# Each phrase is built so the value needs no agreement with it: the pattern
# consumes the preposition ("μένω *στην* Αθήνα" stores "Αθήνα"), so a
# template like "μένεις {value}" would be ungrammatical and "μένεις στη
# {value}" would guess the article's gender. "η πόλη σου είναι {value}"
# needs neither.
#
# Listed most- to least-identifying; that is the order they are spoken in,
# not the order the rows come out of the table.
# test_memory_recall.py asserts every PROFILE_PATTERNS key appears here.
_PROFILE_LABELS = (
    ("ονομα", "το όνομά σου είναι {value}"),
    ("σχολη", "η σχολή σου είναι {value}"),
    ("σπουδεσ", "σπουδάζεις {value}"),
    ("δουλεια", "η δουλειά σου είναι {value}"),
    ("πολη", "η πόλη σου είναι {value}"),
    ("ηλικια", "είσαι {value} χρονών"),
    ("προτιμησεισ", "σου αρέσει {value}"),
)


def _join_greek(items: list[str]) -> str:
    """"α, β και γ" -- a Greek list takes no comma before the final "και"."""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " και " + items[-1]


def spoken_profile(conn: sqlite3.Connection) -> str:
    """What memory holds, phrased to be spoken.

    The answer to a recall question with no topic in it ("Τι θυμάσαι;"): every
    token is a stopword or too short to stem, so there is nothing to
    keyword-match and "δεν θυμάμαι κάτι σχετικό" would be reporting a search
    that was never run.

    Speaks the stored values, not just the keys they are filed under. That
    was not possible while values were captured out of the normalized string
    -- saying a name back as "γιαννισ" is worse than not saying it -- so this
    listed the keys and asked for a narrower question instead. Norm.group()
    stores them verbatim now, so the answer can simply be the facts.

    Still distinct from _profile_digest(), which feeds the same rows to a
    prompt as key=value pairs: this one has to be a Greek sentence.
    """
    held = {
        row["key"]: row["value"]
        for row in conn.execute("SELECT key, value FROM profile")
    }
    facts = [
        template.format(value=held[key])
        for key, template in _PROFILE_LABELS
        if key in held
    ]

    if not facts:
        return (
            "Δεν έχω κρατήσει ακόμα κάτι για σένα. "
            "Πες μου «θυμήσου ότι…» και θα το κρατήσω."
        )

    return f"Θυμάμαι ότι {_join_greek(facts)}."


# How a reminder that is no longer pending is spoken in an agenda. The row
# still belongs to the day; what changed is only whether it still awaits you.
_STATUS_SUFFIX = {"fired": " (έγινε)", "missed": " (χάθηκε)"}


def agenda(
    conn: sqlite3.Connection, day: date, tag: str | None = None
) -> list[tuple[str, str]]:
    """Everything dated `day`, as (kind, text), from every table with a date.

    This is what «τι έχω σήμερα» answers and the reason tags exist: the caller
    no longer has to know which table holds what. `tag` narrows it to one life
    area («τι έχω σήμερα για το μαγαζί»).

    Reminders are included whatever their status, with a fired or missed one
    marked. The question is what the day holds, not what is still queued --
    and since the scheduler began claiming rows, filtering on `pending` would
    make a 9am reminder invisible by 10am.
    """
    stamp = day.isoformat()
    items: list[tuple[str, str]] = []

    exam_sql = "SELECT course, topic FROM exams WHERE due_date = ?"
    exam_params: list = [stamp]
    reminder_sql = "SELECT text, status FROM reminders WHERE due_at LIKE ?"
    reminder_params: list = [f"{stamp}%"]

    if tag:
        # The sentinels in tag_like() are what keep this from matching a tag
        # that merely contains the one asked for.
        exam_sql += " AND tags LIKE ?"
        exam_params.append(tag_like(tag))
        reminder_sql += " AND tags LIKE ?"
        reminder_params.append(tag_like(tag))

    for row in conn.execute(exam_sql, exam_params):
        topic = f" ({row['topic']})" if row["topic"] else ""
        items.append(("exam", f"{row['course']}{topic}"))

    for row in conn.execute(reminder_sql, reminder_params):
        items.append(
            ("reminder", f"{row['text']}{_STATUS_SUFFIX.get(row['status'], '')}")
        )

    return items


def _due_today(conn: sqlite3.Connection, now: datetime) -> list[str]:
    """Today's agenda, rendered for the brain's memory block."""
    return [
        f"Σήμερα εξέταση: {text}" if kind == "exam" else f"Σήμερα: {text}"
        for kind, text in agenda(conn, now.date())
    ]


# Each searchable table: how to render a hit, and its recency column.
_SEARCHABLE = (
    ("notes", "SELECT id, text, created_at FROM notes WHERE norm LIKE ?", lambda r: r["text"]),
    ("exams", "SELECT id, due_date, course, topic, created_at FROM exams WHERE norm LIKE ?",
     lambda r: f"Εξέταση {r['course']} στις {r['due_date']}"
               + (f" ({r['topic']})" if r["topic"] else "")),
    ("courses", "SELECT id, name, semester, created_at FROM courses WHERE norm LIKE ?",
     lambda r: f"Μάθημα: {r['name']}" + (f" ({r['semester']})" if r["semester"] else "")),
    ("businesses", "SELECT id, name, note, created_at FROM businesses WHERE norm LIKE ?",
     lambda r: f"Επιχείρηση{' ' + r['name'] if r['name'] else ''}: {r['note']}"),
    ("reminders", "SELECT id, text, due_at, created_at FROM reminders WHERE norm LIKE ?",
     lambda r: f"Υπενθύμιση {r['due_at'][:10]}: {r['text']}"),
)


def _keyword_hits(text: str, conn: sqlite3.Connection, now: datetime) -> list[str]:
    stems = stems_of(text)
    if not stems:
        return []

    # (table, id) -> [matched stem count, rendered line, created_at]
    scored: dict[tuple[str, int], list] = {}

    for table, query, render in _SEARCHABLE:
        for needle in stems:
            for row in conn.execute(query, (f"%{needle}%",)):
                key = (table, row["id"])
                if key in scored:
                    scored[key][0] += 1
                else:
                    scored[key] = [1, render(row), row["created_at"]]

    def score(entry: list) -> float:
        matched, _, created_at = entry
        try:
            age = (now - datetime.fromisoformat(created_at)).days
        except (TypeError, ValueError):
            age = 0
        return 2 * matched + 1 / (1 + max(age, 0))

    ranked = sorted(scored.values(), key=score, reverse=True)
    return [entry[1] for entry in ranked]


def search(
    text: str, conn: sqlite3.Connection, now: datetime | None = None
) -> list[str]:
    """Ranked matches for a direct question ("τι θυμάσαι για ..."), without
    the profile digest and today's items that recall() always prepends."""
    return _keyword_hits(text, conn, now or datetime.now())


def recall_safe(text: str, now: datetime | None = None) -> str:
    """recall() that can never take down a conversation turn: a broken or
    locked database means no memory this turn, not a failed reply."""
    try:
        conn = db.connect()
        try:
            return recall(text, conn, now)
        finally:
            conn.close()
    except Exception as e:
        print(f"Σφάλμα μνήμης: {e}")
        return ""
