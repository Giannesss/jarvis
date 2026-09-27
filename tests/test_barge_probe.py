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
    7.0  barge prompt fires
    7.6  barge window opens  (default: an instant reaction)
   11.6  barge window closes (BARGE_WINDOW_SECONDS)
   12.0  seam: the next sentence is still being synthesized  13.0
   13.0  ---- audible chunk 2 ------------------ 19.0
   19.0  the reply's last synthesis gap                      20.0
   20.0  ---- idle ----

The prompt sits at 7.0 rather than 6.0 so that the pre-prompt stretch can
prime the running median (ECHO_WINDOW_SECONDS) *and* leave enough behind it to
replay the rule on. At 6.0 it could do only the first, so every --barge case
here printed "not enough clean echo" and the decision-rule block went
untested in the one mode that produces it.
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
PROMPT_AT = 7.0

# Deliberately unequal, so a bucket that swallowed a neighbour would show up
# as a level and not only as a count.
ROOM_DB = -70.0
ECHO_DB = -58.0
SPEECH_DB = -33.0


# How long the user talks for. Deliberately shorter than
# BARGE_WINDOW_SECONDS, and no longer derived from it: one ordinary phrase is
# a fixed length, while the window is a bound chosen to contain it with room
# to spare. Tying the two together hid which of them a test was really about.
SPEECH_SECONDS = 2.0


def _level(stream_time: float, speech_at: float) -> float:
    """A plausible level for the moment, so the summary has real numbers."""
    if any(start <= stream_time < end for start, end in AUDIBLE):
        if speech_at <= stream_time < speech_at + SPEECH_SECONDS:
            return SPEECH_DB
        return ECHO_DB
    return ROOM_DB


class BucketTestCase(unittest.TestCase):
    """Runs _summarize over the timeline above and reads the CSV back."""

    barge = True

    # When the user starts talking, as a stream time. The default is an
    # instant reaction, which is what the fixed-lead window assumed of
    # everybody; subclasses move it to cover the reactions real people have.
    speech_at = PROMPT_AT + barge_probe.BARGE_REACTION_LEAD

    def level(self, stream_time: float) -> float:
        """The fixture's audio, as a hook so a subclass can vary the echo."""
        return _level(stream_time, self.speech_at)

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
        probe.frames = [(i * step, self.level(i * step)) for i in range(count)]
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
        # Anchored to the bucket's own first frame rather than to a separately
        # computed boundary: the window now opens on a *detected* onset, whose
        # float is not bit-identical to PROMPT_AT + lead, and comparing the two
        # put `last` exactly on `closes` -- a failure that printed as
        # "11.6 not less than 11.6".
        first, _ = self.span_of("barge")
        self.assertAlmostEqual(
            first,
            PROMPT_AT + barge_probe.BARGE_REACTION_LEAD,
            delta=listener.FRAME_SECONDS,
        )
        # Counted in frames, not measured in seconds. The window's end is a
        # float sum that a frame time can land either side of, and these times
        # have additionally been through the CSV's 3-decimal formatting -- so
        # the seconds form of this assertion compared 4.08 against 4.0 with a
        # delta of 0.08 and failed on the last bit. One frame of slack, in
        # integers, is both the honest precision and exactly stable.
        nominal = barge_probe.BARGE_WINDOW_SECONDS / listener.FRAME_SECONDS
        self.assertAlmostEqual(self.count_of("barge"), nominal, delta=1)

    def test_the_rest_of_the_reply_is_post_not_barge(self) -> None:
        # Still Jarvis talking, and by far the largest audible stretch: if it
        # leaked into the barge bucket it would dominate it.
        #
        # Derived from the constants rather than written out: this read 9.2
        # while the window was 2.5s wide, and widening it to 4.0 moved the
        # boundary under a test that had the old one hardcoded.
        closes = (
            PROMPT_AT
            + barge_probe.BARGE_REACTION_LEAD
            + barge_probe.BARGE_WINDOW_SECONDS
        )
        self.assertEqual(self.phases_in(closes + listener.FRAME_SECONDS, 12.0), {"post"})
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


