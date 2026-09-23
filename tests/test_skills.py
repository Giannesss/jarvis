"""Tests for jarvis/skills.py: phrase matching, timer parsing, shutdown and
conversation-end detection. See CLAUDE.md "Skills" for the matching rules
these exercise (accent/case-insensitive substring matching).

webbrowser.open, subprocess.Popen and scheduler.schedule are mocked in every
test that could reach them, so nothing ever opens on screen or writes a row
that would later be announced out loud.

A timer used to be a threading.Timer armed in-process, which these tests
patched directly; it is a `reminders` row now, so what they assert on is the
due_at the skill scheduled. See jarvis/scheduler.py.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from jarvis import db, policy, scheduler, skills


class SkillsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # skills.handle() writes an audit row for every decision now, so this
        # suite needs a database even where no skill touches memory. Patched
        # to a throwaway one, as in test_db.py and test_memory_skills.py, so
        # running the tests can never write to the real data/jarvis.db.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        patcher = mock.patch.object(db, "DB_PATH", Path(self._tmp.name) / "jarvis.db")
        patcher.start()
        self.addCleanup(patcher.stop)

        # The frozen flag is module state that would otherwise leak in from
        # whichever test ran last; cleared directly rather than via thaw(),
        # which is a real decision and would write a real audit row.
        policy._frozen = False
        skills.shutdown_requested = False

    def tearDown(self) -> None:
        skills.shutdown_requested = False


class NormalizationMatchingTests(SkillsTestCase):
    def test_time_skill_matches_with_accents(self) -> None:
        self.assertEqual(
            skills.handle("Τι ώρα είναι;")[: len("Η ώρα είναι")],
            "Η ώρα είναι",
        )

    def test_time_skill_matches_without_accents(self) -> None:
        # Same phrase, accents stripped and case-folded, as speech-to-text
        # output sometimes looks.
        self.assertEqual(
            skills.handle("τι ωρα ειναι")[: len("Η ώρα είναι")],
            "Η ώρα είναι",
        )

    def test_date_skill_case_insensitive(self) -> None:
        self.assertIsNotNone(skills.handle("ΤΙ ΗΜΕΡΟΜΗΝΙΑ ΕΧΟΥΜΕ"))

    def test_final_sigma_folds_to_plain_sigma(self) -> None:
        # CONVERSATION_END_PHRASES are spelled with a plain "σ"; _normalize()
        # must fold a trailing "ς" to "σ" for "Τέλος Τζάρβις" to match.
        self.assertTrue(skills.is_conversation_end("Τέλος Τζάρβις"))

    def test_unrelated_text_matches_nothing(self) -> None:
        self.assertIsNone(skills.handle("Πες μου ένα ανέκδοτο"))


class TimerParsingTests(SkillsTestCase):
    def _patched_timer(self):
        return mock.patch.object(skills.scheduler, "schedule", autospec=True)

    @staticmethod
    def _scheduled_seconds(mock_schedule) -> int:
        """How far ahead the skill scheduled the announcement.

        The duration is no longer an argument anywhere -- it lives in the
        due_at the row carries -- so the parsing tests read it back out of
        that. Rounded because a few milliseconds pass between the skill
        calling datetime.now() and this call.
        """
        due_at = mock_schedule.call_args[0][1]
        return round((due_at - datetime.now()).total_seconds())

    def test_number_word_before_unit(self) -> None:
        with self._patched_timer() as mock_timer:
            reply = skills.handle("Βάλε ένα χρονόμετρο για δύο λεπτά")
        self.assertEqual(reply, "Ξεκίνησε το χρονόμετρο για 2 λεπτά.")
        mock_timer.assert_called_once()
        self.assertEqual(self._scheduled_seconds(mock_timer), 120)

    def test_the_timer_is_scheduled_as_a_durable_row(self) -> None:
        """The point of the change: a timer outlives the process that set it,
        so the countdown is a row rather than a threading.Timer."""
        with self._patched_timer() as mock_timer:
            skills.handle("Βάλε ένα χρονόμετρο για δύο λεπτά")

        text, _due_at = mock_timer.call_args[0]
        self.assertEqual(text, "Το χρονόμετρο των 2 λεπτά τελείωσε!")
        self.assertEqual(mock_timer.call_args[1]["kind"], scheduler.TIMER)

    def test_closest_number_wins_over_farther_one(self) -> None:
        # "ένα" belongs to "χρονόμετρο", not the duration; "δύο" (closer to
        # the unit token) must be the one picked. See
        # skills._extract_number's docstring for this exact example.
        with self._patched_timer():
            reply = skills.handle("Βάλε ένα χρονόμετρο για δύο λεπτά")
        self.assertIn("2 λεπτά", reply)

    def test_digit_number_form(self) -> None:
        with self._patched_timer() as mock_timer:
            reply = skills.handle("χρονόμετρο για 5 λεπτά")
        self.assertEqual(reply, "Ξεκίνησε το χρονόμετρο για 5 λεπτά.")
        self.assertEqual(self._scheduled_seconds(mock_timer), 300)

    def test_seconds_stem_checked_before_minutes_stem(self) -> None:
        # "δευτερόλεπτα" contains "λεπτ" as a substring; _TIMER_UNITS must
        # check "δευτερολεπτ" first or this would be misread as minutes.
        with self._patched_timer() as mock_timer:
            reply = skills.handle("χρονόμετρο για δέκα δευτερόλεπτα")
        self.assertEqual(reply, "Ξεκίνησε το χρονόμετρο για 10 δευτερόλεπτα.")
        self.assertEqual(self._scheduled_seconds(mock_timer), 10)

    def test_no_unit_asks_for_clarification(self) -> None:
        with self._patched_timer() as mock_timer:
            reply = skills.handle("Βάλε ένα χρονόμετρο")
        self.assertEqual(
            reply, "Δεν κατάλαβα για πόση ώρα να βάλω το χρονόμετρο."
        )
        mock_timer.assert_not_called()

    def test_unit_with_no_number_asks_for_clarification(self) -> None:
        with self._patched_timer() as mock_timer:
            reply = skills.handle("χρονόμετρο για λεπτά")
        self.assertEqual(
            reply, "Δεν κατάλαβα για πόση ώρα να βάλω το χρονόμετρο."
        )
        mock_timer.assert_not_called()

    def test_no_timer_keyword_is_not_a_timer_skill(self) -> None:
        with self._patched_timer() as mock_timer:
            reply = skills._handle_timer(skills._normalize("δύο λεπτά"))
        self.assertIsNone(reply)
        mock_timer.assert_not_called()


class ShutdownPhraseTests(SkillsTestCase):
    def test_shutdown_phrase_sets_flag_and_replies(self) -> None:
        reply = skills.handle("Κλείσε")
        self.assertEqual(reply, "Αντίο!")
        self.assertTrue(skills.shutdown_requested)

    def test_unrelated_phrase_does_not_shut_down(self) -> None:
        skills.handle("Τι κάνεις")
        self.assertFalse(skills.shutdown_requested)


class ConversationEndTests(SkillsTestCase):
    def test_conversation_end_phrase_matches_anywhere(self) -> None:
        self.assertTrue(skills.is_conversation_end("εντάξει, τέλος Τζάρβις"))

    def test_alternate_end_phrase(self) -> None:
        self.assertTrue(skills.is_conversation_end("Αντίο Τζάρβις"))

    def test_bare_telos_matches_whole_utterance(self) -> None:
        self.assertTrue(skills.is_conversation_end("Τέλος"))

    def test_bare_telos_with_punctuation_still_matches(self) -> None:
        self.assertTrue(skills.is_conversation_end("Τέλος."))

    def test_telos_inside_a_sentence_does_not_end_conversation(self) -> None:
        # "στο τέλος της μέρας" contains "τέλος" but is not the bare
        # end-of-conversation phrase -- CONVERSATION_END_EXACT only matches
        # the whole utterance, precisely to avoid this false positive.
        self.assertFalse(skills.is_conversation_end("Στο τέλος της μέρας"))

    def test_unrelated_text_does_not_end_conversation(self) -> None:
        self.assertFalse(skills.is_conversation_end("Πες μου την ώρα"))


class OpenSkillTests(SkillsTestCase):
    def test_opens_matching_site(self) -> None:
        with mock.patch.object(skills, "webbrowser") as mock_webbrowser, \
                mock.patch.object(skills, "subprocess") as mock_subprocess:
            reply = skills.handle("Άνοιξε το YouTube")
        self.assertEqual(reply, "Άνοιξα το YouTube.")
        mock_webbrowser.open.assert_called_once_with("https://www.youtube.com")
        mock_subprocess.Popen.assert_not_called()

    def test_opens_matching_app(self) -> None:
        with mock.patch.object(skills, "webbrowser") as mock_webbrowser, \
                mock.patch.object(skills, "subprocess") as mock_subprocess:
            reply = skills.handle("Άνοιξε τον υπολογιστή")
        self.assertEqual(reply, "Άνοιξα το υπολογιστή.")
        mock_subprocess.Popen.assert_called_once_with(["calc.exe"])
        mock_webbrowser.open.assert_not_called()

    def test_open_verb_with_no_known_target_opens_nothing(self) -> None:
        with mock.patch.object(skills, "webbrowser") as mock_webbrowser, \
                mock.patch.object(skills, "subprocess") as mock_subprocess:
            reply = skills.handle("Άνοιξε το Spotify")
        self.assertIsNone(reply)
        mock_webbrowser.open.assert_not_called()
        mock_subprocess.Popen.assert_not_called()

    def test_no_open_verb_opens_nothing(self) -> None:
        with mock.patch.object(skills, "webbrowser") as mock_webbrowser, \
                mock.patch.object(skills, "subprocess") as mock_subprocess:
            skills.handle("Καλημέρα")
        mock_webbrowser.open.assert_not_called()
        mock_subprocess.Popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
