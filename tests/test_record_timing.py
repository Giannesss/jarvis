"""Tests for the recording clock: stop decisions, holes, and the capture gate.

These exist because the bugs they pin were found by hand, over hours, from a
single misleading line of output ("[timing] Recording: 43.06s"). Two hand
tests out of many showed a recording of 43.06s while the user spoke normally,
and one of 0.05s that captured nothing at all and answered "Δεν κατάλαβα τι
είπες". Both came from the same place: stop decisions were counted in
*frames*, which measure neither audio nor time, while ffmpeg delivers ~1s of
audio in a millisecond-long burst and the capture gate silently drops frames
in between.

  * 0.05s — a burst that was already buffered satisfied the whole
    end-of-speech rule (2 loud frames + 13 quiet ones = 15 frames = 1.2s of
    stale audio, drained in ~8ms) before the user had said a word.
  * 43.06s — the 187-frame budget (14.96s of audio) collected from a starved
    stream: 187 × 0.2303s = 43.07s of wall clock, with the wall-clock
    backstop unreachable because it lived inside the queue.Empty branch.

So the decision logic is now a pure state machine over (timestamp, level)
pairs, and it is tested the way it is written: from a list, with no
microphone, no threads and no clock. The integration tests below drive
record_command() off a pre-filled queue, which is exactly the shape that
produced the 0.05s reading in the first place.
"""

from __future__ import annotations

import array
import contextlib
import io
import queue
import threading
import re
import time
import unittest
from unittest import mock

from jarvis import listener, speaker


FRAME = listener.FRAME_SECONDS  # 0.08

LOUD = array.array("h", [8000] * listener.FRAME_SAMPLES).tobytes()  # ≈ -12 dB
QUIET = b"\x00" * listener.FRAME_BYTES  # silent, well under the -35 dB floor

LOUD_DB = listener._frame_db(LOUD)
QUIET_DB = listener._frame_db(QUIET)


def stream(start: float, pattern: str, step: float = FRAME):
    """Frames from a pattern string: "L" loud, "." quiet.

    step is how far the *recording* clock advances per frame. A step larger
    than one frame is a hole: audio the microphone heard and we never got.
    """
    return [
        (start + i * step, LOUD if mark == "L" else QUIET)
        for i, mark in enumerate(pattern)
    ]


