"""Tests for jarvis/diag.py, the diagnostic log.

The log exists because the lines that diagnose Jarvis's timing bugs kept
being lost with the terminal scrollback. Two properties matter more than the
formatting and are what these tests pin: that it never raises into a turn,
and that it stays silent until a real session opens it -- which is what
keeps the test suite from writing fake timings into the user's own log.

Nothing here touches the real LOG_PATH: every test redirects it into a temp
directory.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from jarvis import diag


class DiagTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "jarvis.log"

        for name, value in (("LOG_PATH", self.path), ("LOG_ENABLED", True),
                            ("LOG_KEEP", 3), ("LOG_MAX_BYTES", 2 * 1024 * 1024)):
            patcher = mock.patch.object(diag, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Module state, restored so test order cannot matter.
        patcher = mock.patch.object(diag, "_disabled", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _lines(self) -> list[str]:
        return self.path.read_text(encoding="utf-8").splitlines()

    # --- staying silent until a session opens the log

    def test_nothing_is_written_before_start_session(self):
        """Importing jarvis.* must not create or touch the log. This is the
        whole reason _disabled starts True: the test suite imports speaker,
        skills and listener, and their [timing] lines are not sessions."""
        with redirect_stdout(io.StringIO()) as out:
            diag.log("[timing] Piper load: 0.00s")

        self.assertFalse(self.path.exists())
        self.assertEqual(out.getvalue(), "[timing] Piper load: 0.00s\n")

    def test_start_session_opens_the_log_and_marks_the_run(self):
        diag.start_session("session start (wake word True)")
        with redirect_stdout(io.StringIO()):
            diag.log("[timing] Recording: 4.24s wall / 3.04s audio")

        lines = self._lines()
        self.assertIn("--- session start (wake word True) ---", lines[0])
        self.assertTrue(lines[1].endswith("[timing] Recording: 4.24s wall / 3.04s audio"))

    def test_start_session_respects_log_enabled(self):
        with mock.patch.object(diag, "LOG_ENABLED", False):
            diag.start_session("session start")
            with redirect_stdout(io.StringIO()):
                diag.log("[timing] Recording: 1.36s wall / 1.36s audio")

        self.assertFalse(self.path.exists())

    # --- the line itself

    def test_log_prints_and_records_the_same_line(self):
        """log() is a drop-in for the print() it replaced: the terminal must
        see exactly what it saw before, unstamped."""
        diag.start_session("session start")
        message = "[rec] stopped: speech_end after 38 frames"

        with redirect_stdout(io.StringIO()) as out:
            diag.log(message)

        self.assertEqual(out.getvalue(), message + "\n")
        self.assertTrue(self._lines()[-1].endswith(message))

    def test_every_line_is_timestamped(self):
        diag.start_session("session start")
        with redirect_stdout(io.StringIO()):
            diag.log("[wake] fired after 1.20s")

        # "2026-09-23 21:47:29 [wake] ..." -- the date is the point: a log
        # spanning restarts is only useful if a line can be placed in time.
        stamp = self._lines()[-1].split(" [")[0]
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_write_records_without_printing(self):
        diag.start_session("session start")
        with redirect_stdout(io.StringIO()) as out:
            diag.write("[rec] quiet note")

        self.assertEqual(out.getvalue(), "")
        self.assertTrue(self._lines()[-1].endswith("[rec] quiet note"))

    # --- rotation

    def test_rotates_at_the_cap_and_keeps_log_keep_files(self):
        diag.start_session("session start")

        with mock.patch.object(diag, "LOG_MAX_BYTES", 200):
            with redirect_stdout(io.StringIO()):
                for i in range(120):
                    diag.log(f"[timing] Recording: {i}.00s wall / {i}.00s audio")

        kept = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(kept, ["jarvis.log", "jarvis.log.1", "jarvis.log.2", "jarvis.log.3"])

        # The live file holds the newest line; nothing older than .3 survives.
        self.assertTrue(self._lines()[-1].endswith("119.00s wall / 119.00s audio"))
        self.assertFalse((self.root / "jarvis.log.4").exists())

    # --- never costing a turn

    def test_a_failing_write_never_raises(self):
        """A full disk or a locked file loses a log line, never a reply --
        the same discipline as db.backup() and memory.recall_safe()."""
        diag.start_session("session start")

        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            with redirect_stdout(io.StringIO()) as out:
                diag.log("[timing] Recording: 4.24s wall / 3.04s audio")

        # The terminal still got it; only the file lost it.
        self.assertIn("[timing] Recording", out.getvalue())

    def test_a_failed_write_stops_further_attempts(self):
        """Otherwise a broken path costs a failed syscall per frame for the
        rest of the run."""
        diag.start_session("session start")

        opener = mock.Mock(side_effect=OSError("disk full"))
        with mock.patch("builtins.open", opener):
            with redirect_stdout(io.StringIO()):
                for _ in range(5):
                    diag.log("[wake] hit")

        self.assertEqual(opener.call_count, 1)
        self.assertTrue(diag._disabled)


if __name__ == "__main__":
    unittest.main()
