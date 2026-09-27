"""Tests for barge-in: the rule, the arming, and the audio it keeps.

The rule is a port of what tools/barge_probe.py measured, so these tests are
written in the units the probe reports: dB over a running median. The numbers
come from the clean run that produced BARGE_IN_MARGIN_DB=9 --

    echo alone reaches  ~+4 dB   -> the false-fire bound
    speech over him     ~+15 dB  -> the miss bound

-- and the cases below are the rows of its would-have-fired table: 9 and 12 dB
fire on the barge with no false fire, 15 dB and up miss it.

Everything here is levels and frames. Nothing opens a microphone, nothing plays
a sound, and _BargeDecider itself has no clock, no queue and no I/O -- driven
from a list, the same way _StopDecider is.
"""

from __future__ import annotations

import array
import io
import contextlib
import unittest
from unittest import mock

from jarvis import listener


FRAME = listener.FRAME_SECONDS  # 0.08

# The shape of an echo-only stretch: his own voice through the speakers, whose
# vowel peaks reach a few dB over its own running median and no further.
ECHO_FLOOR = -45.0
ECHO_PEAK = -41.0  # +4 dB, the measured false-fire bound

# Someone talking over him at a normal volume: +17 dB on the same floor.
VOICE = -28.0

# Exactly +15 dB over the floor, the margin at which the probe's table went
# from firing to missing. Used to pin the boundary rather than to recommend it.
VOICE_AT_15 = -30.0


def decider(**kwargs) -> listener._BargeDecider:
    """A decider with the shipped settings unless a test overrides one."""
    settings = {
        "margin_db": 9.0,
        "onset_seconds": 0.32,  # 4 frames
        "window_seconds": 2.0,  # 25 frames
        "min_prime_seconds": 1.0,  # 12 frames
    }
    settings.update(kwargs)
    return listener._BargeDecider(**settings)


def feed(target: listener._BargeDecider, levels, start: float = 0.0):
    """Feed levels one frame apart. Returns the index it fired on, or None."""
    fired_at = None
    for index, level in enumerate(levels):
        if target.feed(start + index * FRAME, level) and fired_at is None:
            fired_at = index
    return fired_at


def echo(frames: int) -> list[float]:
    """An echo-only stretch, wobbling the way his voice does."""
    pattern = [ECHO_FLOOR, ECHO_FLOOR, ECHO_PEAK, ECHO_FLOOR]
    return [pattern[i % len(pattern)] for i in range(frames)]


class DecisionRuleTests(unittest.TestCase):
    def test_a_voice_over_him_fires_after_a_sustained_onset(self) -> None:
        target = decider()
        fired_at = feed(target, echo(25) + [VOICE] * 6)

        # Not on the first loud frame: the run has to last ONSET_SECONDS, which
        # is what keeps a door or a desk knock from cutting off a reply.
        self.assertEqual(fired_at, 25 + 3)
        self.assertAlmostEqual(target.delta_db, VOICE - ECHO_FLOOR, places=1)
        self.assertAlmostEqual(target.onset_at, 25 * FRAME, places=3)

    def test_his_own_voice_never_fires(self) -> None:
        # The false-fire case, and the expensive one: a reply cut off for
        # nothing, which the user hears as Jarvis losing his train of thought.
        self.assertIsNone(feed(decider(), echo(200)))

    def test_a_knock_is_too_short_to_count(self) -> None:
        # Three frames is 0.24s, under the 0.32s the rule asks for.
        self.assertIsNone(feed(decider(), echo(25) + [VOICE] * 3 + echo(20)))

    def test_fifteen_db_misses_what_nine_catches(self) -> None:
        # The table's upper rows. Same audio, two margins.
        loud = echo(25) + [VOICE_AT_15] * 8
        self.assertIsNotNone(feed(decider(margin_db=9.0), loud))
        self.assertIsNone(feed(decider(margin_db=15.0), loud))

    def test_twelve_db_also_fires_on_a_real_barge(self) -> None:
        self.assertIsNotNone(feed(decider(margin_db=12.0), echo(25) + [VOICE] * 6))

    def test_it_fires_once_and_then_stays_quiet(self) -> None:
        target = decider()
        levels = echo(25) + [VOICE] * 20
        fires = [
            target.feed(index * FRAME, level) for index, level in enumerate(levels)
        ]
        self.assertEqual(fires.count(True), 1)

    def test_the_floor_is_a_median_a_barge_cannot_drag_up(self) -> None:
        # A mean over 25 frames would be pulled ~2.7 dB per loud frame and stop
        # being cleared; a median cannot move until half the window has turned
        # over, which is far longer than the onset.
        target = decider()
        self.assertIsNotNone(feed(target, echo(25) + [VOICE] * 6))
        self.assertAlmostEqual(target.floor_db, ECHO_FLOOR, places=1)


