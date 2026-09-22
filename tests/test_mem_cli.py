"""Tests for jarvis/mem.py -- the `:mem ...` prompt command and the
standalone `python -m jarvis.mem`, which share one dispatcher.

run() takes its connection, output sink and confirmation reader as
arguments, so nothing here touches the real database, stdout, or a prompt.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import db, mem, memory


class MemCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        patcher = mock.patch.object(db, "DB_PATH", self.root / "jarvis.db")
        patcher.start()
        self.addCleanup(patcher.stop)

        self.conn = db.connect(self.root / "jarvis.db")
        self.addCleanup(self.conn.close)
        self.lines: list[str] = []

    def run_cmd(self, *argv: str, answer: str = "") -> int:
        return mem.run(
            list(argv),
            conn=self.conn,
            out=self.lines.append,
            confirm=lambda _prompt: answer,
        )

    @property
    def output(self) -> str:
        return "\n".join(self.lines)

    def seed_note(self, text: str = "μια σημείωση") -> int:
        parsed = memory.Parsed("notes", {"text": text, "norm": memory.normalize(text)})
        return memory.save(parsed, self.conn)


class ListShowTests(MemCliTestCase):
    def test_list_one_table(self) -> None:
        self.seed_note("το συνέδριο ήταν βαρετό")
        self.assertEqual(self.run_cmd("list", "notes"), 0)
        self.assertIn("συνέδριο", self.output)

    def test_list_without_a_table_covers_everything(self) -> None:
        self.assertEqual(self.run_cmd("list"), 0)
        for table in ("notes", "profile", "exams", "businesses", "audit"):
            self.assertIn(f"--- {table}", self.output)

    def test_show_renders_every_column(self) -> None:
        row_id = self.seed_note()
        self.assertEqual(self.run_cmd("show", "notes", str(row_id)), 0)
        self.assertIn("created_at", self.output)

    def test_show_missing_row_reports_and_fails(self) -> None:
        self.assertEqual(self.run_cmd("show", "notes", "999"), 1)
        self.assertIn("Δεν βρέθηκε", self.output)

    def test_unknown_table_is_rejected(self) -> None:
        # argparse choices keep `:mem del schema_version 1` from being a thing.
        self.assertNotEqual(self.run_cmd("list", "schema_version"), 0)


class EditTests(MemCliTestCase):
    def test_edit_updates_value_and_search_column(self) -> None:
        row_id = self.seed_note("παλιό κείμενο")
        self.assertEqual(
            self.run_cmd("edit", "notes", str(row_id), "text", "καινούριο κείμενο"), 0
        )

        row = self.conn.execute("SELECT * FROM notes WHERE id = ?", (row_id,)).fetchone()
        self.assertEqual(row["text"], "καινούριο κείμενο")
        # norm must follow, or the row stays findable only under its old wording.
        self.assertIn("καινουριο", row["norm"])

    def test_protected_columns_cannot_be_edited(self) -> None:
        row_id = self.seed_note()
        for column in ("id", "created_at", "norm"):
            with self.subTest(column=column):
                self.assertEqual(
                    self.run_cmd("edit", "notes", str(row_id), column, "x"), 1
                )

    def test_edit_is_not_a_way_around_the_sensitivity_check(self) -> None:
        row_id = self.seed_note()
        self.assertEqual(
            self.run_cmd("edit", "notes", str(row_id), "text", "ο κωδικός μου abc123"),
            1,
        )
        self.assertIn("Δεν αποθηκεύω", self.output)
        row = self.conn.execute("SELECT text FROM notes WHERE id = ?", (row_id,)).fetchone()
        self.assertNotIn("κωδικός", row["text"])


class DeleteTests(MemCliTestCase):
    def test_delete_reads_the_row_back_before_asking(self) -> None:
        row_id = self.seed_note("το συνέδριο ήταν βαρετό")
        self.run_cmd("del", "notes", str(row_id), answer="ναι")
        # The read-back must name what is about to go.
        self.assertIn("συνέδριο", self.output)

    def test_delete_proceeds_on_yes(self) -> None:
        row_id = self.seed_note()
        self.assertEqual(self.run_cmd("del", "notes", str(row_id), answer="ναι"), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 0)

    def test_delete_refuses_on_no(self) -> None:
        row_id = self.seed_note()
        self.assertEqual(self.run_cmd("del", "notes", str(row_id), answer="όχι"), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 1)

    def test_delete_refuses_on_an_unrecognised_answer(self) -> None:
        # Anything that is not a yes is a no: deleting is not the safe default.
        row_id = self.seed_note()
        self.assertEqual(self.run_cmd("del", "notes", str(row_id), answer="ίσως"), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 1)

    def test_delete_refuses_on_empty_input(self) -> None:
        row_id = self.seed_note()
        self.assertEqual(self.run_cmd("del", "notes", str(row_id), answer=""), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 1)

    def test_yes_flag_skips_the_prompt(self) -> None:
        row_id = self.seed_note()

        def explode(_prompt):  # must never be reached
            raise AssertionError("--yes should not prompt")

        self.assertEqual(
            mem.run(["del", "notes", str(row_id), "--yes"],
                    conn=self.conn, out=self.lines.append, confirm=explode),
            0,
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 0)


class ExportBackupTests(MemCliTestCase):
    def test_export_to_stdout_is_valid_json(self) -> None:
        self.seed_note("το συνέδριο")
        self.assertEqual(self.run_cmd("export"), 0)

        payload = json.loads(self.output)
        self.assertEqual(payload["notes"][0]["text"], "το συνέδριο")

    def test_export_to_a_file(self) -> None:
        self.seed_note()
        target = self.root / "dump.json"
        self.assertEqual(self.run_cmd("export", "--out", str(target)), 0)
        self.assertIn("notes", json.loads(target.read_text(encoding="utf-8")))

    def test_backup_writes_a_copy(self) -> None:
        self.seed_note()
        with mock.patch.object(db, "BACKUP_DIR", self.root / "backups"):
            self.assertEqual(self.run_cmd("backup"), 0)
        self.assertTrue(list((self.root / "backups").glob("jarvis-*.db")))


if __name__ == "__main__":
    unittest.main()
