"""Command-line access to Jarvis's memory: `python -m jarvis.mem <command>`.

The same dispatcher backs the `:mem ...` command at Jarvis's Enter prompt, so
both entry points share one implementation and one set of tests.

Running this in a second terminal works *while Jarvis is running*, including
in wake-word mode where there is no prompt to type at: it is a separate
process against the same WAL-mode database (see jarvis/db.py).

Everything that writes goes through the same sensitivity check as the voice
path -- the CLI is not a way around it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Callable

from jarvis import db, memory
from jarvis.text import is_yes

# Listable/inspectable tables. audit is included read-only; it is evidence,
# so `edit` and `del` refuse it.
_READABLE = tuple(db.CONTENT_TABLES) + ("audit",)
_WRITABLE = tuple(db.CONTENT_TABLES)

# Never editable from the CLI: identity and provenance. topic_key is
# expertise's identity column (its upsert key, see memory.save_expertise()) --
# editing it directly would desync it from `topic`, so only `topic` itself is
# editable and topic_key stays derived.
_PROTECTED_COLUMNS = {"id", "created_at", "norm", "topic_key"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jarvis.mem",
        description="Δες και διαχειρίσου τη μνήμη του Τζάρβις.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="δες τις εγγραφές ενός πίνακα")
    p_list.add_argument("table", nargs="?", choices=_READABLE)
    p_list.add_argument("--limit", type=int, default=20)

    p_show = sub.add_parser("show", help="δες μία εγγραφή αναλυτικά")
    p_show.add_argument("table", choices=_READABLE)
    p_show.add_argument("row_id", type=int)

    p_edit = sub.add_parser("edit", help="άλλαξε ένα πεδίο μιας εγγραφής")
    p_edit.add_argument("table", choices=_WRITABLE)
    p_edit.add_argument("row_id", type=int)
    p_edit.add_argument("field")
    p_edit.add_argument("value")

    p_del = sub.add_parser("del", help="σβήσε μια εγγραφή")
    p_del.add_argument("table", choices=_WRITABLE)
    p_del.add_argument("row_id", type=int)
    p_del.add_argument("--yes", action="store_true", help="χωρίς επιβεβαίωση")

    p_export = sub.add_parser("export", help="εξαγωγή όλων σε JSON")
    p_export.add_argument("--out", default=None)

    sub.add_parser("backup", help="αντίγραφο ασφαλείας τώρα")

    return parser


def run(
    argv: list[str],
    conn: sqlite3.Connection | None = None,
    out: Callable[[str], None] = print,
    confirm: Callable[[str], str] | None = None,
) -> int:
    """Run one command. Returns a process exit code (0 = success).

    `conn`, `out` and `confirm` are injected so the tests drive this without
    a real database, real stdout, or a real prompt.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:  # argparse exits on bad input; report, don't die
        return int(e.code or 2)

    owns_conn = conn is None
    conn = conn if conn is not None else db.connect()
    try:
        handler = _HANDLERS[args.command]
        return handler(args, conn, out, confirm)
    finally:
        if owns_conn:
            conn.close()


def _cmd_list(args, conn, out, confirm) -> int:
    tables = [args.table] if args.table else list(_READABLE)
    for table in tables:
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (args.limit,)
        ).fetchall()
        out(f"--- {table} ({len(rows)}) ---")
        for row in rows:
            out(f"  [{row['id']}] {_summarize(table, row)}")
    return 0


def _cmd_show(args, conn, out, confirm) -> int:
    row = _fetch(conn, args.table, args.row_id)
    if row is None:
        out(f"Δεν βρέθηκε {args.table} #{args.row_id}.")
        return 1
    for key in row.keys():
        out(f"  {key}: {row[key]}")
    return 0