class StopDeciderTests(unittest.TestCase):
    """The decision logic, fed by hand. No queue, no clock, no audio."""

    def feed_all(self, decider, frames):
        """Feed frames until one stops the recording; return that reason."""
        for ts, frame in frames:
            verdict = decider.feed(ts, listener._frame_db(frame))
            if verdict is not None:
                return verdict
        return None

    def test_speech_then_a_full_second_of_quiet_ends_the_turn(self) -> None:
        decider = listener._StopDecider(no_speech_timeout=8.0)

        # Two loud frames start speech (ONSET_SECONDS), then quiet must last
        # SILENCE_DURATION before it counts as the end.
        self.assertIsNone(self.feed_all(decider, stream(0.0, "LLLL" + "." * 12)))
        self.assertTrue(decider.speech_started)

        # 12 quiet frames is 0.96s -- still short of the second.
        self.assertEqual(decider.feed(16 * FRAME, QUIET_DB), "speech_end")

    def test_a_burst_decides_the_same_as_a_trickle(self) -> None:
        # The whole point: identical frames, so identical verdicts, whether
        # they arrived over a second or all at once in a millisecond.
        frames = stream(0.0, "LLLL" + "." * 13)

        fast = listener._StopDecider(no_speech_timeout=8.0)
        slow = listener._StopDecider(no_speech_timeout=8.0)

        self.assertEqual(self.feed_all(fast, frames), "speech_end")
        self.assertEqual(self.feed_all(slow, frames), "speech_end")
        self.assertEqual(fast.audio_seconds, slow.audio_seconds)

    def test_silence_before_speech_never_ends_the_turn(self) -> None:
        # A pause after the wake word is not the end of a sentence nobody
        # started. Only no_speech_timeout ends this.
        decider = listener._StopDecider(no_speech_timeout=8.0)
        self.assertIsNone(self.feed_all(decider, stream(0.0, "." * 50)))
        self.assertFalse(decider.speech_started)

    def test_nothing_said_stops_with_no_speech_on_the_audio_clock(self) -> None:
        decider = listener._StopDecider(no_speech_timeout=1.0)
        # 1.0s of audio is 12.5 frames, so the 13th crosses it.
        self.assertIsNone(self.feed_all(decider, stream(0.0, "." * 12)))
        self.assertEqual(decider.feed(12 * FRAME, QUIET_DB), "no_speech")

    def test_talking_forever_stops_at_the_audio_budget(self) -> None:
        decider = listener._StopDecider(no_speech_timeout=8.0, max_seconds=1.0)
        self.assertEqual(self.feed_all(decider, stream(0.0, "L" * 40)), "max")
        self.assertGreaterEqual(decider.audio_seconds, 1.0)

    def test_a_small_hole_is_padded_and_counted_as_quiet(self) -> None:
        decider = listener._StopDecider(no_speech_timeout=8.0)
        self.assertIsNone(self.feed_all(decider, stream(0.0, "LL")))

        # 0.4s of audio never arrived, then a quiet frame.
        self.assertIsNone(decider.feed(2 * FRAME + 0.4, QUIET_DB))
        self.assertAlmostEqual(decider.pad_seconds, 0.4)
        self.assertAlmostEqual(decider.lost_seconds, 0.4)

        # The hole counts toward the second of silence: 0.4 already gone, so
        # 0.6s more of quiet ends it rather than a further full second.
        self.assertEqual(self.feed_all(decider, stream(3 * FRAME + 0.4, "." * 8)), "speech_end")

    def test_padding_keeps_the_audio_length_honest(self) -> None:
        decider = listener._StopDecider(no_speech_timeout=8.0)
        decider.feed(0.0, LOUD_DB)
        decider.feed(0.5, LOUD_DB)  # 0.42s hole

        # Two frames of audio, but the recording really spans 0.58s. Without
        # padding, Whisper would hear those two frames welded together.
        self.assertAlmostEqual(decider.audio_seconds, 0.5 + FRAME)
        self.assertAlmostEqual(decider.pad_seconds, 0.42)

    def test_a_hole_breaks_a_run_of_loud_frames(self) -> None:
        # One loud frame either side of a hole is not 0.16s of speech.
        decider = listener._StopDecider(no_speech_timeout=8.0)
        decider.feed(0.0, LOUD_DB)
        decider.feed(0.3, LOUD_DB)
        self.assertFalse(decider.speech_started)

    def test_a_large_hole_aborts_the_utterance(self) -> None:
        decider = listener._StopDecider(no_speech_timeout=8.0)
        self.assertIsNone(self.feed_all(decider, stream(0.0, "LLL")))

        # Longer than MAX_GAP_SECONDS: too much is missing to transcribe.
        self.assertEqual(decider.feed(3 * FRAME + 0.9, LOUD_DB), "gap")

    def test_many_small_holes_abort_once_too_much_is_lost(self) -> None:
        # This is the 43-second case in miniature: every individual hole is
        # small enough to pad, but two thirds of the audio is missing. Better
        # to ask again than to transcribe what survived.
        decider = listener._StopDecider(no_speech_timeout=8.0)
        verdict = self.feed_all(decider, stream(0.0, "L" * 40, step=3 * FRAME))

        self.assertEqual(verdict, "gap")
        self.assertGreater(decider.lost_seconds, listener.MAX_LOST_SECONDS)

    def test_an_ordinary_turn_loses_nothing(self) -> None:
        decider = listener._StopDecider(no_speech_timeout=8.0)
        self.feed_all(decider, stream(0.0, "LLLLLLLL" + "." * 13))
        self.assertEqual(decider.lost_seconds, 0.0)
        self.assertEqual(decider.pad_seconds, 0.0)


