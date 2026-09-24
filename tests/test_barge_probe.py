"""Tests for tools/barge_probe.py's phase bucketing.

The probe is a diagnostic, not part of the app, and it would not normally
carry tests. This file exists because its bucketing was rewritten and then
accused -- reasonably -- of dropping every frame.

What had actually happened was that the microphone dropped off the USB bus,
so the run being read had 13 frames in it and the labelling never saw any
audio at all. That was established by elimination: the CSV holds one row per
captured frame unconditionally, so 13 rows means 13 frames, and no labelling
bug can remove a row. Elimination is not the same as evidence that the new
buckets are *right*, and the rewrite has still never completed a live run.

So the buckets are pinned here on synthetic frames, and pinned through the
CSV rather than the printed summary. The CSV is written by the same `label`
closure the summary counts through, which is the property the rewrite was
partly for ("they were computed by two separate pieces of logic before, which
is a standing invitation for the file and the numbers to disagree") -- so
reading the file back tests the agreement as well as the labels.

The timeline below is the shape of a real streamed reply, and every boundary
in it is one the rewrite claimed to fix:

    0.0  ---- baseline (silent room) ----                    3.0
    3.0  ---- reply: speak() called ------------------------ 20.0
    3.0  synthesis, silent (Edge TTS on the network)          4.0
    4.0  ---- audible chunk 1 ------------------------ 12.0
    6.0  barge prompt fires
    6.6  barge window opens (BARGE_REACTION_LEAD)
    9.1  barge window closes (BARGE_WINDOW_SECONDS)
   12.0  seam: the next sentence is still being synthesized  13.0
   13.0  ---- audible chunk 2 ------------------ 19.0
   19.0  the reply's last synthesis gap                      20.0
   20.0  ---- idle ----
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import barge_probe  # noqa: E402

from jarvis import listener  # noqa: E402


BASELINE_END = 3.0
REPLY = (3.0, 20.0)
AUDIBLE = ((4.0, 12.0), (13.0, 19.0))
PROMPT_AT = 6.0

# Deliberately unequal, so a bucket that swallowed a neighbour would show up
# as a level and not only as a count.
ROOM_DB = -70.0
ECHO_DB = -58.0
SPEECH_DB = -33.0


def _level(stream_time: float) -> float:
    """A plausible level for the moment, so the summary has real numbers."""
    if any(start <= stream_time < end for start, end in AUDIBLE):
        in_barge = PROMPT_AT + barge_probe.BARGE_REACTION_LEAD
        if in_barge <= stream_time < in_barge + barge_probe.BARGE_WINDOW_SECONDS:
            return SPEECH_DB
        return ECHO_DB
    return ROOM_DB


class BucketTestCase(unittest.TestCase):
    """Runs _summarize over the timeline above and reads the CSV back."""

    barge = True

    def setUp(self) -> None:
        # origin 0 makes stream time and the probe's wall clock the same
        # number, so the expectations below read as the timeline they are.
        patcher = mock.patch.object(listener, "_stream_origin", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.csv_path = Path(tempfile.mkdtemp()) / "probe.csv"
        self.addCleanup(lambda: self.csv_path.unlink(missing_ok=True))

        probe = barge_probe._Probe(self.csv_path)
        step = listener.FRAME_SECONDS
        count = int(21.0 / step)
        probe.frames = [(i * step, _level(i * step)) for i in range(count)]
        self.probe = probe

        marks = [
            (0.0, BASELINE_END, "baseline"),
            (REPLY[0], REPLY[1], "reply"),
        ]
        marks += [(start, end, "audible") for start, end in AUDIBLE]

        args = argparse.Namespace(barge=self.barge, csv=str(self.csv_path))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.code = barge_probe._summarize(
                probe, marks, {"at": PROMPT_AT}, args
            )
        self.printed = out.getvalue()

        with self.csv_path.open(encoding="utf-8") as handle:
            self.rows = [
                (float(row["stream_time"]), float(row["level_db"]), row["phase"])
                for row in csv.DictReader(handle)
            ]

    def phases_in(self, start: float, end: float) -> set[str]:
        return {phase for t, _, phase in self.rows if start <= t < end}

    def span_of(self, phase: str) -> tuple[float, float]:
        times = [t for t, _, name in self.rows if name == phase]
        self.assertTrue(times, f"no frames were labelled {phase}")
        return min(times), max(times)

    def count_of(self, phase: str) -> int:
        return sum(1 for _, _, name in self.rows if name == phase)


class PhaseBoundaryTests(BucketTestCase):
    def test_every_captured_frame_is_written_and_labelled(self) -> None:
        # The property the diagnosis rested on: one row per frame, always.
        self.assertEqual(len(self.rows), len(self.probe.frames))
        self.assertEqual(self.code, 0)

    def test_synthesis_silence_is_never_counted_as_echo(self) -> None:
        # The headline of the rewrite. Both silent gaps sit inside the reply
        # and outside every audible interval: the one before the first sound
        # (Edge TTS on the network) and the seam between two sentences.
        self.assertEqual(self.phases_in(3.0, 4.0), {"synthesis"})
        self.assertEqual(self.phases_in(12.0, 13.0), {"synthesis"})
        self.assertEqual(self.phases_in(19.0, 20.0), {"synthesis"})

        # Measured on the run that motivated this: 24 of 75 "echo" frames were
        # really that silence, which dragged echo p50 from -58.2 to -63.7 and
        # doubled the headline margin. So the level is the assertion, not just
        # the label -- no echo frame may sit at the empty room's level.
        echo_levels = {level for _, level, name in self.rows if name == "echo"}
        self.assertEqual(echo_levels, {ECHO_DB})

    def test_the_echo_bucket_is_audible_playback_before_the_prompt(self) -> None:
        self.assertEqual(self.phases_in(4.0, 6.0), {"echo"})
        first, last = self.span_of("echo")
        self.assertAlmostEqual(first, 4.0, places=6)
        self.assertLess(last, PROMPT_AT)

    def test_the_reaction_lead_is_dropped_from_both_buckets(self) -> None:
        # Nobody starts talking the instant they are told to, and counting
        # this stretch either way corrupts the number the probe exists for.
        lead_end = PROMPT_AT + barge_probe.BARGE_REACTION_LEAD
        self.assertEqual(self.phases_in(PROMPT_AT, lead_end), {"reaction"})

    def test_the_barge_bucket_is_bounded_to_its_window(self) -> None:
        # Left unbounded this ran to the end of the reply -- 18.2s for one
        # spoken phrase -- and its median measured the mixture ratio rather
        # than the user's voice.
        opens = PROMPT_AT + barge_probe.BARGE_REACTION_LEAD
        closes = opens + barge_probe.BARGE_WINDOW_SECONDS

        first, last = self.span_of("barge")
        self.assertGreaterEqual(first, opens)
        self.assertLess(last, closes)

        measured = (last - first) + listener.FRAME_SECONDS
        self.assertAlmostEqual(
            measured, barge_probe.BARGE_WINDOW_SECONDS, delta=listener.FRAME_SECONDS
        )

    def test_the_rest_of_the_reply_is_post_not_barge(self) -> None:
        # Still Jarvis talking, and by far the largest audible stretch: if it
        # leaked into the barge bucket it would dominate it.
        self.assertEqual(self.phases_in(9.2, 12.0), {"post"})
        self.assertEqual(self.phases_in(13.0, 19.0), {"post"})
        self.assertGreater(self.count_of("post"), self.count_of("barge"))

    def test_baseline_and_idle_bracket_the_run(self) -> None:
        self.assertEqual(self.phases_in(0.0, BASELINE_END), {"baseline"})
        self.assertEqual(self.phases_in(20.0, 21.0), {"idle"})

    def test_the_csv_and_the_summary_count_the_same_frames(self) -> None:
        # _describe prints "n=<count>" per bucket; the file and the numbers
        # are meant to come from one labelling, so they must agree.
        for label, phase in (
            ("room (silent)", "baseline"),
            ("Jarvis only (echo)", "echo"),
            ("you over Jarvis", "barge"),
        ):
            with self.subTest(phase=phase):
                self.assertRegex(
                    self.printed,
                    rf"{re.escape(label)}\s+n={self.count_of(phase)}\b",
                )

        for phase in ("synthesis", "reaction", "post"):
            with self.subTest(phase=phase):
                self.assertRegex(
                    self.printed, rf"{phase}\s+{self.count_of(phase)}\s+frames"
                )


class EchoOnlyRunTests(BucketTestCase):
    """--echo: no prompt, so every audible frame is echo and nothing is post."""

    barge = False

    def setUp(self) -> None:
        super().setUp()

    def test_the_whole_reply_is_echo(self) -> None:
        self.assertEqual(self.phases_in(4.0, 12.0), {"echo"})
        self.assertEqual(self.phases_in(13.0, 19.0), {"echo"})
        self.assertEqual(self.count_of("barge"), 0)
        self.assertEqual(self.count_of("reaction"), 0)
        self.assertEqual(self.count_of("post"), 0)

    def test_synthesis_is_still_excluded(self) -> None:
        # The bucket the --echo run exists to fill is the one the silence
        # corrupted, so this is the case that mattered most.
        self.assertEqual(self.phases_in(3.0, 4.0), {"synthesis"})
        self.assertEqual(self.phases_in(12.0, 13.0), {"synthesis"})

    def test_it_reports_no_barge_measurement(self) -> None:
        self.assertIn("not measured", self.printed)


if __name__ == "__main__":
    unittest.main()