def _cmd_edit(args, conn, out, confirm) -> int:
    row = _fetch(conn, args.table, args.row_id)
    if row is None:
        out(f"Δεν βρέθηκε {args.table} #{args.row_id}.")
        return 1

    if args.field in _PROTECTED_COLUMNS or args.field not in row.keys():
        out(f"Το πεδίο '{args.field}' δεν αλλάζει.")
        return 1

    # The CLI is not a bypass: the same rule applies as to the voice path.
    if memory.is_sensitive(args.value):
        out("Δεν αποθηκεύω κωδικούς ή αριθμούς κάρτας.")
        return 1

    conn.execute(
        f"UPDATE {args.table} SET {args.field} = ?, updated_at = ? WHERE id = ?",
        (args.value, db.now_iso(), args.row_id),
    )
    _refresh_norm(conn, args.table, args.row_id)
    conn.commit()
    out(f"Ενημερώθηκε {args.table} #{args.row_id}: {args.field} = {args.value}")
    return 0


def _cmd_del(args, conn, out, confirm) -> int:
    row = _fetch(conn, args.table, args.row_id)
    if row is None:
        out(f"Δεν βρέθηκε {args.table} #{args.row_id}.")
        return 1

    if not args.yes:
        # Read back what is about to go, then require an explicit yes.
        out(f"Να σβήσω {args.table} #{args.row_id}: {_summarize(args.table, row)}")
        answer = (confirm or input)("Σίγουρα; [ν/o] ")
        # Anything unrecognised is a no: deleting is not the safe default.
        # Shared with the voice confirmation in policy.py, so a yes means the
        # same thing whichever way it arrives (jarvis/text.py).
        if not is_yes(answer):
            out("Ακυρώθηκε.")
            return 1

    conn.execute(f"DELETE FROM {args.table} WHERE id = ?", (args.row_id,))
    conn.commit()
    out(f"Σβήστηκε {args.table} #{args.row_id}.")
    return 0


def _cmd_export(args, conn, out, confirm) -> int:
    payload = {
        table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in _READABLE
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        out(f"Εξήχθη στο {args.out}.")
    else:
        out(text)
    return 0


def _cmd_backup(args, conn, out, confirm) -> int:
    path = db.backup()
    if path is None:
        out("Δεν υπάρχει ακόμα βάση για αντίγραφο.")
        return 1
    out(f"Αντίγραφο: {path}")
    return 0


_HANDLERS = {
    "list": _cmd_list,
    "show": _cmd_show,
    "edit": _cmd_edit,
    "del": _cmd_del,
    "export": _cmd_export,
    "backup": _cmd_backup,
}

def _fetch(conn: sqlite3.Connection, table: str, row_id: int) -> sqlite3.Row | None:
    return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()


def _summarize(table: str, row: sqlite3.Row) -> str:
    """One-line rendering, used by `list` and by the delete read-back."""
    if table == "audit":
        return f"{row['ts']} {row['action']} -> {row['decision']} ({row['reason']})"
    if table == "profile":
        return f"{row['key']} = {row['value']}"
    if table == "exams":
        return f"{row['due_date']} {row['course']}" + (f" ({row['topic']})" if row["topic"] else "")
    if table == "businesses":
        return f"{row['name'] or '(χωρίς όνομα)'}: {row['note']}"
    if table == "reminders":
        return f"{row['due_at']} [{row['status']}] {row['text']}"
    if table == "courses":
        return f"{row['name']}" + (f" ({row['semester']})" if row["semester"] else "")
    if table == "expertise":
        return f"{row['topic']}: {row['summary']}"
    return str(row[db.CONTENT_TABLES.get(table, "id")])


def _refresh_norm(conn: sqlite3.Connection, table: str, row_id: int) -> None:
    """Keep the search column in step with an edited value, or the row would
    stay findable only under its old wording."""
    row = _fetch(conn, table, row_id)
    if row is None or "norm" not in row.keys():
        return

    parts = [str(row[col]) for col in
             ("key", "name", "course", "topic", "note", "text", "value", "summary")
             if col in row.keys() and row[col]]
    conn.execute(
        f"UPDATE {table} SET norm = ? WHERE id = ?",
        (memory.normalize(" ".join(parts)), row_id),
    )


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