class RecordCommandTestCase(unittest.TestCase):
    """record_command() driven off a pre-filled queue -- the exact shape that
    produced the 0.05s reading."""

    def setUp(self) -> None:
        self.audio_queue: queue.Queue = queue.Queue()
        self.stderr_queue: queue.Queue = queue.Queue()

        for name, value in (
            ("_stream_audio_queue", self.audio_queue),
            ("_stream_stderr_queue", self.stderr_queue),
            ("_capture_floor", 0.0),
            ("MAX_RECORD_SECONDS", 2.0),
        ):
            patcher = mock.patch.object(listener, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Any transcription at all is a failure in these tests: every case
        # here is one where Jarvis has nothing worth sending to Whisper.
        patcher = mock.patch.object(
            listener, "_get_model", side_effect=AssertionError("transcribed a bad turn")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, no_speech_timeout: float = 0.5):
        """Run record_command, capturing what it prints."""
        out = io.StringIO()
        started = time.perf_counter()
        with contextlib.redirect_stdout(out):
            text, reason = listener.record_command(no_speech_timeout=no_speech_timeout)
        return text, reason, time.perf_counter() - started, out.getvalue()

    def audio_seconds(self, output: str) -> float:
        """The audio half of the "[timing] Recording" line."""
        match = re.search(r"/ ([\d.]+)s audio", output)
        self.assertIsNotNone(match, output)
        return float(match.group(1))

    def fill(self, frames) -> None:
        for item in frames:
            self.audio_queue.put(item)


class StaleAudioTests(RecordCommandTestCase):
    """The 0.05s case: a recording decided entirely by audio recorded before
    the turn began."""

    def test_audio_recorded_before_the_flush_cannot_start_speech(self) -> None:
        listener._capture_floor = 5.0

        # A backlog shaped exactly like the one that broke it: the beep and
        # its echo (loud), then the pause before the user speaks (quiet).
        # 15 such frames used to be a complete recording, stopped in 8ms.
        self.fill(stream(2.0, "LL" + "." * 30))
        # Then the real turn starts, and the user says nothing.
        self.fill(stream(5.0, "." * 10))

        text, reason, _, output = self.record(no_speech_timeout=0.5)

        self.assertIsNone(text)
        self.assertEqual(reason, "no_speech")  # not "speech_end" off the beep

        # Only the fresh silence was ever recorded against. The ~2.6s of
        # backlog above would have made this a complete "recording" before.
        self.assertLess(self.audio_seconds(output), 1.0)

    def test_fresh_audio_after_the_floor_is_still_heard(self) -> None:
        # The floor must not become a deaf spot: audio recorded after it is
        # exactly what this turn is for.
        listener._capture_floor = 5.0
        self.fill(stream(2.0, "LL" + "." * 10))  # stale, skipped
        self.fill(stream(5.0, "LLLL" + "." * 14))  # the user, actually speaking

        with mock.patch.object(listener, "_get_model") as model:
            model.return_value.transcribe.return_value = ([], None)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                text, reason = listener.record_command(no_speech_timeout=0.5)

        self.assertEqual(reason, "speech_end")
        self.assertIsNone(text)  # no segments came back from the stub
        model.assert_called_once()  # but it did reach Whisper


class StarvedStreamTests(RecordCommandTestCase):
    """The 43s case: audio arriving too slowly, or not at all."""

    def test_a_stream_losing_two_thirds_of_its_audio_aborts(self) -> None:
        # 187 frames × 0.2303s = 43.07s was the observed reading: the frame
        # budget spent over a stream delivering a third of what it recorded.
        # Speech really does start here (each burst is contiguous), so this
        # is the reported case and not a silent room -- and it must not now
        # run to the budget and hand Whisper the surviving third spliced
        # together.
        frames = []
        for burst in range(8):
            frames += stream(burst * 0.5, "LLL")  # 0.24s heard, 0.26s lost
        self.fill(frames)

        text, reason, elapsed, output = self.record(no_speech_timeout=8.0)

        self.assertIsNone(text)
        self.assertEqual(reason, "gap")
        self.assertGreater(self.audio_seconds(output), 1.0)  # not "no_speech"
        self.assertLess(elapsed, 5.0)

    def test_a_dead_stream_stops_on_the_wall_clock_backstop(self) -> None:
        # Nothing arrives at all: no frame-based rule can ever fire, so the
        # wall clock is the only thing left. It used to be checked only
        # inside the queue.Empty branch, which a trickle of frames bypassed.
        listener.MAX_RECORD_SECONDS = 0.3

        text, reason, elapsed, _ = self.record(no_speech_timeout=0.2)

        self.assertIsNone(text)
        self.assertEqual(reason, "timeout")
        self.assertLess(elapsed, 3.0)

    def test_the_timing_line_reports_both_clocks(self) -> None:
        # The line that hid all of this for hours. Wall clock alone cannot
        # say whether a recording went wrong; the pair can.
        self.fill(stream(0.0, "." * 10))
        _, _, _, output = self.record(no_speech_timeout=0.5)

        self.assertRegex(output, r"\[timing\] Recording: \d+\.\d\ds wall / \d+\.\d\ds audio")


class CaptureGateTests(unittest.TestCase):
    """Whether a frame is muted is a question about when it was *recorded*."""

    def setUp(self) -> None:
        self.origin = 1000.0
        patcher = mock.patch.object(listener, "_stream_origin", self.origin)
        patcher.start()
        self.addCleanup(patcher.stop)

        speaker._intervals.clear()
        self.addCleanup(speaker._intervals.clear)
        # Jarvis was audible from 1.0s to 1.5s into the stream.
        speaker._intervals.append((self.origin + 1.0, self.origin + 1.5))

    def test_jarvis_own_voice_is_dropped_however_late_it_arrives(self) -> None:
        # The bug: this frame was recorded while Jarvis spoke, but ffmpeg
        # hands it over ~1s later, by which time is_speaking() is False and
        # any short cooldown has expired. It used to sail straight through
        # and start a "recording" off Jarvis's own reply.
        self.assertFalse(listener._should_capture(1.2))

    def test_audio_from_before_he_started_survives(self) -> None:
        # The other half of the same mistake: this frame is the user talking
        # just before Jarvis answered, and it used to be dropped for arriving
        # at an inconvenient moment.
        self.assertTrue(listener._should_capture(0.5))

    def test_the_echo_of_a_reply_is_dropped_but_not_the_next_turn(self) -> None:
        self.assertFalse(listener._should_capture(1.5 + speaker.ECHO_PAD / 2))
        self.assertTrue(listener._should_capture(1.5 + speaker.ECHO_PAD * 2))

    def test_before_an_origin_estimate_it_falls_back_to_is_speaking(self) -> None:
        listener._stream_origin = None
        with mock.patch.object(speaker, "is_speaking", return_value=True):
            self.assertFalse(listener._should_capture(1.0))
        with mock.patch.object(speaker, "is_speaking", return_value=False):
            self.assertTrue(listener._should_capture(1.0))


class SpeakingIntervalTests(unittest.TestCase):
    def setUp(self) -> None:
        speaker._intervals.clear()
        self.addCleanup(speaker._intervals.clear)

    def test_playing_records_the_interval_it_spanned(self) -> None:
        before = time.perf_counter()
        with speaker._playing():
            self.assertTrue(speaker.was_speaking(time.perf_counter()))
        after = time.perf_counter()

        self.assertEqual(len(speaker._intervals), 1)
        start, end = speaker._intervals[0]
        self.assertGreaterEqual(start, before)
        self.assertLessEqual(end, after)

        self.assertFalse(speaker.was_speaking(before - 1.0))
        self.assertTrue(speaker.was_speaking(start))

    def test_an_interval_is_recorded_even_if_playback_fails(self) -> None:
        with self.assertRaises(RuntimeError):
            with speaker._playing():
                raise RuntimeError("no audio device")

        self.assertEqual(len(speaker._intervals), 1)


class OriginEstimateTests(unittest.TestCase):
    """_stream_origin maps stream time to the wall clock it was recorded on."""

    def setUp(self) -> None:
        patcher = mock.patch.object(listener, "_stream_origin", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        listener._origin_samples.clear()
        self.addCleanup(listener._origin_samples.clear)

    def test_it_takes_the_tightest_estimate_available(self) -> None:
        # Frames arrive in bursts, so each one's arrival delay differs; the
        # true origin is the smallest (wall - stream_time) seen. A frame that
        # arrives 1s late must not push the estimate 1s late with it.
        listener._note_origin(1.0, 501.0)  # 1s of buffering
        listener._note_origin(2.0, 501.4)  # the tail of the same burst
        self.assertAlmostEqual(listener._stream_origin, 499.4)

    def test_startup_latency_corrects_itself(self) -> None:
        # DirectShow takes ~1.4s to hand over the first frame; that delay
        # must not be baked into every recorded_wall for the whole session.
        listener._note_origin(FRAME, 101.4)
        first = listener._stream_origin
        listener._note_origin(5.0, 105.05)
        self.assertLess(listener._stream_origin, first)

    def test_old_samples_leave_the_window(self) -> None:
        listener._note_origin(1.0, 500.5)
        self.assertAlmostEqual(listener._stream_origin, 499.5)

        later = listener.ORIGIN_WINDOW_SECONDS + 10.0
        listener._note_origin(later, 500.0 + later + 0.9)
        self.assertAlmostEqual(listener._stream_origin, 500.9)


class WatermarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audio_queue: queue.Queue = queue.Queue()
        for name, value in (
            ("_stream_audio_queue", self.audio_queue),
            ("_stream_stderr_queue", queue.Queue()),
            ("_capture_floor", 0.0),
        ):
            patcher = mock.patch.object(listener, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_flush_raises_the_floor_to_now(self) -> None:
        # Draining alone never worked: ffmpeg still holds the second of audio
        # that Jarvis just spoke, and hands it over immediately afterwards.
        with mock.patch.object(listener, "_stream_origin", time.perf_counter() - 30.0):
            listener.flush()
            self.assertAlmostEqual(listener._capture_floor, 30.0, delta=0.5)

    def test_flush_never_lowers_the_floor(self) -> None:
        listener._capture_floor = 99.0
        with mock.patch.object(listener, "_stream_origin", time.perf_counter() - 30.0):
            listener.flush()
        self.assertEqual(listener._capture_floor, 99.0)

    def test_flush_without_a_stream_is_harmless(self) -> None:
        with mock.patch.object(listener, "_stream_origin", None):
            listener.flush()  # must not raise
        self.assertEqual(listener._capture_floor, 0.0)


class WakeWordGapTests(unittest.TestCase):
    """openWakeWord scores each frame from the ~1.5s of audio before it, so a
    hole in the stream leaves its buffer holding two moments spliced."""

    def setUp(self) -> None:
        self.audio_queue: queue.Queue = queue.Queue()
        for name, value in (
            ("_stream_audio_queue", self.audio_queue),
            ("_stream_stderr_queue", queue.Queue()),
            ("_capture_floor", 0.0),
        ):
            patcher = mock.patch.object(listener, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.wakeword = mock.MagicMock()
        self.wakeword.detect.return_value = False
        patcher = mock.patch.object(listener, "wakeword", self.wakeword)
        patcher.start()
        self.addCleanup(patcher.stop)

        # listen_for_wake_word() flushes on entry, deliberately: a backlog
        # from the previous turn is not fresh audio. Here the queue *is* the
        # fresh audio, handed over up front, so the drain half is stubbed.
        patcher = mock.patch.object(listener, "_drain")
        patcher.start()
        self.addCleanup(patcher.stop)

        # The watermark half needs an origin to compute a floor from; with
        # none, flush() leaves _capture_floor alone and every frame below is
        # accepted on its own timestamp. The one test that cares about the
        # floor sets an origin itself.
        patcher = mock.patch.object(listener, "_stream_origin", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_wake_word(self, frames):
        for item in frames:
            self.audio_queue.put(item)
        # So a test can never hang if the wake word fails to fire.
        self.audio_queue.put(None)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            return listener.listen_for_wake_word()

    def test_a_hole_resets_the_model(self) -> None:
        cooldown = int(listener.WAKE_RETRIGGER_COOLDOWN / FRAME)
        frames = stream(0.0, "." * 20)
        frames += stream(0.0 + 20 * FRAME + 1.0, "." * (cooldown + 2))

        fire_at = len(frames) - 1
        self.wakeword.detect.side_effect = [i == fire_at for i in range(len(frames))]

        self.run_wake_word(frames)

        # Once on entry, once at the hole, once after firing.
        self.assertEqual(self.wakeword.reset.call_count, 3)

    def test_a_detection_after_a_hole_is_not_suppressed(self) -> None:
        """The inverse of what this used to pin, and deliberately so.

        A hole used to restart WAKE_RETRIGGER_COOLDOWN along with the reset.
        That cooldown exists to stop the wake word that just *fired* from
        firing again out of the model's own buffer; after a hole nothing was
        just spoken, so there is nothing to suppress — and a freshly reset
        window scores ~0.000 rather than spuriously high, so it cannot
        re-fire on its own either. Restarting it bought no protection and
        cost ~1s of deafness on top of the reset's own.
        """
        cooldown = int(listener.WAKE_RETRIGGER_COOLDOWN / FRAME)
        frames = stream(0.0, "." * 20)  # puts the entry cooldown behind us
        frames += stream(20 * FRAME + 2.0, "." * (cooldown - 2))  # after a 2s hole

        self.wakeword.detect.side_effect = [i >= 20 for i in range(len(frames))]

        self.run_wake_word(frames)  # fires; no RuntimeError from running dry

    def test_the_entry_flush_drops_leftovers_so_a_beep_hole_never_appears(self) -> None:
        """The reason listen_for_wake_word() flushes instead of draining.

        Shaped like the real thing. Returning to wake-listening after
        beep_done(), ffmpeg still holds ~1s of audio recorded *before* the
        beep; it arrives right after entry. Draining alone left the floor
        where the previous turn put it, so those stale frames were accepted
        and set last_ts — and the frames the capture gate dropped while the
        beep sounded (0.16s of beep + 0.4s of ECHO_PAD = 7 frames) then read
        as a hole, resetting the model at the exact moment the beep told the
        user to speak. Raising the floor drops the leftovers instead, so the
        first accepted frame is live audio with last_ts still None.
        """
        floor = 5.0
        with mock.patch.object(listener, "_stream_origin", time.perf_counter() - floor):
            leftovers = stream(floor - 12 * FRAME, "." * 12)  # stale: below the floor
            live = stream(floor + 7 * FRAME, "." * 20)  # after the gated beep window

            # One entry per *accepted* frame: detect() is never reached for a
            # frame below the floor, which is the whole point being pinned.
            self.wakeword.detect.side_effect = [i == len(live) - 1 for i in range(len(live))]

            out = self.run_wake_word(leftovers + live)

        self.assertEqual(self.wakeword.reset.call_count, 2)  # entry + firing only
        self.assertTrue(out)

    def test_a_contiguous_stream_is_never_reset_mid_listen(self) -> None:
        frames = stream(0.0, "." * 20)
        self.wakeword.detect.side_effect = [i == 19 for i in range(len(frames))]

        self.run_wake_word(frames)

        self.assertEqual(self.wakeword.reset.call_count, 2)  # entry + firing


class WakeWordStallTests(unittest.TestCase):
    """The wake-word wait must not hang on a stream that stopped feeding it.

    The live failure: ffmpeg alive, its stdout pipe open, and the DirectShow
    device delivering nothing. _stream_audio_reader blocked in stdout.read(),
    so the EOF sentinel never came, and the loop sat in its queue.Empty
    branch for 200+ seconds printing a heartbeat with frozen counters --
    "258 frames, 20.6s audio" unchanged, gated unchanged, evicted unchanged.

    record_command() got a wall-clock backstop when the 43-second bug was
    fixed, for exactly this reason; this wait never got one, because waiting
    indefinitely is what it is *for*. The distinction it was missing is
    between waiting for someone to speak and waiting for audio that is never
    coming, and _stream_frames_read is what tells those apart.
    """

    def setUp(self) -> None:
        self.audio_queue: queue.Queue = queue.Queue()
        for name, value in (
            ("_stream_audio_queue", self.audio_queue),
            ("_stream_stderr_queue", queue.Queue()),
            ("_capture_floor", 0.0),
            ("_stream_origin", None),
            ("_stream_frames_read", 0),
            # Short enough to test, long enough that the 0.2s queue poll
            # still runs a few times inside it.
            ("STREAM_STALL_TIMEOUT", 0.6),
        ):
            patcher = mock.patch.object(listener, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.wakeword = mock.MagicMock()
        self.wakeword.detect.return_value = False
        for name, value in (("wakeword", self.wakeword),):
            patcher = mock.patch.object(listener, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        patcher = mock.patch.object(listener, "_drain")
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            return listener.listen_for_wake_word()

    def test_a_starved_stream_raises_instead_of_hanging(self) -> None:
        # Nothing in the queue and the frame counter frozen: exactly the
        # state the live session sat in. No sentinel is ever put, so if the
        # watchdog does not fire this test hangs -- which is the bug.
        started = time.perf_counter()
        with self.assertRaises(listener.StreamStalled):
            self.wait()
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 5.0, "should give up promptly, not hang")

    def test_frames_that_never_reach_the_queue_still_count_as_alive(self) -> None:
        """The false-positive guard, and the reason the watchdog reads
        _stream_frames_read rather than the queue.

        While Jarvis is audible the capture gate drops every frame, so the
        queue is empty exactly as it is during a stall. The counter the
        reader thread advances for *every* frame it pulls off ffmpeg --
        gated or not -- is what separates the two, and only it may arm this.
        """
        alive_for = listener.STREAM_STALL_TIMEOUT * 3
        stop = threading.Event()

        def feed_the_counter() -> None:
            deadline = time.perf_counter() + alive_for
            while time.perf_counter() < deadline and not stop.is_set():
                listener._stream_frames_read += 1
                time.sleep(0.02)

        pump = threading.Thread(target=feed_the_counter, daemon=True)
        pump.start()
        self.addCleanup(stop.set)

        started = time.perf_counter()
        with self.assertRaises(listener.StreamStalled):
            self.wait()
        elapsed = time.perf_counter() - started

        # It fired only after the counter stopped moving, not while the
        # queue was empty but the stream was healthy.
        self.assertGreaterEqual(elapsed, alive_for)

    def test_the_eof_sentinel_still_raises_the_plain_error(self) -> None:
        # A process that actually ended is a different failure: no restart
        # will fix it, and main() must be able to tell the two apart.
        self.audio_queue.put(None)
        with self.assertRaises(RuntimeError) as caught:
            self.wait()
        self.assertNotIsInstance(caught.exception, listener.StreamStalled)

    def test_a_wake_word_still_fires_normally(self) -> None:
        # The watchdog must be invisible on the happy path.
        cooldown = int(listener.WAKE_RETRIGGER_COOLDOWN / FRAME)
        frames = stream(0.0, "." * (cooldown + 4))
        for item in frames:
            self.audio_queue.put(item)
        listener._stream_frames_read = len(frames)

        fire_at = len(frames) - 1
        self.wakeword.detect.side_effect = [i == fire_at for i in range(len(frames))]

        self.assertIsInstance(self.wait(), bytes)


class _FakePipe:
    """Hands over a fixed list of chunks, then EOF forever."""

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read(self, size=-1):
        return self.chunks.pop(0) if self.chunks else b""

    def readline(self):
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data):
        return len(data)

    def flush(self):
        pass


class _FakeProcess:
    def __init__(self, stdout_chunks, stderr_lines, exit_code=None):
        self.stdout = _FakePipe(stdout_chunks)
        self.stderr = _FakePipe(stderr_lines)
        self.stdin = _FakePipe([])
        self.returncode = exit_code

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class DeviceOpenTests(unittest.TestCase):
    """start_stream() must not report success for a microphone that isn't there.

    The live failure: the Razer mic dropped off the USB bus, ffmpeg started,
    printed "Could not find audio only device with name [...]" and exited -5
    (AVERROR EIO) about 200ms later. Popen had succeeded, so start_stream()
    returned normally, and every consumer then waited on a queue nothing
    would ever fill -- main() for STREAM_STALL_TIMEOUT, and tools/barge_probe
    for a whole 25-second script it then summarized as a statistics problem
    ("0 frames of clean echo") without once naming the device.

    ffmpeg had explained itself on stderr the entire time. Those lines were
    enqueued and only ever drained, so they went in the message now.
    """

    def setUp(self) -> None:
        self.addCleanup(listener.stop_stream)

    def start(self, process):
        with mock.patch.object(listener.subprocess, "Popen", return_value=process):
            listener.start_stream()

    def test_a_dead_device_raises_with_ffmpegs_own_words(self) -> None:
        process = _FakeProcess(
            stdout_chunks=[],
            stderr_lines=[
                b"[in#0] Could not find audio only device with name [Mic]"
                b" among source devices of type audio.\n",
                b"Error opening input files: I/O error\n",
            ],
            exit_code=-5,
        )

        with self.assertRaises(RuntimeError) as caught:
            self.start(process)

        message = str(caught.exception)
        self.assertIn("Could not find audio only device", message)
        self.assertIn(listener.MICROPHONE_NAME, message)

    def test_a_dead_device_leaves_no_stream_behind(self) -> None:
        # The globals are still unset when the raise happens, so the caller's
        # fallback (main.py calls stop_stream() then drops to the Enter
        # prompt) has nothing to unwind.
        with self.assertRaises(RuntimeError):
            self.start(_FakeProcess([], [], exit_code=-5))

        self.assertIsNone(listener._stream_process)
        self.assertIsNone(listener._stream_audio_queue)
        listener.stop_stream()  # must be a safe no-op

    def test_a_live_device_starts_as_before(self) -> None:
        # One real frame is all the evidence the check wants, and it returns
        # the moment that frame arrives rather than waiting out the timeout.
        process = _FakeProcess([QUIET], [], exit_code=None)

        started = time.perf_counter()
        self.start(process)
        elapsed = time.perf_counter() - started

        self.assertIs(listener._stream_process, process)
        self.assertIsNotNone(listener._stream_audio_queue)
        self.assertLess(elapsed, listener.DEVICE_OPEN_TIMEOUT)

    def test_a_slow_device_is_not_called_dead(self) -> None:
        # ffmpeg alive but silent: a device that is merely slow to start must
        # never cost wake-word mode, so this returns and leaves the case to
        # STREAM_STALL_TIMEOUT.
        with mock.patch.object(listener, "DEVICE_OPEN_TIMEOUT", 0.2):
            self.start(_FakeProcess([], [], exit_code=None))

        self.assertIsNotNone(listener._stream_process)
        self.assertEqual(listener._stream_frames_read, 0)


class StderrLineTests(unittest.TestCase):
    """ffmpeg talking about itself must not bury what it said about the device.

    Both dropped kinds were measured, not imagined. The silencedetect lines
    are the bulk of normal traffic; the startup banner is what a *tail* of
    this queue returns after a healthy stream dies, and six lines of
    "Stream #0:0: Audio: pcm_s16le ..." presented as the explanation of a
    dead microphone is worse than saying nothing.
    """

    def lines(self, *raw: str) -> list[str]:
        stderr_queue: queue.Queue = queue.Queue()
        for line in raw:
            stderr_queue.put((0.0, line + "\n"))
        stderr_queue.put(None)
        return listener._stderr_lines(stderr_queue)

    def test_silencedetect_is_dropped_and_order_is_kept(self) -> None:
        self.assertEqual(
            self.lines(
                "[in#0] Could not find audio only device with name [Mic]",
                "[silencedetect @ 0x1] silence_start: 1.234",
                "Error opening input files: I/O error",
            ),
            [
                "[in#0] Could not find audio only device with name [Mic]",
                "Error opening input files: I/O error",
            ],
        )

    def test_the_startup_banner_is_dropped(self) -> None:
        # Verbatim from a live run, which is the whole point: a made-up
        # banner would pin a filter against text ffmpeg never prints.
        self.assertEqual(
            self.lines(
                "[aist#0:0/pcm_s16le @ 0x1] Guessed Channel Layout: stereo",
                "Input #0, dshow, from 'audio=Mic':",
                "Duration: N/A, start: 7914.601000, bitrate: 1411 kb/s",
                "Stream #0:0: Audio: pcm_s16le, 44100 Hz, stereo, s16",
                "Stream mapping:",
                "Press [q] to stop, [?] for help",
                "Output #0, s16le, to 'pipe:1':",
                "Metadata:",
                "encoder         : Lavf63.1.101",
                "[q] command received. Exiting.",
                "size=      31KiB time=00:00:01.00 bitrate= 256.0kbits/s",
            ),
            [],
        )

    def test_an_unrecognised_failure_survives(self) -> None:
        # The safe direction: anything that isn't known boilerplate is kept,
        # so a message nobody anticipated still reaches the user instead of
        # being reduced to an exit code.
        self.assertEqual(
            self.lines("Something entirely new went wrong"),
            ["Something entirely new went wrong"],
        )


if __name__ == "__main__":
    unittest.main()
