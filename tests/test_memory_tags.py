"""Tests for tags and the cross-table agenda (Phase 4 step 4).

A tag is a row's life area, so «τι έχω σήμερα» can pull from every table at
once instead of needing to know which table holds what. Three things here are
worth pinning beyond the happy path:

  * tagging is **additive** -- a row nothing matches keeps no tag, still lands
    in the same table, and is still found by keyword search. A wrong tag would
    be the expensive failure this codebase keeps guarding against: nobody sees
    it until a recall reads it back;
  * the stored form is comma-**terminated**, which is what stops a LIKE for
    one tag matching another that contains it;
  * the migration is the first that needed a real ALTER, and it backfills.
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from jarvis import db, memory, policy, skills


class TagVocabularyTests(unittest.TestCase):
    """Pure functions: no database, no I/O."""

    def test_a_table_implies_its_own_tag(self) -> None:
        # Exact and free: a course is university whatever its text says.
        self.assertEqual(memory.tags_for("courses", "τιποτα σχετικο"), ",university,")
        self.assertEqual(memory.tags_for("exams", "τιποτα σχετικο"), ",university,")
        self.assertEqual(memory.tags_for("businesses", "τιποτα"), ",business,")

    def test_keywords_tag_a_plain_note(self) -> None:
        self.assertEqual(
            memory.tags_for("notes", memory.normalize("να παραγγείλω καφέ")),
            ",cafe,",
        )

    def test_a_row_can_carry_more_than_one_tag(self) -> None:
        tags = memory.tags_for("notes", memory.normalize("ο καφές για το εστιατόριο"))
        self.assertEqual(memory.unpack_tags(tags), ["cafe", "restaurant"])

    def test_a_table_tag_and_a_keyword_tag_combine_without_repeating(self) -> None:
        tags = memory.tags_for(
            "courses", memory.normalize("μάθημα στατιστικής για το μάρκετινγκ")
        )
        self.assertEqual(memory.unpack_tags(tags), ["university", "ai_marketing"])

    def test_an_unmatched_row_gets_no_tag_at_all(self) -> None:
        """Additive only. NULL rather than "" says "no tag" unambiguously,
        the same choice businesses.name makes."""
        self.assertIsNone(memory.tags_for("notes", memory.normalize("να πάρω ψωμί")))

    def test_the_stored_form_is_comma_terminated(self) -> None:
        self.assertEqual(memory.pack_tags(["cafe", "university"]), ",cafe,university,")
        self.assertIsNone(memory.pack_tags([]))
        self.assertEqual(memory.unpack_tags(None), [])

    def test_a_tag_like_pattern_cannot_match_a_tag_containing_it(self) -> None:
        """The whole reason for the sentinel commas: without them a LIKE for
        "marketing" would match a row tagged "ai_marketing"."""
        packed = ",ai_marketing,"
        self.assertTrue(re.fullmatch(r"%,ai_marketing,%", memory.tag_like("ai_marketing")))

        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE t (tags TEXT)")
        conn.execute("INSERT INTO t VALUES (?)", (packed,))

        hit = conn.execute(
            "SELECT 1 FROM t WHERE tags LIKE ?", (memory.tag_like("ai_marketing"),)
        ).fetchone()
        miss = conn.execute(
            "SELECT 1 FROM t WHERE tags LIKE ?", (memory.tag_like("marketing"),)
        ).fetchone()

        self.assertIsNotNone(hit)
        self.assertIsNone(miss)


class TagOfQueryTests(unittest.TestCase):
    def test_one_named_area_is_the_filter(self) -> None:
        self.assertEqual(memory.tag_of_query("τι έχω σήμερα για το μαγαζί"), "business")

    def test_no_named_area_means_no_filter(self) -> None:
        self.assertIsNone(memory.tag_of_query("τι έχω σήμερα"))

    def test_two_named_areas_mean_no_filter(self) -> None:
        """Filtering by one of two named areas answers a question nobody
        asked; showing everything is the recoverable direction."""
        self.assertIsNone(memory.tag_of_query("τι έχω για το μαγαζί και τη σχολή"))


class MemoryDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "jarvis.db"

        patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)

        policy._frozen = False
        self.addCleanup(lambda: setattr(policy, "_frozen", False))


class SaveTaggingTests(MemoryDbTestCase):
    def test_a_saved_note_carries_its_tag(self) -> None:
        parsed = memory.parse("Θυμήσου ότι πρέπει να παραγγείλω καφέ")
        row_id = memory.save(parsed, self.conn)

        row = self.conn.execute(
            f"SELECT tags FROM {parsed.table} WHERE id = ?", (row_id,)
        ).fetchone()
        self.assertIn("cafe", memory.unpack_tags(row["tags"]))

    def test_a_profile_row_is_tagged_too(self) -> None:
        """profile has an explicit column list rather than going through the
        generic INSERT, so it is the one that silently misses a new column."""
        parsed = memory.parse("Θυμήσου ότι η σχολή μου είναι το ΕΚΠΑ")
        row_id = memory.save(parsed, self.conn)

        row = self.conn.execute(
            "SELECT tags FROM profile WHERE id = ?", (row_id,)
        ).fetchone()
        self.assertIn("university", memory.unpack_tags(row["tags"]))

    def test_an_untagged_save_still_works_and_is_still_found(self) -> None:
        memory.save(memory.parse("Θυμήσου ότι πρέπει να πάρω ψωμί"), self.conn)

        row = self.conn.execute("SELECT tags FROM notes").fetchone()
        self.assertIsNone(row["tags"])
        # Untouched by tagging: keyword search finds it exactly as before.
        self.assertTrue(memory.search("ψωμί", self.conn))


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "jarvis.db"

    def _legacy_database(self) -> None:
        """A version-2 database: today's schema with the tags column removed,
        derived from _SCHEMA itself so it cannot drift away from it."""
        legacy = re.sub(r",\n\s*tags\s+TEXT", "", db._SCHEMA)
        self.assertNotIn("tags", legacy)

        conn = sqlite3.connect(self.db_path)
        conn.executescript(legacy)
        conn.execute("INSERT INTO schema_version (version) VALUES (2)")
        conn.execute(
            "INSERT INTO notes (text, norm, created_at, updated_at)"
            " VALUES ('να παραγγείλω καφέ', 'να παραγγιλω καφε', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO courses (name, norm, created_at, updated_at)"
            " VALUES ('Στατιστική', 'στατιστικι μαθιμα', '2026-01-01', '2026-01-01')"
        )
        conn.commit()
        conn.close()

    def test_the_column_is_added_and_backfilled(self) -> None:
        """_SCHEMA's CREATE ... IF NOT EXISTS can add a table to an existing
        database but never a column, which is what earned this a version of
        its own. The backfill is what makes tags useful on the first run."""
        self._legacy_database()

        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)

        self.assertTrue(db._has_column(conn, "notes", "tags"))
        self.assertEqual(
            conn.execute("SELECT version FROM schema_version").fetchone()["version"],
            db.SCHEMA_VERSION,
        )

        note = conn.execute("SELECT tags FROM notes").fetchone()
        course = conn.execute("SELECT tags FROM courses").fetchone()
        self.assertIn("cafe", memory.unpack_tags(note["tags"]))
        self.assertIn("university", memory.unpack_tags(course["tags"]))

    def test_migrating_twice_is_harmless(self) -> None:
        """_migrate() is not transactional across its steps, so a crash
        between the ALTER and the version stamp would re-run it. That must
        not brick the database on a duplicate column."""
        self._legacy_database()

        conn = db.connect(self.db_path)
        conn.execute("UPDATE schema_version SET version = 2")
        conn.commit()
        conn.close()

        conn = db.connect(self.db_path)  # must not raise
        self.addCleanup(conn.close)
        self.assertTrue(db._has_column(conn, "notes", "tags"))


class AgendaTests(MemoryDbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.today = date(2026, 9, 24)
        self.now = datetime(2026, 9, 24, 9, 0, 0)

    def _reminder(self, text, day, status="pending", tags=None) -> None:
        self.conn.execute(
            "INSERT INTO reminders"
            " (text, norm, due_at, kind, status, tags, created_at, updated_at)"
            " VALUES (?, ?, ?, 'reminder', ?, ?, '2026-09-01', '2026-09-01')",
            (text, memory.normalize(text), f"{day.isoformat()}T10:00:00", status, tags),
        )
        self.conn.commit()

    def _exam(self, course, day, tags=",university,") -> None:
        self.conn.execute(
            "INSERT INTO exams (due_date, course, norm, tags, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, '2026-09-01', '2026-09-01')",
            (day.isoformat(), course, memory.normalize(course), tags),
        )
        self.conn.commit()

    def _class(
        self, course, weekday, start, end=None, room=None, professor=None,
        tags=",university,",
    ) -> None:
        self.conn.execute(
            "INSERT INTO class_schedule"
            " (course, weekday, start_time, end_time, room, professor, norm,"
            "  tags, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-09-01', '2026-09-01')",
            (course, weekday, start, end, room, professor,
             memory.normalize(course), tags),
        )
        self.conn.commit()

    def test_it_pulls_from_every_dated_table_at_once(self) -> None:
        self._exam("Στατιστική", self.today)
        self._reminder("να πάρω ψωμί", self.today)

        items = memory.agenda(self.conn, self.today)

        self.assertEqual(
            items, [("exam", "Στατιστική"), ("reminder", "να πάρω ψωμί")]
        )

    def test_a_class_appears_on_its_weekday(self) -> None:
        # self.today (2026-09-24) is a Thursday -- weekday() == 3.
        self._class("Υδατική Χημεία", 3, "15:15", "17:00", "Β1", "Παπαδόπουλος")
        self.assertEqual(
            memory.agenda(self.conn, self.today),
            [("class", "Υδατική Χημεία 15:15-17:00 (Β1, Παπαδόπουλος)")],
        )

    def test_a_class_is_absent_on_a_different_weekday(self) -> None:
        # Wednesday (2) instead of Thursday (3): a weekly schedule has no
        # `due_date` to fall on a specific calendar day, only a weekday that
        # recurs -- so it must be matched on day.weekday(), not on `day`.
        self._class("Μαθηματικά Ι", 2, "08:15")
        self.assertEqual(memory.agenda(self.conn, self.today), [])

    def test_classes_are_listed_first_and_time_ordered(self) -> None:
        self._exam("Στατιστική", self.today)
        self._class("Φυσική Ατμόσφαιρας", 3, "17:15", "20:00")
        self._class("Μαθηματικά Ι", 3, "08:15", "09:00")

        self.assertEqual(
            memory.agenda(self.conn, self.today),
            [
                ("class", "Μαθηματικά Ι 08:15-09:00"),
                ("class", "Φυσική Ατμόσφαιρας 17:15-20:00"),
                ("exam", "Στατιστική"),
            ],
        )

    def test_a_class_with_no_room_or_professor_omits_the_parenthesis(self) -> None:
        self._class("Προγραμματισμός Η/Υ", 3, "10:00", "12:00")
        self.assertEqual(
            memory.agenda(self.conn, self.today),
            [("class", "Προγραμματισμός Η/Υ 10:00-12:00")],
        )

    def test_a_class_tag_narrows_like_any_other_table(self) -> None:
        self._class("Μαθηματικά Ι", 3, "08:15")
        self.assertEqual(
            memory.agenda(self.conn, self.today, "university"),
            [("class", "Μαθηματικά Ι 08:15")],
        )
        self.assertEqual(memory.agenda(self.conn, self.today, "cafe"), [])

    def test_the_brains_memory_block_labels_a_class(self) -> None:
        self._class("Μαθηματικά Ι", 3, "08:15", "09:00")
        self.assertEqual(
            memory._due_today(self.conn, self.now),
            ["Σήμερα μάθημα: Μαθηματικά Ι 08:15-09:00"],
        )

    def test_another_day_is_not_todays_agenda(self) -> None:
        self._reminder("αυριανό", self.today + timedelta(days=1))
        self.assertEqual(memory.agenda(self.conn, self.today), [])
        self.assertEqual(len(memory.agenda(self.conn, self.today + timedelta(days=1))), 1)

    def test_a_fired_reminder_still_belongs_to_the_day(self) -> None:
        """The question is what the day holds, not what is still queued.
        Filtering on `pending` would make a 9am reminder invisible by 10am,
        now that the scheduler claims rows."""
        self._reminder("να πάρω ψωμί", self.today, status="fired")

        self.assertEqual(
            memory.agenda(self.conn, self.today), [("reminder", "να πάρω ψωμί (έγινε)")]
        )

    def test_a_missed_reminder_is_marked_as_missed(self) -> None:
        self._reminder("να πάρω ψωμί", self.today, status="missed")
        self.assertEqual(
            memory.agenda(self.conn, self.today),
            [("reminder", "να πάρω ψωμί (χάθηκε)")],
        )

    def test_a_tag_narrows_the_agenda_to_one_area(self) -> None:
        self._exam("Στατιστική", self.today)
        self._reminder("παραγγελία καφέ", self.today, tags=",cafe,")

        self.assertEqual(
            memory.agenda(self.conn, self.today, "cafe"),
            [("reminder", "παραγγελία καφέ")],
        )
        self.assertEqual(
            memory.agenda(self.conn, self.today, "university"),
            [("exam", "Στατιστική")],
        )

    def test_an_untagged_row_is_invisible_to_a_tagged_query(self) -> None:
        self._reminder("να πάρω ψωμί", self.today, tags=None)
        self.assertEqual(memory.agenda(self.conn, self.today, "cafe"), [])
        self.assertEqual(len(memory.agenda(self.conn, self.today)), 1)

    def test_the_brains_memory_block_still_reads_the_same(self) -> None:
        """_due_today() is now a rendering of agenda(); recall() must be
        unchanged apart from the status marking."""
        self._exam("Στατιστική", self.today)
        self._reminder("να πάρω ψωμί", self.today)

        self.assertEqual(
            memory._due_today(self.conn, self.now),
            ["Σήμερα εξέταση: Στατιστική", "Σήμερα: να πάρω ψωμί"],
        )


class AgendaSkillTests(MemoryDbTestCase):
    def _reminder(self, text, offset_days, tags=None) -> None:
        day = (datetime.now() + timedelta(days=offset_days)).date()
        self.conn.execute(
            "INSERT INTO reminders"
            " (text, norm, due_at, kind, status, tags, created_at, updated_at)"
            " VALUES (?, ?, ?, 'reminder', 'pending', ?, '2026-09-01', '2026-09-01')",
            (text, memory.normalize(text), f"{day.isoformat()}T10:00:00", tags),
        )
        self.conn.commit()

    def test_an_empty_day_says_so(self) -> None:
        self.assertEqual(skills.handle("Τι έχω σήμερα;"), "Δεν έχεις τίποτα σήμερα.")

    def test_todays_items_are_spoken(self) -> None:
        self._reminder("να πάρω ψωμί", 0)
        self.assertEqual(skills.handle("Τι έχω σήμερα;"), "Σήμερα έχεις: να πάρω ψωμί.")

    def test_tomorrow_is_a_different_day(self) -> None:
        self._reminder("αυριανό", 1)

        self.assertEqual(skills.handle("Τι έχω σήμερα;"), "Δεν έχεις τίποτα σήμερα.")
        self.assertEqual(skills.handle("Τι έχω αύριο;"), "Αύριο έχεις: αυριανό.")

    def test_a_class_is_spoken_with_a_class_prefix(self) -> None:
        today_weekday = datetime.now().weekday()
        self.conn.execute(
            "INSERT INTO class_schedule"
            " (course, weekday, start_time, norm, tags, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ',university,', '2026-09-01', '2026-09-01')",
            ("Μαθηματικά Ι", today_weekday, "08:15", memory.normalize("Μαθηματικά Ι")),
        )
        self.conn.commit()

        self.assertEqual(
            skills.handle("Τι έχω σήμερα;"),
            "Σήμερα έχεις: μάθημα Μαθηματικά Ι 08:15.",
        )

    def test_an_area_narrows_it(self) -> None:
        self._reminder("παραγγελία", 0, tags=",cafe,")
        self._reminder("να πάρω ψωμί", 0)

        self.assertEqual(
            skills.handle("Τι έχω σήμερα για τον καφέ;"), "Σήμερα έχεις: παραγγελία."
        )

    def test_it_runs_before_the_generic_recall(self) -> None:
        """«τι έχω σήμερα» is the narrower question; the generic recall would
        answer it with a keyword search that knows nothing about dates."""
        names = [skill.name for skill in skills.SKILLS]
        self.assertLess(names.index("agenda"), names.index("memory_recall"))

    def test_an_ordinary_question_is_not_an_agenda(self) -> None:
        self.assertIsNone(skills.handle("Πες μου ένα ανέκδοτο"))


if __name__ == "__main__":
    unittest.main()