class PrimingTests(unittest.TestCase):
    def test_it_will_not_fire_before_a_floor_exists(self) -> None:
        # Loud from the very first frame: the only "floor" available is the
        # interruption itself, so there is nothing to compare against.
        self.assertIsNone(feed(decider(), [VOICE] * 8))

    def test_a_short_reply_can_still_be_interrupted_once_primed(self) -> None:
        # 12 frames is 0.96s -- the blind window at the start of a reply.
        fired_at = feed(decider(), echo(12) + [VOICE] * 6)
        self.assertEqual(fired_at, 12 + 3)

    def test_the_run_does_not_carry_over_from_the_unprimed_stretch(self) -> None:
        # Levels already high while the window filled must not count as an
        # onset the instant the floor becomes available.
        target = decider(min_prime_seconds=0.32)  # 4 frames
        self.assertIsNone(feed(target, [VOICE] * 4 + echo(1)))


class ArmingTests(unittest.TestCase):
    """The module-level state: what the reader thread feeds, and what a reply
    leaves behind for the next one."""

    def setUp(self) -> None:
        for name, value in (
            ("_barge", None),
            ("_barge_on_fire", None),
            ("_barge_frames", None),
            ("_barge_window", None),
            # None skips _collect_barge_frames' catch-up wait: there is no
            # stream here for the reader to catch up on.
            ("_stream_origin", None),
        ):
            patcher = mock.patch.object(listener, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def frame(level_db: float) -> bytes:
        """One frame at roughly the requested level."""
        amplitude = int(32768 * (10 ** (level_db / 20)))
        return array.array("h", [amplitude] * listener.FRAME_SAMPLES).tobytes()

    def feed_frames(self, levels, start: float = 0.0) -> None:
        for index, level in enumerate(levels):
            with contextlib.redirect_stdout(io.StringIO()):
                listener._feed_barge(start + index * FRAME, self.frame(level))

    def test_nothing_is_fed_when_nothing_is_armed(self) -> None:
        # The ordinary case: the reader thread calls this for every gated frame
        # of every beep and every reply, armed or not.
        self.feed_frames([VOICE] * 10)
        self.assertFalse(listener.barge_fired())
        self.assertIsNone(listener.disarm_barge())

    def test_an_interruption_calls_the_stop_once(self) -> None:
        stops = []
        listener.arm_barge(lambda: stops.append(1))
        self.feed_frames(echo(25) + [VOICE] * 10)

        self.assertEqual(len(stops), 1)
        self.assertTrue(listener.barge_fired())

    def test_a_raising_stop_callback_does_not_kill_the_reader(self) -> None:
        # _feed_barge runs on the only thread draining ffmpeg's stdout. If it
        # raises, every consumer starves.
        def explode() -> None:
            raise RuntimeError("no player")

        listener.arm_barge(explode)
        self.feed_frames(echo(25) + [VOICE] * 10)  # must not raise
        self.assertTrue(listener.barge_fired())

    def test_the_interrupting_audio_is_kept_from_the_onset(self) -> None:
        listener.arm_barge(lambda: None)
        self.feed_frames(echo(25) + [VOICE] * 10)

        result = listener.disarm_barge(collect=True)
        self.assertIsNotNone(result)
        # From the onset (minus one frame of lead) to the end of what was fed:
        # 10 loud frames, the 4th of which fired, plus the lead.
        self.assertEqual(len(result.frames), 10 + int(listener.BARGE_PREROLL_LEAD / FRAME))
        self.assertAlmostEqual(
            result.frames[0][0], 25 * FRAME - listener.BARGE_PREROLL_LEAD, places=3
        )

    def test_collect_false_keeps_no_audio(self) -> None:
        listener.arm_barge(lambda: None)
        self.feed_frames(echo(25) + [VOICE] * 10)

        result = listener.disarm_barge(collect=False)
        self.assertIsNotNone(result)  # it still fired
        self.assertEqual(result.frames, [])

    def test_an_uninterrupted_reply_returns_nothing(self) -> None:
        listener.arm_barge(lambda: None)
        self.feed_frames(echo(60))
        self.assertIsNone(listener.disarm_barge(collect=True))

    def test_the_floor_carries_over_to_the_next_reply(self) -> None:
        # Why this exists: without it every reply would have a ~1s blind window
        # at the start, because the window would begin empty each time. His
        # voice through these speakers is the same next reply as this one.
        listener.arm_barge(lambda: None)
        self.feed_frames(echo(30))
        listener.disarm_barge()

        stops = []
        listener.arm_barge(lambda: stops.append(1))
        # Interrupted immediately -- far inside what the prime would have cost.
        self.feed_frames([VOICE] * 5, start=100.0)
        self.assertEqual(len(stops), 1)

    # A stream restart forgetting the floor is pinned in test_record_timing.py's
    # DeviceOpenTests, which already has a fake ffmpeg that starts successfully.


class ConversationWiringTests(unittest.TestCase):
    """What main._converse() does with an interruption.

    One rule here matters more than the rest: an interrupted reply must **not**
    be followed by listener.flush(). The flush is what keeps Jarvis from
    recording his own voice as the next command, and after a barge-in it would
    raise the floor past the second the user is still speaking in -- throwing
    away the rest of the sentence that stopped him. The audible part is already
    out of the pipeline (the capture gate refused it) and comes back as the
    pre-roll instead.
    """

    def setUp(self) -> None:
        import main

        self.main = main
        self.turns: list[dict] = []
        self.flushes = 0

        replies = iter([("κάτι", "speech_end"), (None, "no_speech")])

        def record_command(preroll=b"", timeout=None, preroll_frames=None):
            self.turns.append({"preroll_frames": preroll_frames})
            return next(replies)

        def flush() -> None:
            self.flushes += 1

        for name, value in (
            ("record_command", record_command),
            ("flush", flush),
            ("acknowledge", lambda: None),
        ):
            patcher = mock.patch.object(main.listener, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        patcher = mock.patch.object(main.speaker, "beep_done", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)

        patcher = mock.patch.object(main, "CONVERSATION_MODE", True)
        patcher.start()
        self.addCleanup(patcher.stop)

        main.skills.shutdown_requested = False
        self.addCleanup(setattr, main.skills, "shutdown_requested", False)

    def converse(self, answer) -> bool:
        with mock.patch.object(self.main, "_answer", return_value=answer):
            with contextlib.redirect_stdout(io.StringIO()):
                return self.main._converse(b"")

    def test_an_interruption_is_not_flushed_away(self) -> None:
        frames = [(1.0, b"\x00" * listener.FRAME_BYTES)]
        barge = listener.BargeResult(onset_at=1.0, delta_db=17.0, frames=frames)

        self.assertTrue(self.converse(self.main.Answer(True, barge)))

        self.assertEqual(self.flushes, 0)
        # ...and the words that interrupted him open the next turn.
        self.assertEqual(self.turns[1]["preroll_frames"], frames)

    def test_an_ordinary_reply_is_still_flushed(self) -> None:
        # The other half of the rule: without this, the next turn records the
        # tail of Jarvis's own reply as the user's command.
        self.assertTrue(self.converse(self.main.Answer(True, None)))

        self.assertEqual(self.flushes, 1)
        self.assertIsNone(self.turns[1]["preroll_frames"])

    def test_an_interruption_with_no_kept_audio_is_still_not_flushed(self) -> None:
        # BARGE_PREROLL=false: the reply stops and the command is whatever is
        # said next. The user is mid-sentence either way, so the floor must
        # still stay where it is.
        barge = listener.BargeResult(onset_at=1.0, delta_db=17.0, frames=[])

        self.assertTrue(self.converse(self.main.Answer(True, barge)))

        self.assertEqual(self.flushes, 0)
        self.assertIsNone(self.turns[1]["preroll_frames"])


if __name__ == "__main__":
    unittest.main()
