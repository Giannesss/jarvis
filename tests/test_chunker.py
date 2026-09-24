"""Tests for jarvis/chunker.py -- cutting a reply into pieces worth speaking.

Written the way the module is: from lists, with no clock, no network and no
audio, like tests/test_record_timing.py drives _StopDecider.

The negatives are the ones that matter. Loosening where a chunk may be cut
trades a slightly earlier first word for a reply that is spoken in the wrong
shape -- a full stop honoured inside "3.5" or "π.χ." puts a spoken pause in
the middle of a number or an abbreviation, and unlike a missed cut, nobody
can tell from the transcript that it happened.
"""

from __future__ import annotations

import unittest

from jarvis import chunker


def _feed_all(sink: chunker.Chunker, text: str, size: int = 3) -> list[str]:
    """Push `text` through in small deltas, the way a token stream arrives."""
    out: list[str] = []
    for start in range(0, len(text), size):
        out += sink.feed(text[start : start + size])
    return out


def _sentences_only(**kwargs) -> chunker.Chunker:
    return chunker.Chunker(early_first_chunk=False, min_chars=0, **kwargs)


class SentenceBoundaryTests(unittest.TestCase):
    def test_each_sentence_is_emitted_as_it_completes(self) -> None:
        sink = _sentences_only()
        self.assertEqual(sink.feed("Καλημέρα."), ["Καλημέρα."])
        self.assertEqual(sink.feed(" Τι κάνεις"), [])
        self.assertEqual(sink.feed(";"), ["Τι κάνεις;"])

    def test_both_semicolons_end_a_sentence(self) -> None:
        # brain._SENTENCE_ENDINGS holds only U+037E, the formal Greek
        # question mark. Models emit U+003B. Here that difference is audible,
        # so both are boundaries.
        for mark, name in ((";", "U+003B"), (";", "U+037E")):
            with self.subTest(name):
                sink = _sentences_only()
                self.assertEqual(
                    sink.feed(f"Τι ώρα είναι{mark}"), [f"Τι ώρα είναι{mark}"]
                )

    def test_ano_teleia_ends_a_sentence(self) -> None:
        sink = _sentences_only()
        self.assertEqual(sink.feed("Ήρθε·"), ["Ήρθε·"])

    def test_a_run_of_marks_ends_once_at_its_last_character(self) -> None:
        sink = _sentences_only()
        self.assertEqual(sink.feed("Τι!!! "), ["Τι!!!"])

    def test_a_closing_quote_stays_with_its_sentence(self) -> None:
        sink = _sentences_only()
        self.assertEqual(sink.feed('Είπε «ναι».  Μετά έφυγε.'),
                         ['Είπε «ναι».', "Μετά έφυγε."])


class NotABoundaryTests(unittest.TestCase):
    """Full stops that end nothing. Each of these would put a spoken pause
    inside a word or a number."""

    def test_a_decimal_point_is_not_a_sentence(self) -> None:
        # Multi-digit on purpose. "3.5" is also caught by the single-initial
        # rule ("3" is one character), so testing with it exercises the wrong
        # guard entirely -- removing the decimal check leaves such a test
        # passing. "13.5" reaches the decimal check and nothing else.
        sink = _sentences_only()
        self.assertEqual(sink.feed("Κάνει 13.5 βαθμούς"), [])
        self.assertEqual(sink.feed(" σήμερα."), ["Κάνει 13.5 βαθμούς σήμερα."])

    def test_a_year_or_price_is_not_a_sentence(self) -> None:
        sink = _sentences_only()
        self.assertEqual(sink.feed("Κοστίζει 1250.80 ευρώ"), [])
        self.assertEqual(sink.feed(" συνολικά."), ["Κοστίζει 1250.80 ευρώ συνολικά."])

    def test_common_abbreviations_are_not_sentences(self) -> None:
        for abbreviation in ("π.χ.", "κ.λπ.", "δηλ.", "βλ.", "μ.μ."):
            with self.subTest(abbreviation):
                sink = _sentences_only()
                self.assertEqual(sink.feed(f"Έχεις μάθημα, {abbreviation} "), [])

    def test_an_initial_is_not_a_sentence(self) -> None:
        sink = _sentences_only()
        self.assertEqual(sink.feed("Ο Γ. Παπαδόπουλος "), [])

    def test_a_sentence_after_an_abbreviation_still_ends(self) -> None:
        sink = _sentences_only()
        self.assertEqual(
            sink.feed("Πάρε γάλα, ψωμί κ.λπ. από το μαγαζί."),
            ["Πάρε γάλα, ψωμί κ.λπ. από το μαγαζί."],
        )


class MinimumChunkTests(unittest.TestCase):
    def test_a_very_short_sentence_is_merged_into_the_next(self) -> None:
        # «Ναι.» alone would spend a whole Edge round trip on one syllable.
        sink = chunker.Chunker(early_first_chunk=False, min_chars=16)
        self.assertEqual(sink.feed("Ναι."), [])
        self.assertEqual(
            sink.feed(" Θα σου το θυμίσω αύριο το πρωί."),
            ["Ναι. Θα σου το θυμίσω αύριο το πρωί."],
        )

    def test_a_short_reply_that_never_grows_is_still_spoken(self) -> None:
        # Merging must never swallow a reply that simply is short.
        sink = chunker.Chunker(early_first_chunk=False, min_chars=16)
        self.assertEqual(sink.feed("Ναι."), [])
        self.assertEqual(sink.flush(), "Ναι.")


