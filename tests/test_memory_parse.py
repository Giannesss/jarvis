"""Tests for jarvis/memory.py's rule-based parser.

parse() is a pure function -- no database, no clock of its own, no mocks
needed anywhere in this file. Every test pins a decision from the ladder in
parse()'s docstring: which table a spoken sentence lands in, and why.

The fixed "now" is a Tuesday, so weekday and roll-forward cases are stable.

Captured values are verbatim: patterns match against the normalized text,
but Norm.group() reads each capture back out of the original, so "Γιάννης"
is stored as "Γιάννης" and not as the folded "γιαννισ" the pattern saw. The
`norm` field beside it is still folded -- that is the search key, and these
tests pin the two staying different things.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime

from jarvis import memory
from jarvis.text import normalize

NOW = datetime(2026, 9, 22, 14, 30, 0)  # Tuesday 22 September 2026


class SensitiveTests(unittest.TestCase):
    def test_password_is_refused(self) -> None:
        parsed = memory.parse("Θυμήσου ότι ο κωδικός μου είναι abc12345", NOW)
        self.assertEqual(parsed.table, memory.REJECTED)

    def test_card_number_is_refused(self) -> None:
        parsed = memory.parse("Θυμήσου ότι η κάρτα μου είναι 4532 1234 5678 9012", NOW)
        self.assertEqual(parsed.table, memory.REJECTED)

    def test_api_key_shape_is_refused(self) -> None:
        parsed = memory.parse("Θυμήσου ότι το κλειδί είναι sk-abcdef1234567890xyz", NOW)
        self.assertEqual(parsed.table, memory.REJECTED)

    def test_long_id_number_is_refused(self) -> None:
        parsed = memory.parse("Θυμήσου ότι η ταυτότητά μου είναι 12345678", NOW)
        self.assertEqual(parsed.table, memory.REJECTED)

    def test_innocent_card_mention_is_not_refused(self) -> None:
        # "κάρτα" alone must not block an ordinary note -- only alongside a
        # run of digits.
        parsed = memory.parse("Θυμήσου ότι η κάρτα βιβλιοθήκης λήγει τον Μάιο", NOW)
        self.assertNotEqual(parsed.table, memory.REJECTED)


class NotASaveTests(unittest.TestCase):
    def test_plain_question_is_not_a_save(self) -> None:
        # No trigger, no reminder verb: falls through to the brain untouched.
        self.assertIsNone(memory.parse("Τι ώρα είναι;", NOW))

    def test_trigger_with_no_body_is_not_a_save(self) -> None:
        self.assertIsNone(memory.parse("Θυμήσου ότι", NOW))


class IotacismTests(unittest.TestCase):
    """Whisper picks whichever spelling of the /i/ sound its language model
    prefers, and every one of them has to reach the same rung of the ladder.

    η, ι, υ, ει, οι and υι are pronounced identically in Modern Greek. This
    is not hypothetical: a spoken "Θυμήσου ότι με λένε Γιάννη" came back from
    Whisper as "Θυμίσου ...", missed RE_TRIGGER, fell through to the brain
    and was never saved -- the next recall answered "Δεν θυμάμαι κάτι
    σχετικό". The misspellings below stand in for that transcription.
    """

    def test_every_spelling_of_the_trigger_still_saves(self) -> None:
        for spelling in ("Θυμήσου", "Θυμίσου", "Θυμύσου", "Θυμείσου", "Θυμοίσου"):
            with self.subTest(spelling=spelling):
                parsed = memory.parse(f"{spelling} ότι με λένε Γιάννη", NOW)
                self.assertIsNotNone(parsed, "fell through to the brain")
                self.assertEqual(parsed.table, "profile")
                self.assertEqual(parsed.fields["key"], "ονομα")
                self.assertEqual(parsed.fields["value"], "Γιάννη")

    def test_every_spelling_of_the_other_triggers_still_saves(self) -> None:
        for phrase in (
            "Να θυμάσαι ότι ο Κώστας μετακόμισε",
            "Να θημάσαι ότι ο Κώστας μετακόμισε",
            "Σημείωσε ότι ο Κώστας μετακόμισε",
            "Σιμίωσε ότι ο Κώστας μετακόμισε",
            "Μην ξεχάσεις ότι ο Κώστας μετακόμισε",
            "Μιν ξεχάσις ότι ο Κώστας μετακόμισε",
        ):
            with self.subTest(phrase=phrase):
                parsed = memory.parse(phrase, NOW)
                self.assertIsNotNone(parsed, "fell through to the brain")
                self.assertEqual(parsed.table, "notes")

    def test_every_spelling_of_the_reminder_verb_still_saves(self) -> None:
        for verb in ("Υπενθύμισέ", "Υπενθίμησέ", "Υπενθήμησέ", "Θύμισέ"):
            with self.subTest(verb=verb):
                parsed = memory.parse(f"{verb} μου σε 2 ώρες ότι έχω ραντεβού", NOW)
                self.assertIsNotNone(parsed, "fell through to the brain")
                self.assertEqual(parsed.table, "reminders")
                self.assertEqual(parsed.fields["due_at"], "2026-09-22T16:30:00")

    def test_misspelled_body_still_reaches_the_right_table(self) -> None:
        # The ladder's own vocabulary is folded too, not just the trigger:
        # the exam word, the month and the profile verb all survive being
        # respelled.
        parsed = memory.parse(
            "Θυμίσου ότι έχω εξετάσις στις 12 Ιουνήου στα Μαθιματικά", NOW
        )
        self.assertEqual(parsed.table, "exams")
        self.assertEqual(parsed.fields["due_date"], "2027-06-12")
        # Whisper's own misspelling is kept: the fold exists to make the
        # pattern match, not to correct what was said. Only `norm` is folded.
        self.assertEqual(parsed.fields["course"], "Μαθιματικά")
        self.assertIn("μαθιματικα", parsed.fields["norm"])

        parsed = memory.parse("Θυμήσου ότι μου αρέσι ο καφές χωρίς ζάχαρη", NOW)
        self.assertEqual(parsed.table, "profile")
        self.assertEqual(parsed.fields["key"], "προτιμησεισ")

    def test_a_folded_row_is_findable_under_any_spelling(self) -> None:
        # The stored search key and the query are folded the same way, so a
        # note saved from one spelling is found by the others. (recall() runs
        # this against the database; here it is just the two keys meeting.)
        saved = memory.parse("Θυμήσου ότι η εξέτασή μου είναι δύσκολη", NOW)
        for question in ("η εξέτασι μου", "η εξέταση μου", "η εξέτασυ μου"):
            with self.subTest(question=question):
                self.assertIn(memory.stems_of(question)[0], saved.fields["norm"])

    def test_a_homophone_is_not_confused_with_a_different_sound(self) -> None:
        # ου is /u/ and the υ of αυ/ευ is a consonant, so folding them would
        # merge words that do not rhyme. "που" must not become "πι".
        self.assertNotEqual(normalize("που"), normalize("πι"))
        self.assertEqual(normalize("που"), "που")
        self.assertEqual(normalize("αυτό"), "αυτο")
        self.assertEqual(normalize("ευχαριστώ"), "ευχαριστω")


class WhisperPunctuationTests(unittest.TestCase):
    """The recognizer punctuates and splits; the trigger has to survive it.

    Companion to IotacismTests: that one covers Whisper picking the wrong
    *spelling* of a sound, this one covers Whisper inserting commas and word
    breaks that were never spoken. The strings below are transcriptions, not
    sentences anyone would type -- a spoken "Θυμήσου ότι με λένε Γιάννη" came
    back as "Θυμί σου, ό,τι με λένε Γιάννη" and missed RE_TRIGGER entirely,
    so the save fell through to the brain, which answered as though it had
    stored the name while the database stayed empty.
    """

    def test_the_transcription_that_failed_by_hand(self) -> None:
        parsed = memory.parse("Θυμί σου, ό,τι με λένε Γιάννη", NOW)
        self.assertIsNotNone(parsed, "fell through to the brain")
        self.assertEqual(parsed.table, "profile")
        self.assertEqual(parsed.fields["key"], "ονομα")
        self.assertEqual(parsed.fields["value"], "Γιάννη")

    def test_a_comma_after_the_trigger_verb(self) -> None:
        for phrase in (
            "Θυμίσου, ότι με λένε Γιάννη",
            "Θυμήσου, ό,τι με λένε Γιάννη",
            "Τζάρβις, θυμί σου, ό,τι με λένε Γιάννη.",
        ):
            with self.subTest(phrase=phrase):
                parsed = memory.parse(phrase, NOW)
                self.assertIsNotNone(parsed, "fell through to the brain")
                self.assertEqual(parsed.fields["value"], "Γιάννη")

    def test_a_comma_inside_the_other_triggers(self) -> None:
        for phrase in (
            "Να θυμάσαι, ότι ο Κώστας μετακόμισε",
            "Να, θυμάσαι ό,τι ο Κώστας μετακόμισε",
            "Σημείωσε, ό,τι ο Κώστας μετακόμισε",
            "Κράτα, ότι ο Κώστας μετακόμισε",
            "Μην, ξεχάσεις ό,τι ο Κώστας μετακόμισε",
        ):
            with self.subTest(phrase=phrase):
                parsed = memory.parse(phrase, NOW)
                self.assertIsNotNone(parsed, "fell through to the brain")
                self.assertEqual(parsed.table, "notes")

    def test_a_comma_in_the_reminder_verb(self) -> None:
        parsed = memory.parse("Υπενθύμισέ, μου σε 2 ώρες ότι έχω ραντεβού", NOW)
        self.assertIsNotNone(parsed, "fell through to the brain")
        self.assertEqual(parsed.table, "reminders")
        self.assertEqual(parsed.fields["due_at"], "2026-09-22T16:30:00")

    def test_a_punctuated_particle_is_still_not_a_body(self) -> None:
        # "ό,τι" normalizes to "ο,τι". A trigger with nothing after it
        # backtracks -- the optional particle gives way so the mandatory gap
        # can match -- leaving the particle as the body. It has to be
        # stripped with its comma, or "Θυμήσου ό,τι" is stored as a note
        # whose entire content is "ο,τι".
        for phrase in ("Θυμήσου ό,τι", "Θυμίσου, ό,τι", "Να θυμάσαι, ό,τι"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(memory.parse(phrase, NOW))

    def test_punctuation_tolerance_does_not_invent_a_trigger(self) -> None:
        # The gap is permissive about separators, not about the words
        # themselves: a sentence that merely contains them is still a
        # question for the brain.
        for phrase in ("Τι θυμάσαι;", "Θυμάσαι, τι ώρα είναι;", "Θυμήσου,"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(memory.parse(phrase, NOW))

    def test_a_comma_still_ends_a_course_name(self) -> None:
        # The fix stayed out of normalize() precisely so this keeps working:
        # the comma is what tells RE_COURSE_OF where the course stops.
        parsed = memory.parse(
            "Θυμήσου ότι έχω εξετάσεις στα Μαθηματικά, στις 12 Ιουνίου", NOW
        )
        self.assertEqual(parsed.table, "exams")
        self.assertEqual(parsed.fields["course"], "Μαθηματικά")


class ReminderTests(unittest.TestCase):
    def test_relative_hours(self) -> None:
        parsed = memory.parse("Υπενθύμισέ μου σε 2 ώρες ότι έχω ραντεβού", NOW)
        self.assertEqual(parsed.table, "reminders")
        self.assertEqual(parsed.fields["text"], "έχω ραντεβού")
        self.assertEqual(parsed.fields["due_at"], "2026-09-22T16:30:00")
        self.assertIsNone(parsed.fields["referent_date"])
        self.assertEqual(parsed.fields["status"], "pending")

    def test_relative_minutes_with_number_word(self) -> None:
        parsed = memory.parse("Υπενθύμισέ μου σε δέκα λεπτά να βγάλω το ψωμί", NOW)
        self.assertEqual(parsed.fields["due_at"], "2026-09-22T14:40:00")

    def test_seconds_stem_beats_minutes_stem(self) -> None:
        # "δευτερόλεπτα" contains "λεπτ"; UNITS must check the longer stem first.
        parsed = memory.parse("Υπενθύμισέ μου σε 30 δευτερόλεπτα ότι κάτι", NOW)
        self.assertEqual(parsed.fields["due_at"], "2026-09-22T14:30:30")

    def test_half_hour_fraction(self) -> None:
        parsed = memory.parse("Υπενθύμισέ μου σε μισή ώρα να φύγω", NOW)
        self.assertEqual(parsed.fields["due_at"], "2026-09-22T15:00:00")

    def test_absolute_afternoon_hour(self) -> None:
        parsed = memory.parse("Υπενθύμισέ μου στις 5 το απόγευμα να πάρω τη μαμά", NOW)
        self.assertEqual(parsed.fields["text"], "πάρω τη μαμά")
        self.assertEqual(parsed.fields["due_at"], "2026-09-22T17:00:00")

    def test_absolute_hour_already_past_rolls_to_tomorrow(self) -> None:
        parsed = memory.parse("Υπενθύμισέ μου στις 9 το πρωί να τηλεφωνήσω", NOW)
        self.assertEqual(parsed.fields["due_at"], "2026-09-23T09:00:00")

    def test_reminder_needs_no_memory_trigger(self) -> None:
        # The reminder verb is its own trigger; "θυμήσου" is not required.
        parsed = memory.parse("Υπενθύμισέ μου σε 1 ώρα ότι κάτι", NOW)
        self.assertEqual(parsed.table, "reminders")


class ExamTests(unittest.TestCase):
    def test_exam_with_month_name_and_course(self) -> None:
        parsed = memory.parse(
            "Θυμήσου ότι έχω εξετάσεις στις 12 Ιουνίου στα Μαθηματικά", NOW
        )
        self.assertEqual(parsed.table, "exams")
        # 12 June has already passed in September, so it rolls to next year.
        self.assertEqual(parsed.fields["due_date"], "2027-06-12")
        self.assertEqual(parsed.fields["course"], "Μαθηματικά")

    def test_exam_date_later_this_year_does_not_roll(self) -> None:
        parsed = memory.parse(
            "Θυμήσου ότι έχω διαγώνισμα στις 5 Δεκεμβρίου στη Φυσική", NOW
        )
        self.assertEqual(parsed.fields["due_date"], "2026-12-05")

    def test_exam_word_without_a_date_is_a_note(self) -> None:
        # Both an exam word and a parseable date are required.
        parsed = memory.parse("Θυμήσου ότι έχω εξετάσεις σύντομα", NOW)
        self.assertEqual(parsed.table, "notes")


class CourseTests(unittest.TestCase):
    def test_course_with_semester(self) -> None:
        parsed = memory.parse(
            "Θυμήσου ότι κάνω το μάθημα Βάσεις Δεδομένων αυτό το εξάμηνο", NOW
        )
        self.assertEqual(parsed.table, "courses")
        self.assertEqual(parsed.fields["name"], "Βάσεις Δεδομένων")
        self.assertEqual(parsed.fields["semester"], "αυτό το εξάμηνο")

    def test_gate_keeps_non_courses_out(self) -> None:
        # No "μάθημα", no "εξάμηνο" -- this must stay a note rather than
        # becoming a fake university course.
        parsed = memory.parse("Θυμήσου ότι κάνω γυμναστική κάθε Τρίτη", NOW)
        self.assertEqual(parsed.table, "notes")


class ProfileTests(unittest.TestCase):
    def test_name(self) -> None:
        parsed = memory.parse("Θυμήσου ότι με λένε Γιάννης", NOW)
        self.assertEqual(parsed.table, "profile")
        self.assertEqual(parsed.fields["key"], "ονομα")
        self.assertEqual(parsed.fields["value"], "Γιάννης")

    def test_job(self) -> None:
        parsed = memory.parse("Θυμήσου ότι δουλεύω ως προγραμματιστής", NOW)
        self.assertEqual(parsed.fields["key"], "δουλεια")
        self.assertEqual(parsed.fields["value"], "προγραμματιστής")

    def test_studies(self) -> None:
        parsed = memory.parse("Θυμήσου ότι σπουδάζω Πληροφορική", NOW)
        self.assertEqual(parsed.fields["key"], "σπουδεσ")
        self.assertEqual(parsed.fields["value"], "Πληροφορική")

    def test_preferences(self) -> None:
        parsed = memory.parse("Θυμήσου ότι μου αρέσει ο καφές χωρίς ζάχαρη", NOW)
        self.assertEqual(parsed.fields["key"], "προτιμησεισ")
        self.assertEqual(parsed.fields["value"], "ο καφές χωρίς ζάχαρη")

    def test_school_value_keeps_its_article_verbatim(self) -> None:
        # Values are stored exactly as captured, articles included, for
        # consistency across every profile pattern. No article stripping.
        parsed = memory.parse("Θυμήσου ότι η σχολή μου είναι το ΕΚΠΑ", NOW)
        self.assertEqual(parsed.fields["key"], "σχολη")
        self.assertEqual(parsed.fields["value"], "το ΕΚΠΑ")


class BusinessTests(unittest.TestCase):
    def test_unnamed_business_stores_name_as_none_not_empty_string(self) -> None:
        # RE_BIZ_NEED's name group is [^,.]*? -- zero-or-more -- so an
        # unnamed business matches with an empty capture. That empty capture
        # must become None: NULL says "no name given" unambiguously, where ""
        # would be indistinguishable from an empty name.
        parsed = memory.parse(
            "Θυμήσου ότι η επιχείρησή μου χρειάζεται καινούριο λογιστή", NOW
        )
        self.assertEqual(parsed.table, "businesses")
        self.assertIsNone(parsed.fields["name"])
        self.assertNotEqual(parsed.fields["name"], "")
        self.assertEqual(parsed.fields["note"], "καινούριο λογιστή")

    def test_named_business_keeps_the_captured_name(self) -> None:
        parsed = memory.parse(
            "Θυμήσου ότι η επιχείρησή μου Καφέ Αθηνά θέλει καινούριο μενού", NOW
        )
        self.assertEqual(parsed.table, "businesses")
        self.assertEqual(parsed.fields["name"], "Καφέ Αθηνά")
        self.assertEqual(parsed.fields["note"], "καινούριο μενού")

    def test_business_without_a_need_verb(self) -> None:
        parsed = memory.parse("Θυμήσου ότι το μαγαζί μου πάει καλά φέτος", NOW)
        self.assertEqual(parsed.table, "businesses")
        self.assertIsNone(parsed.fields["name"])
        self.assertEqual(parsed.fields["note"], "πάει καλά φέτος")


class FallbackTests(unittest.TestCase):
    def test_unmatched_becomes_a_note_verbatim(self) -> None:
        parsed = memory.parse("Θυμήσου ότι το συνέδριο ήταν βαρετό", NOW)
        self.assertEqual(parsed.table, "notes")
        # Notes keep the original spelling, accents and all.
        self.assertEqual(parsed.fields["text"], "Θυμήσου ότι το συνέδριο ήταν βαρετό")

    def test_alternate_triggers_all_reach_the_fallback(self) -> None:
        for phrase in (
            "Να θυμάσαι ότι ο Κώστας μετακόμισε",
            "Σημείωσε ότι ο Κώστας μετακόμισε",
            "Κράτα ότι ο Κώστας μετακόμισε",
            "Μην ξεχάσεις ότι ο Κώστας μετακόμισε",
        ):
            with self.subTest(phrase=phrase):
                parsed = memory.parse(phrase, NOW)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed.table, "notes")


class DateHelperTests(unittest.TestCase):
    """_parse_date() is only ever handed normalized text, so these go through
    normalize() rather than spelling out what it produces."""

    def _on(self, text: str) -> date | None:
        return memory._parse_date(normalize(text), NOW.date())

    def test_relative_day_words(self) -> None:
        self.assertEqual(self._on("αύριο"), date(2026, 9, 23))
        self.assertEqual(self._on("μεθαύριο"), date(2026, 9, 24))

    def test_weekday_resolves_to_next_occurrence(self) -> None:
        # NOW is a Tuesday; "Παρασκευή" is three days out.
        self.assertEqual(self._on("την Παρασκευή"), date(2026, 9, 25))

    def test_same_weekday_means_next_week(self) -> None:
        self.assertEqual(self._on("την Τρίτη"), date(2026, 9, 29))

    def test_numeric_date(self) -> None:
        self.assertEqual(self._on("3/11"), date(2026, 11, 3))

    def test_impossible_date_is_rejected(self) -> None:
        self.assertIsNone(self._on("32/13"))


class VerbatimCaptureTests(unittest.TestCase):
    """Every display column holds what was said; every `norm` stays folded.

    The gap this closes showed up in a live session: "Γιάννης" was saved and
    then spoken back as "Γιάννι". Captures were being sliced out of the
    normalized string, so the folded form was what reached the column meant
    to be readable. Norm.group() reads them out of the original instead.
    """

    def _fields(self, text: str) -> dict:
        parsed = memory.parse(text, NOW)
        self.assertIsNotNone(parsed, "fell through to the brain")
        return parsed.fields

    def test_accents_and_capitals_survive_every_table(self) -> None:
        for text, column, expected in (
            ("Θυμήσου ότι με λένε Γιάννης", "value", "Γιάννης"),
            ("Θυμήσου ότι η σχολή μου είναι το ΕΚΠΑ", "value", "το ΕΚΠΑ"),
            (
                "Θυμήσου ότι κάνω το μάθημα Βάσεις Δεδομένων αυτό το εξάμηνο",
                "name",
                "Βάσεις Δεδομένων",
            ),
            (
                "Θυμήσου ότι έχω εξετάσεις στις 12 Ιουνίου στα Μαθηματικά",
                "course",
                "Μαθηματικά",
            ),
            (
                "Θυμήσου ότι η επιχείρησή μου Καφέ Αθηνά θέλει νέο μενού",
                "name",
                "Καφέ Αθηνά",
            ),
            ("Υπενθύμισέ μου σε 2 ώρες ότι έχω ραντεβού", "text", "έχω ραντεβού"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self._fields(text)[column], expected)

    def test_the_folded_digraph_does_not_shift_the_capture(self) -> None:
        # ει/οι/υι are the only characters normalize() collapses, so a value
        # containing one is exactly where an off-by-one would show up: the
        # naive slice would start a character late for each one before it.
        for text, expected in (
            ("Θυμήσου ότι με λένε Ειρήνη", "Ειρήνη"),
            ("Θυμήσου ότι μένω στοι Ποικίλοι Οικισμοί", "Ποικίλοι Οικισμοί"),
            ("Θυμήσου ότι μου αρέσει το ουίσκι", "το ουίσκι"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self._fields(text)["value"], expected)

    def test_norm_is_still_folded_and_searchable(self) -> None:
        # The display column changed; the search key did not. A question
        # asked with a different spelling of the same sound still hits it.
        fields = self._fields("Θυμήσου ότι σπουδάζω Πληροφορική")
        self.assertEqual(fields["value"], "Πληροφορική")
        self.assertEqual(fields["norm"], "σπουδεσ πλιροφορικι")
        self.assertIn(memory.stems_of("πλιροφορικι")[0], fields["norm"])

    def test_whisper_noise_still_yields_a_clean_value(self) -> None:
        # The transcription that failed by hand, end to end: a split verb, a
        # comma inside it, and "ό,τι" for "ότι". The name still comes back
        # spelled the way Whisper spelled it, not the way the fold saw it.
        fields = self._fields("Θυμί σου, ό,τι με λένε Γιάννη")
        self.assertEqual(fields["value"], "Γιάννη")
        self.assertEqual(fields["norm"], "ονομα γιαννι")

    def test_notes_are_unchanged(self) -> None:
        # Notes always stored the raw utterance; nothing here should move.
        fields = self._fields("Θυμήσου ότι το συνέδριο ήταν βαρετό")
        self.assertEqual(fields["text"], "Θυμήσου ότι το συνέδριο ήταν βαρετό")


class NormViewTests(unittest.TestCase):
    """The span map itself, at the level parse() uses it."""

    def test_a_slice_still_points_into_the_original(self) -> None:
        view = memory.Norm.of("Θυμήσου ότι με λένε Γιάννης")
        body = view.slice(view.norm.index("με")).strip()
        self.assertEqual(body.norm, "με λενε γιαννισ")
        self.assertEqual(body.original(0, len(body.norm)), "με λένε Γιάννης")

    def test_a_group_that_did_not_match_is_empty_not_an_error(self) -> None:
        # RE_BIZ_NEED's name group is optional; an absent group's span is
        # (-1, -1), which has to read as "no value" rather than raising.
        view = memory.Norm.of("η επιχείρησή μου χρειάζεται λογιστή")
        m = memory.RE_BIZ_NEED.search(view.norm)
        self.assertEqual(view.group(m, "name"), "")

    def test_leading_whitespace_does_not_shift_the_map(self) -> None:
        view = memory.Norm.of("   με λένε Γιάννης")
        start = view.norm.index("γιαννισ")
        self.assertEqual(view.original(start, start + len("γιαννισ")), "Γιάννης")


if __name__ == "__main__":
    unittest.main()
