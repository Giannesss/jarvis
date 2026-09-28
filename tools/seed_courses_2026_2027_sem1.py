"""One-off data seed for Phase 5 step 5 (see CLAUDE.md "Roadmap").

Inserts the 1st-semester course list from the user's own program-of-studies
schedule (χειμερινό εξάμηνο, ακαδ. έτος 2026-2027) into the `courses` table,
using memory.save() -- the same function a real voice save goes through --
rather than raw SQL, so the row gets the same norm/tags/timestamps a spoken
"Θυμήσου ότι κάνω μάθημα ..." would produce.

Bypasses memory.parse()'s trigger-matching on purpose: these are long,
comma-heavy course names ("Εισαγωγή στην Επιστήμη του Μηχανικού
Περιβάλλοντος") that are exactly the shape RE_COURSE_OF's punctuation-based
end-of-name boundary is fragile against, and there is no ambiguity to
resolve here -- these are the real courses, out of a real program-of-studies
document, not a sentence to guess a trigger out of.

Known limitation, on purpose: the source schedule carries day/time/room/
professor detail (e.g. "Μαθηματικά Ι, Δευτέρα-Τετάρτη-Πέμπτη, 8:15-9:00,
Παπασχοινόπουλος/Στεφανίδου, Β1-Β2"). The `courses` table has no columns for
any of that -- only `name` and `semester` -- so only the course names are
saved here. A weekly recurring schedule is a different shape of data from
`exams`/`reminders` (which are single dated events the agenda already
handles), and would need its own table and its own agenda wiring to be
worth anything spoken aloud. Flagged rather than silently dropped or
silently bolted on.

Run once, locally, against the real database:
    .\.venv\Scripts\python.exe tools\seed_courses_2026_2027_sem1.py
"""

from __future__ import annotations

from jarvis import db, memory

SEMESTER = "1ο εξάμηνο, χειμερινό 2026-2027"

# Distinct course names off the schedule image, in the order they first
# appear (ΔΕΥΤΕΡΑ column down, then ΤΡΙΤΗ, ...). Kept verbatim except for
# capitalization (the schedule is in full caps; Title Case reads better
# spoken back and saved -- memory.normalize() folds either way for search).
COURSES = [
    "Μαθηματικά Ι",
    "Εισαγωγή στην Επιστήμη του Μηχανικού Περιβάλλοντος",
    "Προγραμματισμός Η/Υ",
    "Βιολογία - Οικολογία",
    "Εισαγωγή στις Εργαστηριακές Πρακτικές του Μηχανικού Περιβάλλοντος",
    "Υδατική Χημεία",
    "Φυσική Ατμόσφαιρας",
]


def main() -> None:
    conn = db.connect()
    try:
        existing = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM courses WHERE semester = ?", (SEMESTER,)
            )
        }
        added = 0
        for name in COURSES:
            if name in existing:
                print(f"  (ήδη υπάρχει) {name}")
                continue
            parsed = memory.Parsed(
                "courses",
                {
                    "name": name,
                    "semester": SEMESTER,
                    "norm": memory.normalize(
                        f"{name} {SEMESTER} {memory._TABLE_KEYWORDS['courses']}"
                    ),
                },
            )
            memory.save(parsed, conn)
            print(f"  + {name}")
            added += 1
        print(f"\n{added} νέα μαθήματα αποθηκεύτηκαν για: {SEMESTER}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
