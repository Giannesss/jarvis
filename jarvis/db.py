"""SQLite storage for Jarvis's persistent memory. See CLAUDE.md "Memory".

One file, data/jarvis.db, opened in WAL mode so the running assistant and a
second terminal's `python -m jarvis.mem` can use it at the same time -- that
is what makes the CLI usable while wake-word mode is holding the microphone.

Timestamps are local ISO-8601 to second precision throughout. This is a
single-user, single-machine assistant that reasons about "today" and "στις 5
το απόγευμα"; storing UTC would mean converting on every read for no benefit.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from jarvis.config import BACKUP_DIR, BACKUP_KEEP, DB_PATH
from jarvis.text import fold_iotacism

# 2: text.normalize() began folding the iotacism vowels, so every stored
# `norm` written before that has to be refolded -- see _migrate().
#
# This tracks *data* migrations, not the shape of the schema. Adding a table
# needs no bump: every statement below is CREATE ... IF NOT EXISTS and _init()
# runs the whole script on every connect, so an existing database picks up a
# new table on its next start (that is how `audit` and `policy_state` both
# arrived). Bumping it would only re-run the refold above for nothing.
SCHEMA_VERSION = 3

# Tables carrying free text also carry a `norm` column: text.normalize() of
# whatever should be searchable in that row. Recall matches against it with
# LIKE, so the stored text stays readable while the search stays
# accent/case-insensitive. See memory.recall().
_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS profile (
    id         INTEGER PRIMARY KEY,
    key        TEXT NOT NULL UNIQUE,
    value      TEXT NOT NULL,
    norm       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    tags       TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    norm       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    tags       TEXT
);

CREATE TABLE IF NOT EXISTS semesters (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    semester    TEXT,
    norm        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    tags        TEXT
);

CREATE TABLE IF NOT EXISTS exams (
    id         INTEGER PRIMARY KEY,
    due_date   TEXT NOT NULL,
    course     TEXT NOT NULL,
    topic      TEXT,
    norm       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    tags       TEXT
);

-- name is deliberately nullable: "η επιχείρησή μου χρειάζεται λογιστή" names
-- no business, and NULL says "no name given" unambiguously where "" would be
-- indistinguishable from a name that happens to be empty.
CREATE TABLE IF NOT EXISTS businesses (
    id         INTEGER PRIMARY KEY,
    name       TEXT,
    note       TEXT NOT NULL,
    norm       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    tags       TEXT
);

CREATE TABLE IF NOT EXISTS reminders (
    id            INTEGER PRIMARY KEY,
    text          TEXT NOT NULL,
    norm          TEXT NOT NULL,
    due_at        TEXT NOT NULL,
    kind          TEXT NOT NULL,
    referent_date TEXT,
    status        TEXT NOT NULL,
    fired_at      TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    tags          TEXT
);

-- Deliberately minimal: timestamp, what was attempted, what was decided, and
-- a short fixed reason code. Never a transcript of what was said.
CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    action   TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason   TEXT
);

-- Policy state shared between processes: currently just the kill switch's
-- frozen flag. It lives in the database rather than in memory so that
-- `python -m jarvis.policy unlock` in a second terminal reaches a *running*
-- Jarvis, the same way `python -m jarvis.mem` reaches its memory (WAL mode is
-- what makes both work). main() clears it at startup, because a restart has
-- to unlock too and a persisted flag would otherwise outlive the process
-- that set it. See jarvis/policy.py.
CREATE TABLE IF NOT EXISTS policy_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- A dated "expertise" row (Phase 5 step 2): the structured summary
-- research() produced for one topic, not the search transcript itself.
-- topic_key is the topic alone, normalized, and UNIQUE -- researching the
-- same topic again (the "ξανακάνε έρευνα για..." refresh trigger) updates
-- this row in place rather than piling up duplicate summaries. norm covers
-- topic *and* summary, so a keyword search over unrelated wording can still
-- surface it -- see memory.save_expertise() and memory._SEARCHABLE.
CREATE TABLE IF NOT EXISTS expertise (
    id         INTEGER PRIMARY KEY,
    topic      TEXT NOT NULL,
    topic_key  TEXT NOT NULL UNIQUE,
    summary    TEXT NOT NULL,
    norm       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    tags       TEXT
);

CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (status, due_at);
CREATE INDEX IF NOT EXISTS idx_exams_due ON exams (due_date);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit (ts);
"""

