r"""One-off data seed for the weekly class schedule (see CLAUDE.md "The
weekly class schedule" under "Memory").

Inserts the χειμερινό εξάμηνο 2026-2027, 1ο εξάμηνο σπουδών weekly timetable
into the `class_schedule` table, using memory.save() -- the same function a
real voice save goes through -- rather than raw SQL, so each row gets the
same norm/tags/timestamps a real save would produce. (There is no voice
trigger that writes to class_schedule yet, only :mem/the CLI and this seed;
see CLAUDE.md for why.)

Source: the user's own πρόγραμμα σπουδών schedule image (χειμερινό εξάμηνο,
ακαδ. έτος 2026-2027, 1ο εξάμηνο σπουδών). Building/room legend from that
source, kept here for reference rather than stored anywhere --
class_schedule.room holds the raw code from the schedule (e.g. "Β1-Β2"),
not the expanded name:
    Β1 έως Β7    -- στα ΠΡΟΚΑΤ
    Κ-εργ        -- εργαστήρια στα ΚΙΜΜΕΡΙΑ
    ΥΚ / Τ.Μ.Π.  -- Υπολογιστικό Κέντρο Τμήματος Μηχανικών Περιβάλλοντος, Β7,
                    Κιμμέρια

class_schedule has no column for lecture type (Θεωρία/Εργαστήριο) or lab
group, unlike `courses`, which just holds the bare title
(tools/seed_courses_2026_2027_sem1.py). Where a course meets more than once
a week in a way that would otherwise collide or lose real information --
Προγραμματισμός Η/Υ's one lecture plus three separate lab groups, Υδατική
Χημεία's separate lecture/lab sessions -- that distinction is folded into
`course` itself as a parenthetical ("Προγραμματισμός Η/Υ (Ε, Ομάδα Α)")
rather than left implicit in the (weekday, start_time, room) tuple alone, so
a spoken "τι έχω σήμερα" or a :mem listing still says which session it is.
Μαθηματικά Ι and Βιολογία - Οικολογία each meet twice a week as the same
plain lecture with nothing to disambiguate, so those keep the bare course
name exactly as `courses.name` has it.

A course's own two-hour meeting always appears as one contiguous
(start_time, end_time) row here, merged from however many 45-minute slots
the source table draws it across, rather than one row per slot -- the same
level of detail «τι έχω σήμερα» would speak back.

Run once, locally, against the real database:
    .\.venv\Scripts\python.exe tools\seed_class_schedule_2026_2027_sem1.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Same fix tools/seed_courses_2026_2027_sem1.py already needed: running this
# file directly puts only its own directory on sys.path, not the project
# root, so "from jarvis import" fails with ModuleNotFoundError unless the
# root is added first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis import db, memory  # noqa: E402

SEMESTER = "1ο εξάμηνο, χειμερινό 2026-2027"

# (course, weekday, start_time, end_time, room, professor)
#
# weekday follows Python's date.weekday() convention, the same one
# class_schedule.weekday and memory.WEEKDAY_NAMES already use:
# Δευτέρα=0, Τρίτη=1, Τετάρτη=2, Πέμπτη=3, Παρασκευή=4.
#
# professor is None exactly where the schedule image names no one for that
# slot (both "Εισαγωγή..." courses) -- not a gap in transcription.
ROWS = [
    ("Μαθηματικά Ι", 2, "08:15", "11:00", "Β1-Β2",
     "Παπασχοινόπουλος, Στεφανίδου"),
    ("Μαθηματικά Ι", 3, "11:15", "14:00", "Β1-Β2",
     "Παπασχοινόπουλος, Στεφανίδου"),

    ("Εισαγωγή στην Επιστήμη του Μηχανικού Περιβάλλοντος", 1,
     "09:15", "12:00", "Β1-2", None),

    ("Προγραμματισμός Η/Υ (Θ)", 1, "15:15", "17:00", "Β1-2", "Συλαίος"),
    ("Προγραμματισμός Η/Υ (Ε, Ομάδα Α)", 2, "12:15", "14:00", "ΥΚ",
     "Συλαίος, ΜΔ Κόκκος, Κεραμέα"),
    ("Προγραμματισμός Η/Υ (Ε, Ομάδα Α2)", 2, "15:15", "17:00", "ΥΚ",
     "Συλαίος, Καρακατσάνης"),
    ("Προγραμματισμός Η/Υ (Ε, Ομάδα Α3)", 4, "09:15", "11:00", "ΥΚ",
     "Συλαίος, Κοσμαδάκης"),

    ("Βιολογία - Οικολογία", 0, "12:15", "14:00", "Β3-Β4", "Ρέμμας"),
    ("Βιολογία - Οικολογία", 0, "15:15", "17:00", "Β1-Β2", "Ρέμμας"),

    ("Εισαγωγή στις Εργαστηριακές Πρακτικές του Μηχανικού Περιβάλλοντος", 4,
     "11:15", "13:00", "Β1-2", None),

    ("Υδατική Χημεία (Ε)", 3, "15:15", "17:00", "Β1-2",
     "Χριστοφορίδης, Παπασπύρος, ΥΔ Ιωαννίδου"),
    ("Υδατική Χημεία (Θ)", 4, "15:15", "19:00", "Β1-Β2", "Χριστοφορίδης"),

    ("Φυσική Ατμόσφαιρας", 3, "17:15", "20:00", "Β1-Β2",
     "Κουρτίδης, Σταθόπουλος"),
]


def main() -> None:
    conn = db.connect()
    try:
        # Keyed on (course, weekday, start_time) rather than id, so running
        # this twice is harmless -- the same idempotency seed_courses gives
        # its own rows, keyed there on (name, semester).
        existing = {
            (row["course"], row["weekday"], row["start_time"])
            for row in conn.execute(
                "SELECT course, weekday, start_time FROM class_schedule"
                " WHERE semester = ?",
                (SEMESTER,),
            )
        }
        added = 0
        for course, weekday, start, end, room, professor in ROWS:
            day = memory.WEEKDAY_NAMES[weekday]
            if (course, weekday, start) in existing:
                print(f"  (ήδη υπάρχει) {course} -- {day} {start}")
                continue
            norm = memory.normalize(
                f"{course} {professor or ''} {room or ''} {SEMESTER} {day}"
                f" {memory._TABLE_KEYWORDS['class_schedule']}"
            )
            parsed = memory.Parsed(
                "class_schedule",
                {
                    "course": course,
                    "weekday": weekday,
                    "start_time": start,
                    "end_time": end,
                    "room": room,
                    "professor": professor,
                    "semester": SEMESTER,
                    "norm": norm,
                },
            )
            memory.save(parsed, conn)
            print(f"  + {course} -- {day} {start}-{end}")
            added += 1
        print(f"\n{added} νέες ώρες μαθημάτων αποθηκεύτηκαν για: {SEMESTER}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
