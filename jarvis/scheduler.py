"""Reminders and timers that survive a restart.

The `reminders` table has carried `due_at`, `kind`, `status` and `fired_at`
since it was created, and nothing ever fired them: `status` was only ever
written as "pending", `fired_at` never written at all. A reminder set for 5pm
was stored and then surfaced only if the brain happened to be asked something
that recalled it. This module is the half that was missing.

Three decisions shape everything here:

  * **A row is claimed before it is announced.** The claim is an UPDATE
    guarded on `status = 'pending'`, and the announcement happens only if it
    changed exactly one row. That makes a double-announcement impossible even
    with two Jarvis processes against the same database -- which WAL mode
    deliberately allows, and which `:mem` in a second terminal already relies
    on.

  * **Claim first, speak second.** A crash in between loses that one
    announcement. The other order risks the realistic failure instead: if
    speak() is itself what is broken (no audio device), speaking before
    marking would re-announce the same reminder every tick forever.

  * **A freeze stops the clock, it does not eat it.** While the kill switch
    is on, a tick claims nothing and announces nothing, so the reminders are
    still pending when it is lifted. Announcing would break the freeze;
    claiming silently would make the freeze destroy data.

Nothing here imports speaker or listener. The announcement is an injected
callable (`set_announcer`), the same way policy.py takes its confirm asker,
so the whole module is testable without a microphone.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Iterator

from jarvis import db, diag, policy
from jarvis.config import SCHEDULER_CATCHUP_LIMIT, SCHEDULER_ENABLED, SCHEDULER_TICK
from jarvis.text import normalize

# --- Statuses --------------------------------------------------------------
#
# Fixed strings, like policy's DECISIONS/REASONS, so the column can be grepped
# and `:mem list reminders` reads consistently.
PENDING = "pending"  # written by memory.parse() and schedule()
FIRED = "fired"      # announced on time
MISSED = "missed"    # came due while Jarvis was not running

# Kinds. "reminder" is what memory.parse() writes; "timer" is a countdown,
# whose `text` is already the whole sentence to say.
REMINDER = "reminder"
TIMER = "timer"


# --- What Jarvis says ------------------------------------------------------

CATCHUP_PREFIX = "Όσο ήμουν κλειστός"


def _reminder_line(text: str) -> str:
    return f"Υπενθύμιση: {text}"


def _catchup_line(reminders: list[str], timers: int) -> str:
    """One sentence for everything that came due while Jarvis was off.

    Announced as a group rather than fired one after another: five reminders
    replayed back to back on startup is a wall of speech nobody listens to.
    The count is always spoken even when the text is not, so a reminder is
    never silently dropped -- and every row stays in the table as `missed`,
    readable with `:mem list reminders`.
    """
    if reminders:
        shown = reminders[:SCHEDULER_CATCHUP_LIMIT]
        line = f"{CATCHUP_PREFIX}, έχασες: " + ", ".join(shown)

        extra = len(reminders) - len(shown)
        if extra == 1:
            line += " και άλλη μία υπενθύμιση"
        elif extra > 1:
            line += f" και άλλες {extra} υπενθυμίσεις"
        line += "."

        if timers == 1:
            line += " Έληξε επίσης ένα χρονόμετρο."
        elif timers > 1:
            line += f" Έληξαν επίσης {timers} χρονόμετρα."
        return line

    # Timers only. A countdown from yesterday has no content worth replaying,
    # so it is counted rather than read out -- but it is still reported.
    if timers == 1:
        return f"{CATCHUP_PREFIX} έληξε ένα χρονόμετρο."
    return f"{CATCHUP_PREFIX} έληξαν {timers} χρονόμετρα."


# --- The injected voice ----------------------------------------------------

_announcer: Callable[[str], None] | None = None


def set_announcer(announcer: Callable[[str], None] | None) -> None:
    """Install what says a reminder out loud (main() passes speaker.speak)."""
    global _announcer
    _announcer = announcer


def _say(message: str) -> None:
    """Announce, and never let a failing voice kill the scheduler thread.

    speaker.speak() serializes on its own lock, so a reminder firing while
    Jarvis is already talking waits its turn rather than overlapping -- the
    same guarantee the old threading.Timer announcement relied on.
    """
    diag.log(f"[sched] {message}")
    if _announcer is None:
        return
    try:
        _announcer(message)
    except Exception as e:
        print(f"Σφάλμα εκφώνησης υπενθύμισης: {e}")


# --- Rows ------------------------------------------------------------------


@contextmanager
def _connection(conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    """Use the caller's connection, or open one for the length of the call.

    The thread holds its own for its lifetime (db.connect() re-runs the whole
    schema script, which is not something to do every couple of seconds); the
    one-shot callers and the tests pass theirs in.
    """
    if conn is not None:
        yield conn
        return

    owned = db.connect()
    try:
        yield owned
    finally:
        owned.close()


def _due(conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    """Pending rows that have come due, oldest first.

    `due_at <= now` is the whole filter, and it is what keeps a future-dated
    row untouched: it stays pending, stays out of every announcement, and is
    still there tomorrow. A test pins that.
    """
    return conn.execute(
        "SELECT id, text, kind, due_at FROM reminders "
        "WHERE status = ? AND due_at <= ? ORDER BY due_at",
        (PENDING, now.isoformat(timespec="seconds")),
    ).fetchall()


def _claim(conn: sqlite3.Connection, row_id: int, status: str) -> bool:
    """Take ownership of one row, or report that someone else already has.

    Guarded on `status = 'pending'`, so the row can be claimed exactly once
    however many processes are looking at it.
    """
    stamp = db.now_iso()
    cursor = conn.execute(
        "UPDATE reminders SET status = ?, fired_at = ?, updated_at = ? "
        "WHERE id = ? AND status = ?",
        (status, stamp, stamp, row_id, PENDING),
    )
    conn.commit()
    return cursor.rowcount == 1


def schedule(
    text: str,
    due_at: datetime,
    kind: str = REMINDER,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Store one future announcement and return its row id.

    `text` is kept verbatim, like every other display column (see CLAUDE.md
    "Verbatim captures"); `norm` beside it is what search matches on.
    """
    stamp = db.now_iso()
    with _connection(conn) as active:
        cursor = active.execute(
            "INSERT INTO reminders "
            "(text, norm, due_at, kind, referent_date, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                text,
                normalize(text),
                due_at.isoformat(timespec="seconds"),
                kind,
                PENDING,
                stamp,
                stamp,
            ),
        )
        active.commit()
        return int(cursor.lastrowid)