class LateReactionTests(BucketTestCase):
    """The failure the onset detector exists for.

    Live, the prompt was answered 5.8s after it appeared, against a window
    that opened 0.6s after it and shut 2.5s later. The barge bucket came back
    holding 2.6s of empty room -- p50 -65.9 dB against that run's own silent
    baseline of -66.5 -- the summary reported its +1.9 dB margin as though it
    had measured a voice, and the voice itself (peaking -14.1 dB) was filed as
    `post` and discarded. Recomputed against a real echo floor the same burst
    reached +25.8 dB over a +11.9 dB false-fire bound, so the run had been
    read as evidence against a feature it was actually evidence for.

    Here the answer comes 8.0s after the prompt, which also puts it in the
    *second* audible chunk -- the onset has to be findable across a synthesis
    seam, not just late.
    """

    speech_at = 14.0

    def test_the_barge_bucket_lands_on_the_voice(self) -> None:
        first, _ = self.span_of("barge")
        self.assertAlmostEqual(first, self.speech_at, delta=listener.FRAME_SECONDS)

    def test_the_barge_bucket_is_not_room_tone(self) -> None:
        # The assertion that would have caught the live run. A bucket of the
        # wrong frames is still a full bucket, so counting them proves
        # nothing; the level is the whole evidence.
        levels = {level for _, level, name in self.rows if name == "barge"}
        self.assertIn(SPEECH_DB, levels)
        self.assertNotIn(ROOM_DB, levels)

    def test_the_whole_wait_is_dropped_as_reaction(self) -> None:
        # 8s of it, where the fixed lead always called it 0.6s. These frames
        # are echo-only but sit after the prompt, so they are neither the
        # clean prime nor the user's voice.
        self.assertEqual(self.phases_in(PROMPT_AT, 12.0), {"reaction"})
        self.assertGreater(
            self.count_of("reaction") * listener.FRAME_SECONDS,
            barge_probe.BARGE_REACTION_LEAD * 2,
        )

    def test_it_reports_where_the_onset_was_found(self) -> None:
        waited = self.speech_at - PROMPT_AT
        self.assertRegex(
            self.printed, rf"Speech onset found {waited:.2f}s after the prompt"
        )

    def test_the_rule_still_measures_the_barge(self) -> None:
        # The point of the exercise: a slow reaction must still produce a
        # number, not "(not measured)" and not one derived from room tone.
        self.assertRegex(self.printed, r"you over Jarvis\s+\+\d+\.\d dB")
        self.assertNotIn("no usable margin", self.printed)


class DriftingEchoTests(BucketTestCase):
    """The echo is not the same level when you start as it was at the prompt.

    Which is the second half of the live failure, and the flattering half. On
    that run the mic heard essentially nothing for the reply's first 12s --
    the "echo" stretch sat at the room floor, -66.2 dB -- while the clean
    --echo run a minute earlier measured the same playback at -57.1 dB. Any
    reach computed against the pre-prompt frames was therefore 9.2 dB too
    generous, and 9.2 dB is most of the margin the feature is being judged on.

    So the barge replay is primed on the audible audio immediately before the
    onset, which is what the live decider would be holding. Here the echo is
    QUIET_ECHO_DB until the prompt and ECHO_DB afterwards, so the two answers
    differ by a known amount and the test can say which one was used.
    """

    speech_at = 14.0

    # Only 4 dB below ECHO_DB, and it cannot be much more: the detector's
    # floor is ONSET_DETECT_OVER_ECHO_P95_DB (6 dB) over the *pre-prompt*
    # p95, so a playback level that climbs by more than that after the prompt
    # is read as speech and the onset lands at the prompt instead. That is a
    # real limitation of keying the floor to the echo measured so far, and
    # the reason the --echo run establishes the bound rather than this one.
    # Written at -68 first, which tripped exactly that and put the onset at
    # the prompt with a +10.0 dB reach.
    QUIET_ECHO_DB = -62.0

    def level(self, stream_time: float) -> float:
        base = _level(stream_time, self.speech_at)
        if base == ECHO_DB and stream_time < PROMPT_AT:
            return self.QUIET_ECHO_DB
        return base

    def reported_reach(self) -> float:
        match = re.search(r"you over Jarvis\s+([+-]\d+\.\d) dB", self.printed)
        self.assertIsNotNone(match, f"no barge reach in:\n{self.printed}")
        return float(match.group(1))

    def test_the_reach_is_measured_against_the_echo_at_the_onset(self) -> None:
        self.assertAlmostEqual(self.reported_reach(), SPEECH_DB - ECHO_DB, delta=0.1)

    def test_it_is_not_measured_against_the_pre_prompt_echo(self) -> None:
        # The bug, stated as the number it would have printed: priming on the
        # quiet pre-prompt frames inflates the reach by the drift.
        inflated = SPEECH_DB - self.QUIET_ECHO_DB
        self.assertLess(self.reported_reach(), inflated - 1.0)


class NobodySpokeTests(BucketTestCase):
    """Nothing above the echo anywhere after the prompt.

    The fallback matters far less than the announcement. The live run's real
    defect was not that its window was in the wrong place -- it was that it
    reported a margin from an empty bucket in complete silence, so the number
    looked like a measurement and read as a verdict on the feature.
    """

    speech_at = 100.0  # never, inside a 21s timeline

    def test_it_says_so_instead_of_reporting_a_margin(self) -> None:
        self.assertIn("No speech onset found", self.printed)
        self.assertIn("room tone", self.printed)

    def test_the_window_falls_back_to_the_fixed_lead(self) -> None:
        opens = PROMPT_AT + barge_probe.BARGE_REACTION_LEAD
        first, last = self.span_of("barge")
        self.assertAlmostEqual(first, opens, delta=listener.FRAME_SECONDS)
        self.assertLess(last, opens + barge_probe.BARGE_WINDOW_SECONDS)

    def test_the_bucket_is_echo_and_is_admitted_to_be(self) -> None:
        levels = {level for _, level, name in self.rows if name == "barge"}
        self.assertEqual(levels, {ECHO_DB})


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
