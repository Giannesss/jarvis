"""Tests for jarvis/player.py, the cancellable playback engine.

This module exists because the old one could not be stopped: winsound refuses
SND_MEMORY|SND_ASYNC outright ("Cannot play asynchronously from memory"), so a
reply handed to it was going to finish whatever the user did. Everything below
pins the three properties barge-in actually needs from the replacement:

  * a reply split into several synthesized chunks plays as one continuous
    stream, with no seam and no reordering;
  * abort() stops it and says how much was handed to the device, which is what
    the conversation history gets committed from;
  * a chunk that arrives late produces a *counted* underrun, not a silent one.

Nothing here opens an audio device. `sounddevice` is replaced with a fake in
sys.modules, and the callback -- which is the whole of the module's logic -- is
driven by hand, one block at a time, the way tests/test_record_timing.py drives
_StopDecider from a list.
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from jarvis import player


class _CallbackStop(Exception):
    pass


class _CallbackAbort(Exception):
    pass


class _FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.aborted = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        self.closed = True


class _FakeSounddevice:
    CallbackStop = _CallbackStop
    CallbackAbort = _CallbackAbort

    DEVICES = [
        {"name": "Microsoft Sound Mapper - Output", "max_output_channels": 2},
        {"name": "Μικρόφωνο (Razer Seiren Mini)", "max_output_channels": 0},
        {"name": "Realtek Digital Output (Realtek(R) Audio)", "max_output_channels": 2},
    ]

    def __init__(self):
        self.streams: list[_FakeStream] = []

    def RawOutputStream(self, **kwargs) -> _FakeStream:  # noqa: N802 (mirrors sd)
        stream = _FakeStream(**kwargs)
        self.streams.append(stream)
        return stream

    def query_devices(self):
        return list(self.DEVICES)


class PlayerTestCase(unittest.TestCase):
    """Every test gets a started Player backed by a fake device."""

    SAMPLERATE = 22050
    BLOCK = 4  # frames per callback; small enough to reason about by hand

    def setUp(self) -> None:
        self.sd = _FakeSounddevice()
        patcher = mock.patch.dict(sys.modules, {"sounddevice": self.sd})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _player(self) -> player.Player:
        sink = player.Player(self.SAMPLERATE, device=None, blocksize=self.BLOCK)
        sink.start()
        self.addCleanup(sink.close)
        return sink

    def _pull(self, sink: player.Player, blocks: int = 1) -> bytes:
        """Run the callback `blocks` times and return what the device got."""
        out = bytearray()
        for _ in range(blocks):
            buffer = bytearray(self.BLOCK * player.BYTES_PER_SAMPLE)
            sink._callback(buffer, self.BLOCK, None, None)
            out += buffer
        return bytes(out)


def _tone(byte: int, frames: int) -> bytes:
    """`frames` frames of a recognisable constant, so order is checkable."""
    return bytes([byte, 0]) * frames


class ContinuityTests(PlayerTestCase):
    def test_chunks_play_back_to_back_with_no_seam(self) -> None:
        # The reason for one stream instead of one PlaySound per sentence: a
        # reply arrives as several separately synthesized pieces and must not
        # sound like it.
        sink = self._player()
        sink.write(_tone(1, 4))
        sink.write(_tone(2, 4))

        self.assertEqual(self._pull(sink, blocks=2), _tone(1, 4) + _tone(2, 4))

    def test_a_chunk_is_split_across_blocks_without_losing_bytes(self) -> None:
        # Chunk boundaries and block boundaries have no reason to line up.
        sink = self._player()
        sink.write(_tone(7, 10))

        played = self._pull(sink, blocks=3)
        self.assertEqual(played[: 10 * 2], _tone(7, 10))
        self.assertEqual(sink.played_seconds, 10 / self.SAMPLERATE)

    def test_the_block_that_drains_the_buffer_is_still_played(self) -> None:
        # CallbackStop means "stop calling me, but play what is pending"
        # (sounddevice's own words), so raising it on the block that exactly
        # empties the buffer is both correct and the earliest it can be
        # raised. Raising it a block later would append a block of silence to
        # every reply; not raising it at all would leave the stream open and
        # count an underrun for a sentence that simply ended.
        sink = self._player()
        sink.write(_tone(3, 4))
        sink.done_writing()

        buffer = bytearray(self.BLOCK * player.BYTES_PER_SAMPLE)
        with self.assertRaises(_CallbackStop):
            sink._callback(buffer, self.BLOCK, None, None)

        self.assertEqual(bytes(buffer), _tone(3, 4))
        self.assertEqual(sink.underruns, 0)

    def test_a_partly_filled_final_block_is_padded_and_ends_the_stream(self) -> None:
        sink = self._player()
        sink.write(_tone(3, 2))
        sink.done_writing()

        buffer = bytearray(self.BLOCK * player.BYTES_PER_SAMPLE)
        with self.assertRaises(_CallbackStop):
            sink._callback(buffer, self.BLOCK, None, None)

        self.assertEqual(bytes(buffer), _tone(3, 2) + b"\x00\x00" * 2)
        self.assertEqual(sink.underruns, 0)


class UnderrunTests(PlayerTestCase):
    def test_starving_the_callback_counts_an_underrun(self) -> None:
        # Synthesis falling behind playback is audible as a hole in the middle
        # of a sentence, and is otherwise indistinguishable from a pause.
        sink = self._player()
        sink.write(_tone(5, 2))

        played = self._pull(sink)
        self.assertEqual(played, _tone(5, 2) + b"\x00\x00" * 2)
        self.assertEqual(sink.underruns, 1)

    def test_a_drained_finished_stream_is_not_an_underrun(self) -> None:
        sink = self._player()
        sink.write(_tone(5, 2))
        sink.done_writing()

        with self.assertRaises(_CallbackStop):
            self._pull(sink)
        self.assertEqual(sink.underruns, 0)

    def test_late_audio_still_plays_after_an_underrun(self) -> None:
        # An underrun is a gap, not an end: the stream stays open so the rest
        # of the reply can still arrive.
        sink = self._player()
        sink.write(_tone(5, 2))
        self._pull(sink)

        sink.write(_tone(6, 4))
        self.assertEqual(self._pull(sink), _tone(6, 4))


class AbortTests(PlayerTestCase):
    def test_abort_stops_the_device_and_drops_queued_audio(self) -> None:
        sink = self._player()
        sink.write(_tone(1, 4))
        sink.write(_tone(2, 400))  # a long tail nobody will hear

        self._pull(sink)
        sink.abort()

        self.assertTrue(self.sd.streams[0].aborted)
        self.assertTrue(sink.aborted)
        self.assertTrue(sink.wait(timeout=0))
        # Only what actually reached the device is credited.
        self.assertEqual(sink.played_seconds, 4 / self.SAMPLERATE)

    def test_abort_is_idempotent(self) -> None:
        # Barge-in and a reply ending on its own can race; the loser must not
        # raise.
        sink = self._player()
        sink.abort()
        sink.abort()
        self.assertTrue(sink.aborted)

    def test_write_after_abort_is_ignored(self) -> None:
        # The synthesis worker can be one chunk ahead when barge-in lands.
        sink = self._player()
        sink.abort()
        sink.write(_tone(9, 4))

        self.assertEqual(self._pull(sink), b"\x00\x00" * 4)

    def test_abort_survives_a_stream_that_raises(self) -> None:
        sink = self._player()
        self.sd.streams[0].abort = mock.Mock(side_effect=OSError("device gone"))

        sink.abort()  # must not propagate: the reply is over either way
        self.assertTrue(sink.aborted)


class DeviceResolutionTests(PlayerTestCase):
    def test_empty_means_portaudio_default(self) -> None:
        self.assertIsNone(player._resolve_device(""))
        self.assertIsNone(player._resolve_device("   "))

    def test_a_number_is_an_index(self) -> None:
        self.assertEqual(player._resolve_device("3"), 3)

    def test_a_name_is_matched_case_insensitively_among_outputs(self) -> None:
        self.assertEqual(player._resolve_device("realtek"), 2)

    def test_an_input_only_device_never_matches(self) -> None:
        # The mic's name is in the list too, and picking it would open a
        # stream that can never make a sound.
        with self.assertRaises(player.PlaybackUnavailable):
            player._resolve_device("Razer")

    def test_an_unknown_name_is_reported_not_guessed(self) -> None:
        with self.assertRaises(player.PlaybackUnavailable):
            player._resolve_device("Sonos")


class StartupFailureTests(PlayerTestCase):
    def test_a_device_that_will_not_open_raises_playback_unavailable(self) -> None:
        # speaker.py catches exactly this to fall back to winsound, so the
        # type matters more than the message.
        self.sd.RawOutputStream = mock.Mock(side_effect=OSError("no such device"))
        sink = player.Player(self.SAMPLERATE, device=None, blocksize=self.BLOCK)

        with self.assertRaises(player.PlaybackUnavailable):
            sink.start()


if __name__ == "__main__":
    unittest.main()
