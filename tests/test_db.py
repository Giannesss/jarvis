"""Tests for jarvis/db.py: schema, concurrency settings, backup rotation.

Every test runs against a throwaway database in a temp directory -- db.DB_PATH
is patched, so nothing here can touch the real data/jarvis.db.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import db


class DbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "jarvis.db"
        self.backup_dir = self.root / "backups"

        patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)


class SchemaTests(DbTestCase):
    def test_connect_creates_every_table(self) -> None:
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)

        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertEqual(
            names,
            {"schema_version", "profile", "notes", "semesters", "courses",
             "exams", "businesses", "reminders", "audit", "policy_state"},
        )

    def test_connect_creates_parent_directory(self) -> None:
        nested = self.root / "deep" / "deeper" / "jarvis.db"
        conn = db.connect(nested)
        self.addCleanup(conn.close)
        self.assertTrue(nested.exists())

    def test_connect_is_idempotent(self) -> None:
        db.connect(self.db_path).close()
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        versions = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        self.assertEqual(versions, 1)

    def test_businesses_name_accepts_null(self) -> None:
        # The unnamed-business case: NULL must be storable, since that is how
        # "no name given" is recorded. See memory._parse_business.
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO businesses (name, note, norm, created_at, updated_at)"
            " VALUES (NULL, 'x', 'x', 't', 't')"
        )
        conn.commit()
        self.assertIsNone(conn.execute("SELECT name FROM businesses").fetchone()["name"])

    def test_audit_table_has_no_room_for_a_transcript(self) -> None:
        # The privacy guarantee is structural: there is nowhere to put one.
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit)")}
        self.assertEqual(columns, {"id", "ts", "action", "decision", "reason"})


class MigrationTests(DbTestCase):
    """Schema 1 -> 2: normalize() started folding the iotacism vowels, so
    every search key written before that has to be refolded or the rows stop
    matching queries that are folded."""

    def _as_schema_1(self) -> None:
        conn = db.connect(self.db_path)
        try:
            conn.execute("UPDATE schema_version SET version = 1")
            conn.execute(
                "INSERT INTO notes (id, text, norm, created_at, updated_at)"
                " VALUES (1, ?, ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
                ("Η εξέτασή μου", "η εξεταση μου"),  # normalize() as it was
            )
            conn.commit()
        finally:
            conn.close()

    def test_stored_search_keys_are_refolded(self) -> None:
        self._as_schema_1()

        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        row = conn.execute("SELECT text, norm FROM notes WHERE id = 1").fetchone()

        self.assertEqual(row["norm"], "ι εξετασι μου")
        # The readable text is untouched: only the search key is rewritten.
        self.assertEqual(row["text"], "Η εξέτασή μου")

    def test_migration_records_the_new_version_and_does_not_repeat(self) -> None:
        self._as_schema_1()

        db.connect(self.db_path).close()
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)

        self.assertEqual(
            conn.execute("SELECT version FROM schema_version").fetchone()["version"],
            db.SCHEMA_VERSION,
        )
        # Folding is idempotent, so a second pass would be harmless -- but it
        # must not run at all.
        self.assertEqual(
            conn.execute("SELECT norm FROM notes WHERE id = 1").fetchone()["norm"],
            "ι εξετασι μου",
        )


class ConcurrencyTests(DbTestCase):
    def test_wal_mode_and_busy_timeout(self) -> None:
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_a_second_connection_can_write_while_the_first_is_open(self) -> None:
        # This is what makes `python -m jarvis.mem` usable from a second
        # terminal while Jarvis holds the microphone.
        first = db.connect(self.db_path)
        self.addCleanup(first.close)
        second = db.connect(self.db_path)
        self.addCleanup(second.close)

        second.execute(
            "INSERT INTO notes (text, norm, created_at, updated_at)"
            " VALUES ('γεια', 'γεια', 't', 't')"
        )
        second.commit()

        self.assertEqual(first.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 1)


class BackupTests(DbTestCase):
    def _seed(self) -> None:
        conn = db.connect(self.db_path)
        conn.execute(
            "INSERT INTO notes (text, norm, created_at, updated_at)"
            " VALUES ('γεια', 'γεια', 't', 't')"
        )
        conn.commit()
        conn.close()

    def test_backup_copies_the_data(self) -> None:
        self._seed()
        path = db.backup(self.db_path, self.backup_dir)

        self.assertIsNotNone(path)
        copy = sqlite3.connect(path)
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("SELECT text FROM notes").fetchone()[0], "γεια")

    def test_backup_of_a_missing_database_returns_none(self) -> None:
        self.assertIsNone(db.backup(self.root / "absent.db", self.backup_dir))

    def test_rotation_keeps_only_the_newest_five(self) -> None:
        self._seed()
        made = [db.backup(self.db_path, self.backup_dir, keep=5) for _ in range(8)]

        kept = sorted(self.backup_dir.glob("jarvis-*.db"))
        self.assertEqual(len(kept), 5)
        # The five that survive are the five most recent.
        self.assertEqual(kept, sorted(made[-5:]))

    def test_backups_in_the_same_second_do_not_overwrite(self) -> None:
        # Timestamps are second-precision, so a startup backup plus an
        # immediate ":mem backup" would collide without the counter suffix.
        self._seed()
        first = db.backup(self.db_path, self.backup_dir, keep=10)
        second = db.backup(self.db_path, self.backup_dir, keep=10)
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())


if __name__ == "__main__":
    unittest.main()