# Tables the CLI and recall are allowed to touch, and the column holding each
# one's human-readable text. Anything not listed here is not addressable by
# name from the outside -- which is what keeps `:mem del <table> <id>` from
# being pointed at schema_version.
CONTENT_TABLES = {
    "profile": "value",
    "notes": "text",
    "courses": "name",
    "exams": "course",
    "businesses": "note",
    "reminders": "text",
    "expertise": "summary",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the memory database, schema applied.

    WAL mode plus a busy timeout is what lets a second process run
    `python -m jarvis.mem` against the same file while Jarvis is running.
    """
    db_path = Path(path) if path is not None else Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _init(conn)
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        # A database created right now is already at the current version:
        # every table is empty, so there is nothing to migrate.
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    elif row["version"] < SCHEMA_VERSION:
        _migrate(conn, row["version"])
    conn.commit()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row["name"] == column
        for row in conn.execute(f"PRAGMA table_info({table})")
    )


def _migrate(conn: sqlite3.Connection, version: int) -> None:
    """Bring an existing database up to SCHEMA_VERSION, in place.

    Runs inside connect(), so it happens on the next start after an upgrade
    rather than needing a command of its own.
    """
    if version < 2:
        # normalize() now folds the iotacism vowels (text.fold_iotacism), so
        # a query stemmed from speech is folded while these stored search
        # keys are not, and the LIKE match silently stops finding them.
        #
        # Refolding the stored `norm` is exact rather than approximate:
        # folding is the only step added to normalize(), so
        # fold(old_norm) == the new normalize() of the same source text.
        # That matters because a row's `norm` is often built from more than
        # the columns kept beside it (an exam's includes the whole
        # utterance), and so cannot be rebuilt from the row itself.
        for table in CONTENT_TABLES:
            rows = conn.execute(f"SELECT id, norm FROM {table}").fetchall()
            conn.executemany(
                f"UPDATE {table} SET norm = ? WHERE id = ?",
                [(fold_iotacism(row["norm"]), row["id"]) for row in rows],
            )

    if version < 3:
        # A new *table* rides in on _SCHEMA's CREATE ... IF NOT EXISTS -- that
        # is how audit and policy_state arrived without a version bump. A new
        # *column* cannot: IF NOT EXISTS skips the entire statement for a
        # table that already exists, so nothing in _SCHEMA can ever reach an
        # existing one. An ALTER is the only way, and needing it is exactly
        # what earns this step a version of its own.
        #
        # ALTER TABLE ... ADD COLUMN is cheap in SQLite (no table rewrite) and
        # leaves existing rows NULL, so the backfill below is what makes tags
        # useful on the first run rather than only for rows saved afterwards.
        #
        # The import is deferred because memory.py imports db.py at module
        # level. It runs once, inside connect(), and only for a database
        # written before tags existed.
        from jarvis.memory import tags_for

        for table in CONTENT_TABLES:
            # Conditional, because _migrate() is not transactional across its
            # steps: a crash between the ALTER and the version stamp would
            # otherwise make every later start fail on a duplicate column,
            # with the database unreachable and no way back short of editing
            # it by hand.
            if not _has_column(conn, table, "tags"):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN tags TEXT")
            rows = conn.execute(f"SELECT id, norm FROM {table}").fetchall()
            conn.executemany(
                f"UPDATE {table} SET tags = ? WHERE id = ?",
                [(tags_for(table, row["norm"]), row["id"]) for row in rows],
            )

    conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))


def backup(
    source: str | Path | None = None,
    backup_dir: str | Path | None = None,
    keep: int | None = None,
) -> Path | None:
    """Copy the database with SQLite's backup API, then prune to the newest
    `keep` files. Returns the new backup's path, or None if there is no
    database to copy yet.

    The backup API is used rather than a file copy because it is safe against
    a concurrent writer -- the whole point of running this at startup while
    the previous session may not have shut down cleanly.
    """
    src = Path(source) if source is not None else Path(DB_PATH)
    if not src.exists():
        return None

    dest_dir = Path(backup_dir) if backup_dir is not None else Path(BACKUP_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest = _unique_backup_path(dest_dir)

    source_conn = sqlite3.connect(src, timeout=5)
    try:
        target_conn = sqlite3.connect(dest)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    finally:
        source_conn.close()

    _prune(dest_dir, keep if keep is not None else BACKUP_KEEP)
    return dest


def _unique_backup_path(dest_dir: Path) -> Path:
    """Microsecond-stamped, so names are unique and sort chronologically.

    Two coarser schemes were tried and are wrong. A second-precision stamp
    collides outright (startup plus an immediate ":mem backup"). Adding a
    "first free counter" fixes the collision but reintroduces the bug from
    the other side: _prune deletes the oldest files, freeing low counters
    that the next backup in the same second then reuses, overwriting a
    backup and scrambling the ordering _prune depends on. A stamp that never
    repeats avoids both.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return dest_dir / f"jarvis-{stamp}.db"


def _prune(dest_dir: Path, keep: int) -> None:
    # Names start with a zero-padded timestamp, so lexical order is
    # chronological order.
    backups = sorted(dest_dir.glob("jarvis-*.db"))
    for stale in backups[: max(0, len(backups) - keep)]:
        try:
            stale.unlink()
        except OSError:
            pass  # a locked or already-removed backup must not break startup
