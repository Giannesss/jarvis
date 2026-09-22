"""Tests that memory is actually *wired in* -- that skills.handle() dispatches
to it and main._reply_to() injects it, rather than the pieces merely existing.

The rest of the memory suite tests parse(), recall() and the CLI in isolation.
This file exists because all of that can pass while nothing calls it: the
or-chain in handle() is the seam where the feature becomes real.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
from jarvis import brain, db, skills


class MemorySkillTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = Path(self._tmp.name) / "jarvis.db"

        patcher = mock.patch.object(db, "DB_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.db_path = path
        skills.shutdown_requested = False
        self.addCleanup(lambda: setattr(skills, "shutdown_requested", False))

    def rows(self, table: str) -> list:
        conn = db.connect(self.db_path)
        try:
            return conn.execute(f"SELECT * FROM {table}").fetchall()
        finally:
            conn.close()


class DispatchTests(MemorySkillTestCase):
    def test_handle_dispatches_a_save(self) -> None:
        reply = skills.handle("Θυμήσου ότι με λένε Γιάννης")

        self.assertEqual(reply, skills._SAVE_REPLIES["profile"])
        self.assertEqual(self.rows("profile")[0]["value"], "γιαννισ")

    def test_handle_dispatches_each_table(self) -> None:
        cases = [
            ("Θυμήσου ότι έχω εξετάσεις στις 12 Ιουνίου στα Μαθηματικά", "exams"),
            ("Θυμήσου ότι κάνω το μάθημα Βάσεις Δεδομένων αυτό το εξάμηνο", "courses"),
            ("Θυμήσου ότι η επιχείρησή μου χρειάζεται λογιστή", "businesses"),
            ("Θυμήσου ότι το συνέδριο ήταν βαρετό", "notes"),
            ("Υπενθύμισέ μου σε 2 ώρες ότι έχω ραντεβού", "reminders"),
        ]
        for phrase, table in cases:
            with self.subTest(table=table):
                reply = skills.handle(phrase)
                self.assertEqual(reply, skills._SAVE_REPLIES[table])
                self.assertEqual(len(self.rows(table)), 1)

    def test_handle_dispatches_a_recall(self) -> None:
        skills.handle("Θυμήσου ότι έχω εξετάσεις στις 12 Ιουνίου στα Μαθηματικά")
        reply = skills.handle("Τι θυμάσαι για τα μαθηματικά;")
        self.assertIn("2027-06-12", reply)

    def test_recall_with_nothing_stored(self) -> None:
        self.assertEqual(
            skills.handle("Τι θυμάσαι για τη Νορβηγία;"), "Δεν θυμάμαι κάτι σχετικό."
        )

    def test_topic_less_recall_names_what_is_held(self) -> None:
        # Used to answer "Δεν θυμάμαι κάτι σχετικό" -- reporting an empty
        # result for a keyword search that had no keyword to run.
        skills.handle("Θυμήσου ότι με λένε Γιάννης")
        reply = skills.handle("Τι θυμάσαι;")
        self.assertIn("Θυμάμαι", reply)
        self.assertIn("το όνομά σου", reply)

    def test_topic_less_recall_with_an_empty_profile(self) -> None:
        self.assertIn("Δεν έχω κρατήσει", skills.handle("Τι θυμάσαι;"))

    def test_widened_phrases_reach_the_recall_skill(self) -> None:
        # Each of these fell through to the brain before the list was widened.
        skills.handle("Θυμήσου ότι με λένε Γιάννης")
        for phrase in (
            "Θυμάσαι κάποιο όνομα;",
            "Θυμάσαι αν σου έχω πει κάτι;",
            "Τι σου είπα;",
            "Τι μου είπες;",
            "Σου είχα πει τίποτα;",
            "Τι έχεις;",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNotNone(
                    skills.handle(phrase), "fell through to the brain"
                )

    def test_recall_verbs_alone_do_not_become_search_terms(self) -> None:
        # "είπες" would otherwise stem to a junk needle and report a failed
        # search instead of taking the topic-less path.
        skills.handle("Θυμήσου ότι με λένε Γιάννης")
        self.assertIn("Θυμάμαι", skills.handle("Τι μου είπες;"))

    def test_save_still_wins_over_the_widened_recall_list(self) -> None:
        # "να θυμάσαι ότι…" is a save trigger and now also contains a recall
        # phrase; handle() runs the save first, and must keep doing so.
        reply = skills.handle("Να θυμάσαι ότι με λένε Γιάννης")
        self.assertEqual(reply, skills._SAVE_REPLIES["profile"])
        self.assertEqual(len(self.rows("profile")), 1)

    def test_reminder_is_not_captured_by_the_recall_list(self) -> None:
        reply = skills.handle("Υπενθύμισέ μου σε 2 ώρες ότι έχω ραντεβού")
        self.assertEqual(reply, skills._SAVE_REPLIES["reminders"])

    def test_sensitive_save_is_refused_and_stores_nothing(self) -> None:
        reply = skills.handle("Θυμήσου ότι ο κωδικός μου είναι abc12345")
        self.assertEqual(reply, "Δεν αποθηκεύω κωδικούς ή αριθμούς κάρτας.")
        self.assertEqual(self.rows("notes"), [])
        self.assertEqual(self.rows("profile"), [])


class NonInterferenceTests(MemorySkillTestCase):
    def test_shutdown_still_wins_over_memory(self) -> None:
        # _handle_shutdown stays first in the chain: "κλείσε" must never be
        # intercepted by anything added after it.
        self.assertEqual(skills.handle("Κλείσε"), "Αντίο!")
        self.assertTrue(skills.shutdown_requested)

    def test_existing_skills_are_unaffected(self) -> None:
        self.assertTrue(skills.handle("Τι ώρα είναι;").startswith("Η ώρα είναι"))
        self.assertIsNotNone(skills.handle("Τι μέρα είναι;"))

    def test_non_memory_text_falls_through_to_the_brain(self) -> None:
        # No trigger phrase: handle() must decline so main._reply_to() can
        # hand the utterance to the brain.
        self.assertIsNone(skills.handle("Πες μου ένα ανέκδοτο"))

    def test_timer_still_reaches_the_timer_skill(self) -> None:
        # "χρονόμετρο" sits after memory in the chain; memory must not eat it.
        with mock.patch.object(skills.threading, "Timer", autospec=True) as timer:
            reply = skills.handle("Βάλε ένα χρονόμετρο για δύο λεπτά")
        self.assertEqual(reply, "Ξεκίνησε το χρονόμετρο για 2 λεπτά.")
        timer.assert_called_once()


class InjectionTests(MemorySkillTestCase):
    def test_reply_to_passes_recalled_memory_to_the_brain(self) -> None:
        skills.handle("Θυμήσου ότι με λένε Γιάννης")

        with mock.patch.object(brain, "ask", return_value="ok") as ask:
            main._reply_to("Πες μου ένα ανέκδοτο")

        ask.assert_called_once()
        memory_block = ask.call_args[0][1]
        self.assertIn("γιαννισ", memory_block)

    def test_brain_ask_keeps_memory_out_of_history(self) -> None:
        # The block is rebuilt every turn, so it must never accumulate in
        # _history or the trim could drop half of one.
        before = list(brain._history)
        with mock.patch.dict(brain._PROVIDERS, {"ollama": lambda messages: "απάντηση"}):
            brain.ask("γεια", "Προφίλ: όνομα=γιαννησ")

        self.addCleanup(lambda: brain._history.__setitem__(slice(None), before))
        self.assertNotIn(
            "Προφίλ: όνομα=γιαννησ",
            [message["content"] for message in brain._history],
        )

    def test_brain_ask_without_memory_is_unchanged(self) -> None:
        before = list(brain._history)
        seen = {}

        def capture(messages):
            seen["messages"] = list(messages)
            return "απάντηση"

        with mock.patch.dict(brain._PROVIDERS, {"ollama": capture}):
            brain.ask("γεια")

        self.addCleanup(lambda: brain._history.__setitem__(slice(None), before))
        # No memory block means the message list is exactly the history.
        self.assertEqual(seen["messages"], brain._history[:-1])

    def test_memory_block_is_injected_after_the_frozen_prefix(self) -> None:
        before = list(brain._history)
        seen = {}

        def capture(messages):
            seen["messages"] = list(messages)
            return "απάντηση"

        with mock.patch.dict(brain._PROVIDERS, {"ollama": capture}):
            brain.ask("γεια", "Προφίλ: όνομα=γιαννησ")

        self.addCleanup(lambda: brain._history.__setitem__(slice(None), before))
        injected = seen["messages"][brain._PREFIX_LEN]
        self.assertEqual(injected["role"], "system")
        self.assertIn("γιαννησ", injected["content"])


if __name__ == "__main__":
    unittest.main()
