"""What Jarvis is allowed to do, and the record that it decided.

Sits between `skills.handle()` and every skill handler: nothing runs without a
permission, nothing that isn't SAFE runs without an explicit yes, and every
decision leaves an audit row behind. See CLAUDE.md "Policy".

**This module owns the Skill and Permission types, not skills.py.**
`dispatch()` has to call a skill's handler, so policy cannot import skills;
the registry's *contents* stay in skills.py and its *types* live here, which
skills.py imports back and re-exports under the same names. Same reason
text.py owns SHUTDOWN_PHRASES -- "κλείσε" has to keep working while frozen,
so the phrases have to be reachable from here without a cycle.

Nothing here imports speaker or listener either. Asking the user a question is
an injected callable (`set_confirm_asker`), exactly as `mem.run()` takes its
confirm reader, so the whole layer is testable without a microphone.

**The audit log never holds what was said.** Four short columns -- when, which
skill, what was decided, and a fixed reason code from the vocabulary below.
An utterance refused while frozen is logged as `request`, not as its text.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from jarvis import db
from jarvis.text import SHUTDOWN_PHRASES, fuzzy_word, match_core, normalize

# --- Vocabularies ----------------------------------------------------------
#
# Fixed strings, so `:mem list audit` reads consistently and a decision can be
# grepped for. Never interpolate user text into any of these.
DECISIONS = ("allowed", "blocked", "declined", "frozen", "thawed")
REASONS = (
    "safe",              # a SAFE skill ran
    "no_skill_matched",  # the brain answered instead
    "user_confirmed",    # a CONFIRM skill, approved out loud
    "user_declined",     # a CONFIRM skill, refused (or unanswered)
    "deny_by_default",   # registered BLOCKED, or registered without a permission
    "unknown_permission",
    "kill_switch",
    "voice",
    "voice_fuzzy",  # the kill switch, matched through a mangled transcription
    "cli",
    "restart",
)


class Permission(Enum):
    SAFE = "safe"
    CONFIRM = "confirm"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Skill:
    """One entry in skills.SKILLS.

    `phrases` documents what the skill responds to; it is not the dispatch
    mechanism, since matching style varies per skill (substring-in-list,
    single-keyword-then-parse, memory.py's own regex ladder). `input` says
    which text variant `handler` expects: "norm" (normalized) or "raw".

    **`permission` defaults to BLOCKED.** That is the deny-by-default rule:
    a skill added later without thinking about its permission cannot run.

    `matches` exists because the existing handlers conflate "does this match?"
    with "do the thing" -- they answer the first question by doing the second.
    That is fine for a SAFE skill, where running it *is* the decision, but a
    CONFIRM skill has to be recognised *before* it acts or there is nothing
    left to confirm. So anything not SAFE must be able to say whether it
    matched without acting, and a CONFIRM skill must also carry the Greek
    question to ask.
    """

    name: str
    phrases: tuple[str, ...]
    description: str
    handler: Callable[[str], str | None]
    permission: Permission = Permission.BLOCKED
    input: str = "norm"
    matches: Callable[[str], bool] | None = None
    confirm_prompt: str | None = None

    def __post_init__(self) -> None:
        # A CONFIRM skill missing either piece would silently never run, which
        # is a registration bug wearing a safety feature's clothes. Fail at
        # import instead. BLOCKED needs neither: with no matcher it simply
        # never runs, which is the correct outcome anyway.
        if self.permission is Permission.CONFIRM:
            if self.matches is None:
                raise ValueError(f"CONFIRM skill {self.name!r} needs a matches()")
            if not self.confirm_prompt:
                raise ValueError(f"CONFIRM skill {self.name!r} needs a confirm_prompt")


# --- What Jarvis says ------------------------------------------------------

FREEZE_REPLY = "Πάγωσα τα πάντα. Πες «κλείσε» για τερματισμό."
FROZEN_REPLY = "Είμαι παγωμένος. Πες «κλείσε» για τερματισμό."
BLOCKED_REPLY = "Δεν μου επιτρέπεται να το κάνω αυτό."
DECLINED_REPLY = "Εντάξει, δεν το έκανα."

# --- The kill switch -------------------------------------------------------
#
# Matched as a substring, and deliberately so: a false positive freezes Jarvis
# (recoverable, and exactly what the user asked for in spirit), while a false
# negative means the kill switch did not work when it was needed. Those are
# not symmetric.
#
# Built as a regex rather than a phrase list because normalize() does not
# strip punctuation and Whisper inserts it mid-phrase -- a spoken "σταμάτα τα
# πάντα" can arrive as "σταμάτα, τα πάντα", which no substring check would
# catch. Same separator idiom as memory._GAP; see CLAUDE.md "Punctuation is
# *not* normalized away".
_GAP = r"[\s,.·]+"

KILL_PHRASES = ("σταμάτα τα πάντα", "σταμάτησε τα πάντα", "πάγωσε τα πάντα")

_KILL_RE = re.compile(
    "|".join(
        _GAP.join(normalize(word) for word in phrase.split())
        for phrase in KILL_PHRASES
    )
)


# --- The kill switch, through a mangled transcription ----------------------
#
# Live test, twice: a spoken «σταμάτα τα πάντα» came back as «Στα μάτα τα
# πάντα» and «Λέω στα μάτα τα πάντα». Both normalize to "στα ματα τα παντα" --
# the ending survived, but the recognizer put a word break *inside* the verb,
# and _GAP above only tolerates separators *between* the phrase's words. A
# safety feature that speech recognition cannot reliably produce is not a
# safety feature, so the verb is matched the way memory's save triggers are:
# stressed stem exactly, unstressed ending by edit distance (text.fuzzy_word).
#
# **The asymmetry is the opposite of memory's, and that is what sets the
# budgets.** There a false positive is a wrong row nobody sees until a recall
# reads it back, so the budgets are stingy and "κράτα" gets none at all. Here
# a false positive is a freeze -- announced out loud, and undone by `:policy
# unlock` or a restart -- while a false negative is the kill switch failing at
# the one moment it exists for.
#
# **What keeps that honest is the object, not the budget.** A guessed verb
# earns nothing on its own: "τα πάντα" must follow it immediately, matched
# exactly (separators free, so a split "τα πά ντα" survives too). That is the
# same guard as memory's `notes_ok` -- a guessed trigger has to be confirmed
# by a second, independent piece before it counts. It is doing real work:
# "κοίτα με στα μάτια" normalizes to "κιτα με στα ματια", whose "στα ματια"
# matches the verb with an ending 1 edit away, and does not fire only because
# no "τα πάντα" follows. Widening the budget would not change that; dropping
# the object would.
#
# Two differences from memory.fuzzy_trigger(), both deliberate:
#
#   - **It scans every position** rather than anchoring at the start. The kill
#     switch has always been a substring match, and one of the two live
#     transcripts ("Λέω στα μάτα...") depends on it.
#   - **It runs only when _KILL_RE misses**, so every utterance that freezes
#     Jarvis today keeps its exact path, and the audit log tells the two
#     apart ("voice" vs "voice_fuzzy") -- which is the evidence for tuning
#     these budgets later.
#
# Measured over the corpus in tests/test_policy.py: 14/14 positives fire,
# 19/21 negatives stay silent. The two that fire are «θέλω να σταματήσω τα
# πάντα και να φύγω» and «σταμάτησα τα πάντα χθες» -- first-person forms of
# the phrase itself, and the nearest sentences in the language to what the
# switch listens for. Accepted: "σταμάτησε τα πάντα" already takes the
# third-person narration of the same thing.
_FUZZY_KILL = tuple(
    (normalize(core), tuple(normalize(end) for end in endings), budget)
    for core, endings, budget in (
        # Stem through the stress ("σταΜΑτα"), so nothing after it is trusted.
        ("σταματ", ("α", "ήσε"), 2),
        # The σ is load-bearing: it excludes "παγωτό" outright.
        ("πάγωσ", ("ε",), 2),
    )
)

_KILL_OBJECT = normalize("τα πάντα")


def fuzzy_kill(norm: str) -> bool:
    """Whether the utterance is the kill phrase with a mangled verb.

    Only consulted when the exact _KILL_RE misses. See the note above for why
    the object is mandatory and the ending's budget is not the guard.
    """
    for start in range(len(norm)):
        for core, endings, budget in _FUZZY_KILL:
            end = fuzzy_word(norm, core, endings, budget, start)
            # match_core() skips leading separators, so the object may be
            # spoken with any gap or punctuation after the verb -- but not
            # with another word in between.
            if end is not None and match_core(norm, _KILL_OBJECT, end) is not None:
                return True
    return False


_FROZEN_KEY = "frozen"

# Cache of the flag below, and the fallback when the database cannot be read.
# Never authoritative on its own: a second terminal's `unlock` writes to the
# database, so a sticky in-process True would make that unlock impossible.
# See is_frozen().
_frozen = False

_confirm_asker: Callable[[str], bool] | None = None


# --- Frozen state ----------------------------------------------------------


def is_frozen(conn: sqlite3.Connection | None = None) -> bool:
    """Whether the kill switch is on, for this process or any other.

    The database is authoritative whenever it can be read, so `unlock` from a
    second terminal takes effect on the next utterance. When it cannot be
    read, the last known value stands -- which fails *frozen* if this process
    is the one that froze, because forgetting a freeze is the expensive
    direction.
    """
    global _frozen
    state = _read_state(_FROZEN_KEY, conn)
    if state is not None:
        _frozen = state == "1"
    return _frozen


def freeze(source: str, conn: sqlite3.Connection | None = None) -> None:
    global _frozen
    _frozen = True
    _write_state(_FROZEN_KEY, "1", conn)
    record("kill_switch", "frozen", source, conn)


def thaw(source: str, conn: sqlite3.Connection | None = None) -> None:
    global _frozen
    _frozen = False
    _write_state(_FROZEN_KEY, "0", conn)
    record("kill_switch", "thawed", source, conn)


def clear_on_startup(conn: sqlite3.Connection | None = None) -> None:
    """Unlock on restart -- the kill switch's other documented exit.

    The flag is persisted so a second terminal can reach a running Jarvis,
    which means it also outlives the process that set it. Without this, a
    freeze would survive every restart and the only way out would be to edit
    the database by hand.

    Wrapped whole: a database that cannot be written is worth reporting but
    must never stop Jarvis from starting.
    """
    global _frozen
    try:
        was_frozen = _read_state(_FROZEN_KEY, conn) == "1"
        _frozen = False
        _write_state(_FROZEN_KEY, "0", conn)
        if was_frozen:
            # Only worth a row when it actually undid something; otherwise
            # every startup would log a thaw that thawed nothing.
            record("kill_switch", "thawed", "restart", conn)
            print("[policy] Ο διακόπτης ασφαλείας καθαρίστηκε στην εκκίνηση.")
    except Exception as e:
        print(f"Σφάλμα πολιτικής: {e}")


# --- Confirmation ----------------------------------------------------------


def set_confirm_asker(asker: Callable[[str], bool] | None) -> None:
    """Install the thing that asks the user a yes/no question.

    main.py installs a voice asker; the tests install a fake. Injected rather
    than imported so this module never depends on the microphone.
    """
    global _confirm_asker
    _confirm_asker = asker


def _ask(question: str) -> bool:
    # No asker installed (a CLI run, a test that forgot) is a no, not a yes:
    # an unattended process must not be able to approve on the user's behalf.
    if _confirm_asker is None:
        return False
    try:
        return bool(_confirm_asker(question))
    except Exception as e:
        print(f"Σφάλμα επιβεβαίωσης: {e}")
        return False


# --- The gate --------------------------------------------------------------


def intercept(norm: str) -> str | None:
    """Policy's look at an utterance before any skill sees it.

    Returns a reply to send instead of running skills, or None to carry on.

    The kill switch is checked here rather than registered as a skill on
    purpose: it governs the policy layer, so it must not be gated *by* the
    policy layer. Freezing has to work whatever state everything else is in.
    """
    if _KILL_RE.search(norm):
        freeze("voice")
        return FREEZE_REPLY

    if fuzzy_kill(norm):
        freeze("voice_fuzzy")
        return FREEZE_REPLY

    if not is_frozen():
        return None

    # "κλείσε" is the one thing a freeze does not touch -- being unable to
    # shut down a frozen assistant would be a worse trap than the one the
    # kill switch exists to escape.
    if any(p in norm for p in SHUTDOWN_PHRASES):
        return None

    record("request", "frozen", "kill_switch")
    return FROZEN_REPLY


def dispatch(skill: Skill, arg: str) -> str | None:
    """Run one skill, or don't, and say so in the audit log.

    Returns the skill's reply, a refusal, or None when the skill didn't match
    (which is what tells skills.handle() to try the next one).
    """
    permission = skill.permission if isinstance(skill.permission, Permission) else None

    if permission is Permission.SAFE:
        # A SAFE skill answers "did you match?" by acting, so the audit row is
        # written after the fact -- it records what happened, which is all a
        # safe action needs.
        reply = skill.handler(arg)
        if reply is not None:
            record(skill.name, "allowed", "safe")
        return reply

    # Everything below has to be recognised without acting.
    if skill.matches is None or not skill.matches(arg):
        return None

    if permission is Permission.CONFIRM:
        if _ask(skill.confirm_prompt or ""):
            reply = skill.handler(arg)
            record(skill.name, "allowed", "user_confirmed")
            return reply
        record(skill.name, "declined", "user_declined")
        return DECLINED_REPLY

    record(
        skill.name,
        "blocked",
        "deny_by_default" if permission is Permission.BLOCKED else "unknown_permission",
    )
    return BLOCKED_REPLY


# --- Audit -----------------------------------------------------------------


def record(
    action: str,
    decision: str,
    reason: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Append one audit row. Never raises, and never takes user text.

    `action` is a skill name or a fixed word, `decision` is from DECISIONS and
    `reason` from REASONS. A failed write costs a log row, never a turn --
    same discipline as memory.recall_safe().
    """
    try:
        _execute(
            "INSERT INTO audit (ts, action, decision, reason) VALUES (?, ?, ?, ?)",
            (db.now_iso(), action, decision, reason),
            conn,
        )
    except Exception as e:
        print(f"Σφάλμα καταγραφής: {e}")


# --- Storage helpers -------------------------------------------------------


def _execute(sql: str, params: tuple, conn: sqlite3.Connection | None) -> None:
    if conn is not None:
        conn.execute(sql, params)
        conn.commit()
        return
    own = db.connect()
    try:
        own.execute(sql, params)
        own.commit()
    finally:
        own.close()


def _read_state(key: str, conn: sqlite3.Connection | None = None) -> str | None:
    """The stored value, or None if it is absent *or* unreadable -- the two
    cases are deliberately the same to the caller, which falls back either
    way rather than guessing."""
    try:
        if conn is not None:
            row = conn.execute(
                "SELECT value FROM policy_state WHERE key = ?", (key,)
            ).fetchone()
        else:
            own = db.connect()
            try:
                row = own.execute(
                    "SELECT value FROM policy_state WHERE key = ?", (key,)
                ).fetchone()
            finally:
                own.close()
    except Exception:
        return None
    return None if row is None else str(row["value"])


def _write_state(key: str, value: str, conn: sqlite3.Connection | None = None) -> None:
    _execute(
        "INSERT INTO policy_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (key, value, db.now_iso()),
        conn,
    )


# --- CLI -------------------------------------------------------------------
#
# `python -m jarvis.policy unlock` from a second terminal, and `:policy
# unlock` at Jarvis's own Enter prompt, share this dispatcher -- the same
# arrangement as jarvis/mem.py.


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jarvis.policy",
        description="Δες και άλλαξε την πολιτική ασφαλείας του Τζάρβις.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="είναι παγωμένος;")
    sub.add_parser("unlock", help="ξεπάγωσε τον")
    sub.add_parser("freeze", help="πάγωσέ τον")
    return parser


def run(
    argv: list[str],
    conn: sqlite3.Connection | None = None,
    out: Callable[[str], None] = print,
) -> int:
    """Run one command. Returns a process exit code (0 = success).

    `conn` and `out` are injected so the tests drive this without a real
    database or real stdout, matching mem.run().
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:  # argparse exits on bad input; report, don't die
        return int(e.code or 2)

    owns_conn = conn is None
    conn = conn if conn is not None else db.connect()
    try:
        if args.command == "status":
            out("Παγωμένος." if is_frozen(conn) else "Κανονική λειτουργία.")
            return 0

        if args.command == "unlock":
            if not is_frozen(conn):
                out("Δεν ήταν παγωμένος.")
                return 0
            thaw("cli", conn)
            out("Ξεπάγωσε.")
            return 0

        freeze("cli", conn)
        out("Πάγωσε.")
        return 0
    finally:
        if owns_conn:
            conn.close()


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
