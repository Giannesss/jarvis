"""Tests for jarvis/memory.py's stemming, scoring and token budget.

The stemming table here is the one worked out during design: it exists to
catch a suffix list that silently fails to match real Greek inflections --
the first draft did exactly that for εξετάσεις/εξέταση.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from jarvis import db, memory
from jarvis.text import normalize

NOW = datetime(2026, 9, 22, 14, 30, 0)


class StemTests(unittest.TestCase):
    """stem() is only ever handed normalize()d tokens, so every word here
    goes through normalize() first -- it folds the iotacism vowels, and a
    hand-written "εξετασεισ" would not be what stem() actually sees."""

    def _stem(self, word: str) -> str:
        return memory.stem(normalize(word))

    def test_inflection_pairs_share_a_stem(self) -> None:
        # Each group must collapse to one stem, in every direction.
        groups = [
            ("εξετάσεις", "εξέταση", "εξετάσης"),
            ("μάθημα", "μαθήματα", "μαθήματος"),
            ("επιχείρηση", "επιχειρήσεις", "επιχείρησης"),
            ("μαθηματικά", "μαθηματικών", "μαθηματικής"),
            ("εξάμηνο", "εξαμήνου", "εξάμηνα"),
            ("πελάτης", "πελάτες", "πελάτη"),
        ]
        for group in groups:
            with self.subTest(group=group):
                stems = {self._stem(word) for word in group}
                self.assertEqual(len(stems), 1, f"{group} -> {stems}")

    def test_stem_is_a_prefix_of_every_form(self) -> None:
        # Matching is LIKE '%stem%' against stored text, so the stem has to be
        # a literal prefix of the inflected forms for recall to be symmetric.
        for word in ("εξετάσεις", "εξέταση", "εξετάσης"):
            with self.subTest(word=word):
                self.assertTrue(normalize(word).startswith(self._stem("εξέταση")))

    def test_min_stem_rejects_an_over_aggressive_suffix(self) -> None:
        # "ματα" would leave "μαθι" (4), which also matches μάθηση/μαθητής.
        # It must be skipped in favour of "ατα", leaving "μαθιμ".
        self.assertEqual(self._stem("μαθήματα"), normalize("μαθημ"))

    def test_short_words_are_left_alone(self) -> None:
        # Known gap: under MIN_STEM these stay literal. The iotacism fold
        # narrows it to one direction -- "πόλη" folds to "πολι", which is a
        # prefix of the stored "πολισ", so asking about πόλη now finds a note
        # about πόλεις; the reverse still misses. Documented in CLAUDE.md.
        self.assertEqual(self._stem("πόλη"), normalize("πόλη"))
        self.assertNotEqual(self._stem("πόλη"), self._stem("πόλεις"))
        self.assertTrue(normalize("πόλεις").startswith(self._stem("πόλη")))

    def test_stopwords_and_short_tokens_are_dropped(self) -> None:
        stems = memory.stems_of("τι θυμάσαι για τα μαθηματικά")
        self.assertNotIn(normalize("για"), stems)
        self.assertNotIn(normalize("θυμάσαι"), stems)
        self.assertIn(self._stem("μαθηματικά"), stems)

    def test_a_query_finds_a_row_saved_under_another_spelling(self) -> None:
        # The end-to-end point of the fold: what Whisper heard as "εξετάσις"
        # when saving and "εξέταση" when asking is one and the same word.
        self.assertTrue(
            normalize("εξετάσις").startswith(self._stem("εξέταση"))
        )


class RecallTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = Path(self._tmp.name) / "jarvis.db"

        patcher = mock.patch.object(db, "DB_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.conn = db.connect(path)
        self.addCleanup(self.conn.close)

    def _save(self, phrase: str, now: datetime = NOW) -> None:
        parsed = memory.parse(phrase, now)
        self.assertIsNotNone(parsed, f"{phrase!r} did not parse as a save")
        memory.save(parsed, self.conn)


class RecallContentTests(RecallTestCase):
    def test_profile_digest_is_always_present(self) -> None:
        self._save("Θυμήσου ότι με λένε Γιάννης")
        block = memory.recall("κάτι εντελώς άσχετο", self.conn, NOW)
        self.assertIn("Προφίλ", block)
        self.assertIn("γιαννισ", block)

    def test_exam_due_today_is_always_present(self) -> None:
        self.conn.execute(
            "INSERT INTO exams (due_date, course, topic, norm, created_at, updated_at)"
            " VALUES (?, 'φυσικη', NULL, 'φυσικη', ?, ?)",
            (NOW.date().isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
        self.conn.commit()

        block = memory.recall("άσχετη ερώτηση", self.conn, NOW)
        self.assertIn("Σήμερα εξέταση", block)

    def test_keyword_hit_is_found_through_inflection(self) -> None:
        self._save("Θυμήσου ότι έχω εξετάσεις στις 12 Ιουνίου στα Μαθηματικά")
        # Asked in the singular, stored in the plural.
        block = memory.recall("πες μου για την εξέταση", self.conn, NOW)
        self.assertIn("2027-06-12", block)

    def test_unnamed_business_is_findable_by_its_category_word(self) -> None:
        # The head phrase is consumed by the pattern, so the row would
        # otherwise hold only its note and be unfindable by "επιχείρηση".
        self._save("Θυμήσου ότι η επιχείρησή μου χρειάζεται καινούριο λογιστή")
        block = memory.recall("τι γίνεται με την επιχείρηση;", self.conn, NOW)
        self.assertIn("λογιστι", block)

    def test_empty_memory_yields_an_empty_block(self) -> None:
        self.assertEqual(memory.recall("οτιδήποτε", self.conn, NOW), "")

    def test_unrelated_query_returns_no_keyword_hits(self) -> None:
        self._save("Θυμήσου ότι το συνέδριο ήταν βαρετό")
        block = memory.recall("τι θυμάσαι για τη Νορβηγία", self.conn, NOW)
        self.assertNotIn("συνέδριο", block)


class ScoringTests(RecallTestCase):
    def test_more_matched_stems_outrank_fewer(self) -> None:
        self._save("Θυμήσου ότι το συνέδριο πληροφορικής ήταν βαρετό")
        self._save("Θυμήσου ότι το συνέδριο ακυρώθηκε")

        block = memory.recall("συνέδριο πληροφορικής", self.conn, NOW)
        lines = block.splitlines()
        self.assertIn("πληροφορικής", lines[0])

    def test_recency_breaks_a_tie(self) -> None:
        old = (NOW - timedelta(days=200)).isoformat(timespec="seconds")
        for text, created in (("παλιά σημείωση συνέδριο", old),
                              ("νέα σημείωση συνέδριο", NOW.isoformat(timespec="seconds"))):
            self.conn.execute(
                "INSERT INTO notes (text, norm, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (text, memory.normalize(text), created, created),
            )
        self.conn.commit()

        block = memory.recall("συνέδριο", self.conn, NOW)
        self.assertIn("νέα", block.splitlines()[0])


class BudgetTests(RecallTestCase):
    def test_block_stays_within_the_token_budget(self) -> None:
        for i in range(60):
            self._save(f"Θυμήσου ότι το συνέδριο νούμερο {i} ήταν αξιοσημείωτο και μεγάλο")

        block = memory.recall("συνέδριο", self.conn, NOW)
        self.assertLessEqual(
            memory.estimate_tokens(block), memory.MEMORY_TOKEN_BUDGET
        )

    def test_one_enormous_note_does_not_blow_the_budget(self) -> None:
        huge = "λεπτομέρεια " * 500
        self.conn.execute(
            "INSERT INTO notes (text, norm, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (huge, memory.normalize(f"συνεδριο {huge}"), NOW.isoformat(), NOW.isoformat()),
        )
        self.conn.commit()

        block = memory.recall("συνέδριο", self.conn, NOW)
        # Dropped whole rather than truncated mid-sentence.
        self.assertEqual(block, "")

    def test_profile_and_today_survive_a_crowded_budget(self) -> None:
        self._save("Θυμήσου ότι με λένε Γιάννης")
        for i in range(60):
            self._save(f"Θυμήσου ότι το συνέδριο νούμερο {i} ήταν αξιοσημείωτο και μεγάλο")

        block = memory.recall("συνέδριο", self.conn, NOW)
        self.assertTrue(block.startswith("Προφίλ"))


class SpokenProfileTests(RecallTestCase):
    """The topic-less "Τι θυμάσαι;" answer. Distinct from _profile_digest():
    this one is spoken, so it must never read a stored value aloud."""

    def test_every_profile_key_has_a_spoken_label(self) -> None:
        # A new PROFILE_PATTERNS entry without a label would be stored and
        # then silently omitted from the spoken answer.
        labelled = {key for key, _ in memory._PROFILE_LABELS}
        for _, key in memory.PROFILE_PATTERNS:
            with self.subTest(key=key):
                self.assertIn(key, labelled)

    def test_empty_profile_invites_a_first_save(self) -> None:
        reply = memory.spoken_profile(self.conn)
        self.assertIn("Δεν έχω κρατήσει", reply)

    def test_names_the_keys_it_holds(self) -> None:
        self._save("Θυμήσου ότι με λένε Γιάννης")
        self._save("Θυμήσου ότι μένω στην Αθήνα")

        reply = memory.spoken_profile(self.conn)
        self.assertIn("το όνομά σου", reply)
        self.assertIn("πού μένεις", reply)

    def test_never_speaks_a_stored_value(self) -> None:
        # The stored value is normalized ("γιαννισ"), so saying it aloud
        # would mispronounce the user's own name. See CLAUDE.md known gaps.
        self._save("Θυμήσου ότι με λένε Γιάννης")
        reply = memory.spoken_profile(self.conn)
        self.assertNotIn("γιαννισ", reply)
        self.assertNotIn("=", reply)

    def test_joins_three_labels_the_greek_way(self) -> None:
        self._save("Θυμήσου ότι με λένε Γιάννης")
        self._save("Θυμήσου ότι μένω στην Αθήνα")
        self._save("Θυμήσου ότι η σχολή μου είναι το ΕΚΠΑ")

        reply = memory.spoken_profile(self.conn)
        # No comma before the final "και".
        self.assertIn(", ", reply)
        self.assertNotIn(", και", reply)


class RecallSafeTests(RecallTestCase):
    def test_recall_safe_swallows_a_broken_database(self) -> None:
        # Memory must never take down a conversation turn.
        with mock.patch.object(db, "connect", side_effect=RuntimeError("boom")):
            self.assertEqual(memory.recall_safe("οτιδήποτε"), "")


if __name__ == "__main__":
    unittest.main()
