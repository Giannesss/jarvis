"""Tests for jarvis/text.py: the normalization every phrase list, pattern and
stored search key in Jarvis is compared through.

The iotacism fold is the interesting half. Modern Greek pronounces η, ι, υ,
ει, οι and υι identically, so Whisper's choice of spelling is arbitrary and
normalize() has to make them all compare equal -- without merging the three
digraphs that only look like they belong (ου, αυ, ευ).
"""

from __future__ import annotations

import re
import unittest

from jarvis import text


class IotacismTests(unittest.TestCase):
    def test_every_spelling_of_the_sound_folds_together(self) -> None:
        spellings = ("θυμήσου", "θυμίσου", "θυμύσου", "θυμείσου", "θυμοίσου")
        folded = {text.normalize(word) for word in spellings}
        self.assertEqual(folded, {"θιμισου"}, folded)

    def test_the_other_vowel_digraphs_survive(self) -> None:
        # ου is /u/, and the υ of αυ/ευ is a consonant: folding either would
        # merge words that do not sound alike. "που" must not become "πι".
        for word, expected in (
            ("που", "που"),
            ("ούτε", "ουτε"),
            ("αυτό", "αυτο"),
            ("ευχαριστώ", "ευχαριστω"),
            ("Δευτέρα", "δευτερα"),
        ):
            with self.subTest(word=word):
                self.assertEqual(text.normalize(word), expected)

    def test_distinct_sounds_stay_distinct(self) -> None:
        self.assertNotEqual(text.normalize("που"), text.normalize("πι"))
        self.assertNotEqual(text.normalize("ναύτης"), text.normalize("νίτης"))

    def test_the_fold_is_idempotent(self) -> None:
        # connect()'s migration refolds stored search keys, and normalize()
        # itself ends in a fold: applying it twice must change nothing.
        for word in ("θυμήσου", "ευχαριστώ", "ηύρα", "που", "μαθηματικά"):
            with self.subTest(word=word):
                once = text.normalize(word)
                self.assertEqual(text.fold_iotacism(once), once)

    def test_ascii_is_untouched(self) -> None:
        # memory._re() folds a regular expression's *source*, which is only
        # safe because every metacharacter is ASCII and survives the fold.
        source = r"^(?P<h>\d{1,2})[:.](?P<min>\d{2})\s+[A-Za-z_-]+$"
        self.assertEqual(text.fold_iotacism(source), source)

    def test_accents_come_off_without_touching_case(self) -> None:
        # _re() strips accents but must not lower-case: "\S" is not "\s".
        self.assertEqual(text.strip_accents("Θυμήσου Ό,τι"), "Θυμησου Ο,τι")
        self.assertEqual(text.strip_accents(r"\S\s\W"), r"\S\s\W")

    def test_a_folded_pattern_still_compiles_and_matches(self) -> None:
        # What memory._re() does: accents off, iotacism folded, case and
        # metacharacters left exactly as written.
        source = text.fold_iotacism(text.strip_accents(r"\bθυμήσου\s+(?P<body>.+)"))
        match = re.compile(source).search(text.normalize("Θυμίσου ότι κάτι"))
        self.assertIsNotNone(match)
        self.assertEqual(match["body"], "οτι κατι")


class NormalizeTests(unittest.TestCase):
    def test_case_accents_and_final_sigma(self) -> None:
        self.assertEqual(text.normalize("  Τέλος Τζάρβις "), "τελοσ τζαρβισ")

    def test_phrases_normalizes_every_entry(self) -> None:
        self.assertEqual(
            text.phrases("κλείσε", "Τέλος Τζάρβις"), ["κλισε", "τελοσ τζαρβισ"]
        )

    def test_the_shipped_phrase_lists_are_normalized(self) -> None:
        # A phrase written out pre-folded by hand is the bug this guards
        # against: it would stop matching the next time normalize() changes.
        for phrase in text.SHUTDOWN_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertEqual(text.normalize(phrase), phrase)
        for word in text.NUMBER_WORDS:
            with self.subTest(word=word):
                self.assertEqual(text.normalize(word), word)

    def test_number_words_survive_folding_without_collisions(self) -> None:
        self.assertEqual(len(text.NUMBER_WORDS), 31)
        self.assertEqual(text.NUMBER_WORDS[text.normalize("δύο")], 2)
        self.assertEqual(text.NUMBER_WORDS[text.normalize("είκοσι")], 20)

    def test_strip_punctuation(self) -> None:
        self.assertEqual(text.strip_punctuation("τέλος."), "τέλος")


if __name__ == "__main__":
    unittest.main()