class EarlyFirstChunkTests(unittest.TestCase):
    """The knob that decides whether streaming is audible at all."""

    def test_the_first_chunk_breaks_at_a_comma_once_long_enough(self) -> None:
        sink = chunker.Chunker(
            early_first_chunk=True, first_min=20, first_max=140, min_chars=0
        )
        text = "Λοιπόν, να σου πω τι έχω δει μέχρι τώρα, και μετά συνεχίζουμε."

        chunks = _feed_all(sink, text)
        self.assertTrue(chunks, "the first chunk should not wait for the full stop")
        self.assertTrue(chunks[0].endswith(","))
        # The very first comma is too early to be worth it; the break is the
        # first one past first_min.
        self.assertGreaterEqual(len(chunks[0]), 20)

    def test_only_the_first_chunk_breaks_early(self) -> None:
        sink = chunker.Chunker(
            early_first_chunk=True, first_min=20, first_max=140, min_chars=0
        )
        _feed_all(sink, "Λοιπόν, να σου πω τι έχω δει μέχρι τώρα, και τελείωσα.")
        sink.feed(" ")

        # Everything after the first chunk waits for real sentence ends.
        later = sink.feed("Δεύτερη πρόταση, με κόμμα μέσα της, συνεχίζει")
        self.assertEqual(later, [])
        self.assertEqual(sink.feed("."), ["Δεύτερη πρόταση, με κόμμα μέσα της, συνεχίζει."])

    def test_a_comma_less_opening_is_cut_at_a_word_boundary(self) -> None:
        sink = chunker.Chunker(
            early_first_chunk=True, first_min=10, first_max=30, min_chars=0
        )
        text = "αυτη ειναι μια πολυ μεγαλη προταση χωρις κομματα καθολου"
        chunks = _feed_all(sink, text)

        self.assertTrue(chunks)
        self.assertLessEqual(len(chunks[0]), 30)
        # The cut must land between words, never inside one: the chunk is a
        # prefix of the reply, and the character following it is a space.
        self.assertTrue(text.startswith(chunks[0]))
        self.assertEqual(text[len(chunks[0])], " ")

    def test_disabled_means_whole_sentences_only(self) -> None:
        sink = chunker.Chunker(early_first_chunk=False, min_chars=0)
        self.assertEqual(
            _feed_all(sink, "Λοιπόν, να σου πω τι έχω δει μέχρι τώρα, και μετά"), []
        )


class FlushTests(unittest.TestCase):
    def test_flush_speaks_the_tail_of_a_reply_that_ended_normally(self) -> None:
        sink = _sentences_only()
        sink.feed("Τελείωσα χωρίς τελεία")
        self.assertEqual(sink.flush(), "Τελείωσα χωρίς τελεία")

    def test_a_truncated_reply_drops_its_unfinished_tail(self) -> None:
        # MAX_REPLY_TOKENS cut the model off mid-sentence. The old
        # non-streaming path repaired that with _trim_to_last_sentence; here
        # the tail was simply never emitted, so dropping it is the same
        # behaviour and costs nothing.
        sink = _sentences_only()
        self.assertEqual(sink.feed("Πρώτη πρόταση. Δεύτερη που κόπηκε στη μέ"),
                         ["Πρώτη πρόταση."])
        self.assertIsNone(sink.flush(truncated=True))

    def test_flush_returns_nothing_twice(self) -> None:
        sink = _sentences_only()
        sink.feed("Κάτι")
        self.assertEqual(sink.flush(), "Κάτι")
        self.assertIsNone(sink.flush())


class DeltaShapeTests(unittest.TestCase):
    def test_the_result_does_not_depend_on_how_deltas_are_split(self) -> None:
        # A token stream splits wherever the tokenizer says, including
        # between a letter and its full stop.
        text = "Καλημέρα. Τι κάνεις σήμερα; Όλα καλά εδώ."
        expected = ["Καλημέρα.", "Τι κάνεις σήμερα;", "Όλα καλά εδώ."]

        for size in (1, 2, 3, 7, 40):
            with self.subTest(delta=size):
                sink = _sentences_only()
                chunks = _feed_all(sink, text, size=size)
                tail = sink.flush()
                if tail:
                    chunks.append(tail)
                self.assertEqual(chunks, expected)

    def test_an_empty_delta_changes_nothing(self) -> None:
        sink = _sentences_only()
        self.assertEqual(sink.feed(""), [])
        self.assertEqual(sink.feed("Ναι."), ["Ναι."])

    def test_the_spoken_text_is_the_reply_with_only_whitespace_lost(self) -> None:
        # Nothing may be dropped or duplicated: this is what gets committed
        # to the conversation history.
        text = "Πρώτη. Δεύτερη! Τρίτη; Τέταρτη…"
        sink = _sentences_only()
        chunks = _feed_all(sink, text, size=5)
        tail = sink.flush()
        if tail:
            chunks.append(tail)

        self.assertEqual(" ".join(chunks), text)


if __name__ == "__main__":
    unittest.main()