# --- Firing ----------------------------------------------------------------


def tick(
    now: datetime | None = None, conn: sqlite3.Connection | None = None
) -> list[str]:
    """Announce everything that has come due. Returns what was said.

    Claims are taken while the connection is held and the announcements are
    made after it is released: speaking takes seconds, and holding a write
    transaction across it would block `:mem` in the other terminal for just
    as long.
    """
    now = now or datetime.now()
    lines = []

    with _connection(conn) as active:
        # Asked on the connection we already hold: is_frozen() opens its own
        # otherwise, and db.connect() re-runs the whole schema script -- not
        # something to do every couple of seconds forever.
        if policy.is_frozen(active):
            # The kill switch freezes everything except shutdown. Claiming
            # here would mark these fired without a word being said, so a
            # freeze would quietly destroy them; instead they stay pending
            # until it is lifted.
            return []

        for row in _due(active, now):
            if _claim(active, row["id"], FIRED):
                lines.append(
                    row["text"] if row["kind"] == TIMER else _reminder_line(row["text"])
                )

    for line in lines:
        _say(line)
    return lines


def catch_up(
    now: datetime | None = None, conn: sqlite3.Connection | None = None
) -> str | None:
    """Report everything that came due while Jarvis was not running.

    Called once at startup, before the loop. Returns the announcement, or
    None when nothing was missed.
    """
    now = now or datetime.now()

    with _connection(conn) as active:
        claimed = [row for row in _due(active, now) if _claim(active, row["id"], MISSED)]

    if not claimed:
        return None

    # Newest first: the most recently missed is the one still worth hearing.
    reminders = [
        row["text"] for row in sorted(claimed, key=lambda r: r["due_at"], reverse=True)
        if row["kind"] != TIMER
    ]
    timers = sum(1 for row in claimed if row["kind"] == TIMER)

    message = _catchup_line(reminders, timers)
    _say(message)
    return message


# --- The thread ------------------------------------------------------------

_thread: threading.Thread | None = None
_stop = threading.Event()


def _run() -> None:
    """Poll rather than arm a timer per row, because a timer object cannot
    survive a restart -- which is the entire requirement. Polling also means
    a reminder added from a second terminal, or edited with `:mem edit`, is
    picked up on the next tick with no further wiring.

    The connection is opened here and held: it belongs to this thread (sqlite3
    connections are thread-bound), and db.connect() re-runs the schema script
    on every call, which is not a thing to do every couple of seconds.
    """
    conn = None
    try:
        conn = db.connect()
        while not _stop.wait(SCHEDULER_TICK):
            try:
                tick(conn=conn)
            except Exception as e:
                # A bad tick costs one round, never the thread: a scheduler
                # that dies silently is worse than one that misses a beat.
                print(f"Σφάλμα χρονοπρογραμματιστή: {e}")
    except Exception as e:
        print(f"Ο χρονοπρογραμματιστής σταμάτησε: {e}")
    finally:
        if conn is not None:
            conn.close()


def start(announcer: Callable[[str], None] | None = None) -> None:
    """Catch up on what was missed, then start ticking.

    The catch-up runs on the caller's thread, before the loop, so it is heard
    right after "Jarvis έτοιμος" rather than arriving a tick later.
    """
    global _thread

    if announcer is not None:
        set_announcer(announcer)

    if not SCHEDULER_ENABLED:
        return

    catch_up()

    if _thread is not None and _thread.is_alive():
        return

    _stop.clear()
    _thread = threading.Thread(target=_run, name="jarvis-scheduler", daemon=True)
    _thread.start()


def stop() -> None:
    """Ask the thread to finish the current wait and exit.

    Daemon either way, so a pending tick can never hang process exit; this
    just makes a clean shutdown clean.
    """
    global _thread

    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2)
        _thread = None
