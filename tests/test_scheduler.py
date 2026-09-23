"""Tests for jarvis/scheduler.py: reminders and timers that survive a restart.

The `reminders` table carried due_at/status/fired_at from the day it was
written and nothing ever fired them. These tests pin the four properties that
make the firing safe rather than merely present:

  * a row fires exactly once, even under a second claimant;
  * a future-dated row is never touched -- the roadmap's "never silently
    drops a future-dated exam or deadline";
  * a freeze stops the clock without eating it;
  * nothing missed while Jarvis was off is dropped, whether or not it was
    read out loud.

Every test drives tick() and catch_up() synchronously against a throwaway
database with an injected `now`, so nothing here starts a thread, sleeps, or
needs a microphone. The announcer is a list.append.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from jarvis import db, policy, scheduler


class SchedulerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "jarvis.db"

        patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)

        self.said: list[str] = []
        scheduler.set_announcer(self.said.append)
        self.addCleanup(scheduler.set_announcer, None)

        # policy's frozen flag is module state, like in test_policy.py.
        policy._frozen = False
        self.addCleanup(lambda: setattr(policy, "_frozen", False))

        self.now = datetime(2026, 9, 23, 18, 0, 0)

    def add(self, text: str, offset_seconds: int, kind: str = scheduler.REMINDER) -> int:
        """A row due `offset_seconds` from self.now (negative = overdue)."""
        return scheduler.schedule(
            text,
            self.now + timedelta(seconds=offset_seconds),
            kind=kind,
            conn=self.conn,
        )

    def row(self, row_id: int):
        return self.conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (row_id,)
        ).fetchone()


class FiringTests(SchedulerTestCase):
    def test_a_due_reminder_is_announced_and_marked(self) -> None:
        row_id = self.add("να πάρω ψωμί", -10)

        self.assertEqual(
            scheduler.tick(self.now, self.conn), ["Υπενθύμιση: να πάρω ψωμί"]
        )
        self.assertEqual(self.said, ["Υπενθύμιση: να πάρω ψωμί"])

        row = self.row(row_id)
        self.assertEqual(row["status"], scheduler.FIRED)
        self.assertIsNotNone(row["fired_at"])

    def test_a_reminder_fires_exactly_once(self) -> None:
        """The claim is what guarantees this, not the announcement: ticking
        again must find nothing left to take."""
        self.add("να πάρω ψωμί", -10)

        scheduler.tick(self.now, self.conn)
        scheduler.tick(self.now, self.conn)
        scheduler.tick(self.now + timedelta(hours=1), self.conn)

        self.assertEqual(len(self.said), 1)

    def test_a_claim_lost_to_another_process_announces_nothing(self) -> None:
        """Two Jarvis processes against one database is a shape WAL mode
        deliberately allows. The UPDATE is guarded on status = 'pending', so
        the loser of the race says nothing rather than repeating it."""
        row_id = self.add("να πάρω ψωμί", -10)

        # Someone else got there between the SELECT and the UPDATE.
        other = db.connect(self.db_path)
        self.addCleanup(other.close)
        self.assertTrue(scheduler._claim(other, row_id, scheduler.FIRED))

        self.assertEqual(scheduler.tick(self.now, self.conn), [])
        self.assertEqual(self.said, [])

    def test_a_timer_says_its_own_sentence(self) -> None:
        """A timer's text is already the whole announcement, so it is not
        wrapped in "Υπενθύμιση:" the way a reminder is."""
        self.add("Το χρονόμετρο των 2 λεπτά τελείωσε!", -1, kind=scheduler.TIMER)

        self.assertEqual(
            scheduler.tick(self.now, self.conn),
            ["Το χρονόμετρο των 2 λεπτά τελείωσε!"],
        )

    def test_a_failing_announcer_does_not_stop_the_scheduler(self) -> None:
        self.add("να πάρω ψωμί", -10)
        scheduler.set_announcer(mock.Mock(side_effect=RuntimeError("no audio")))

        # The row is still claimed: the failure is in the speaking, and
        # re-announcing it every tick forever is the worse outcome.
        self.assertEqual(len(scheduler.tick(self.now, self.conn)), 1)


class FutureRowTests(SchedulerTestCase):
    def test_a_future_reminder_is_left_alone(self) -> None:
        """The roadmap's "never silently drops a future-dated exam or
        deadline": the sweep filters on due_at <= now, so tomorrow's row is
        still pending afterwards and still there to fire tomorrow."""
        row_id = self.add("η εξέταση στα μαθηματικά", 86400)

        scheduler.tick(self.now, self.conn)
        scheduler.catch_up(self.now, self.conn)

        row = self.row(row_id)
        self.assertEqual(row["status"], scheduler.PENDING)
        self.assertIsNone(row["fired_at"])
        self.assertEqual(self.said, [])

    def test_it_fires_once_its_time_comes(self) -> None:
        self.add("η εξέταση στα μαθηματικά", 86400)

        self.assertEqual(scheduler.tick(self.now, self.conn), [])
        self.assertEqual(len(scheduler.tick(self.now + timedelta(days=1), self.conn)), 1)

    def test_the_boundary_is_inclusive(self) -> None:
        # due_at == now is due. Pinned because a strict < would make a
        # reminder land in the gap between two ticks.
        self.add("τώρα", 0)
        self.assertEqual(len(scheduler.tick(self.now, self.conn)), 1)


class FrozenTests(SchedulerTestCase):
    def test_a_freeze_announces_nothing(self) -> None:
        self.add("να πάρω ψωμί", -10)
        policy.freeze("voice", self.conn)

        self.assertEqual(scheduler.tick(self.now, self.conn), [])
        self.assertEqual(self.said, [])

    def test_a_freeze_does_not_consume_the_reminder(self) -> None:
        """The important half. Claiming while frozen would mark it fired with
        nothing said, so the kill switch would quietly destroy data."""
        row_id = self.add("να πάρω ψωμί", -10)
        policy.freeze("voice", self.conn)
        scheduler.tick(self.now, self.conn)

        self.assertEqual(self.row(row_id)["status"], scheduler.PENDING)

        policy.thaw("cli", self.conn)
        self.assertEqual(len(scheduler.tick(self.now, self.conn)), 1)


class CatchUpTests(SchedulerTestCase):
    def test_nothing_missed_says_nothing(self) -> None:
        self.add("αύριο", 86400)
        self.assertIsNone(scheduler.catch_up(self.now, self.conn))
        self.assertEqual(self.said, [])

    def test_missed_reminders_are_reported_and_marked_missed(self) -> None:
        row_id = self.add("να πάρω ψωμί", -3600)

        message = scheduler.catch_up(self.now, self.conn)

        self.assertIn("Όσο ήμουν κλειστός", message)
        self.assertIn("να πάρω ψωμί", message)
        self.assertEqual(self.row(row_id)["status"], scheduler.MISSED)

    def test_missed_is_distinct_from_fired(self) -> None:
        """So `:mem list reminders` can still tell "you were told this" from
        "this went by while Jarvis was off"."""
        missed = self.add("χθεσινό", -3600)
        scheduler.catch_up(self.now, self.conn)

        due = self.add("τωρινό", -1)
        scheduler.tick(self.now, self.conn)

        self.assertEqual(self.row(missed)["status"], scheduler.MISSED)
        self.assertEqual(self.row(due)["status"], scheduler.FIRED)

    def test_everything_past_the_limit_is_counted_not_dropped(self) -> None:
        """The bound is on how much is read aloud, never on how much is
        reported: the count covers the rest and every row stays in the table."""
        ids = [self.add(f"υπενθύμιση {i}", -60 * (i + 1)) for i in range(5)]

        message = scheduler.catch_up(self.now, self.conn)

        self.assertIn("και άλλες 2 υπενθυμίσεις", message)
        for row_id in ids:
            self.assertEqual(self.row(row_id)["status"], scheduler.MISSED)

    def test_the_newest_missed_are_the_ones_read_out(self) -> None:
        self.add("παλιό", -7200)
        self.add("πρόσφατο", -60)

        with mock.patch.object(scheduler, "SCHEDULER_CATCHUP_LIMIT", 1):
            message = scheduler.catch_up(self.now, self.conn)

        self.assertIn("πρόσφατο", message)
        self.assertNotIn("παλιό", message)
        self.assertIn("και άλλη μία υπενθύμιση", message)

    def test_a_missed_timer_is_counted_rather_than_replayed(self) -> None:
        """A countdown from yesterday has no content worth hearing again, but
        it is still reported -- counted, never simply dropped."""
        self.add("Το χρονόμετρο των 2 λεπτά τελείωσε!", -7200, kind=scheduler.TIMER)

        message = scheduler.catch_up(self.now, self.conn)

        self.assertEqual(message, "Όσο ήμουν κλειστός έληξε ένα χρονόμετρο.")
        self.assertNotIn("χρονόμετρο των 2", message)

    def test_missed_timers_are_counted_alongside_reminders(self) -> None:
        self.add("να πάρω ψωμί", -60)
        self.add("Το χρονόμετρο τελείωσε!", -70, kind=scheduler.TIMER)
        self.add("Το χρονόμετρο τελείωσε!", -80, kind=scheduler.TIMER)

        message = scheduler.catch_up(self.now, self.conn)

        self.assertIn("να πάρω ψωμί", message)
        self.assertIn("Έληξαν επίσης 2 χρονόμετρα.", message)

    def test_a_restart_does_not_replay_what_was_already_caught_up(self) -> None:
        self.add("να πάρω ψωμί", -3600)

        scheduler.catch_up(self.now, self.conn)
        self.assertIsNone(scheduler.catch_up(self.now, self.conn))
        self.assertEqual(len(self.said), 1)


class DurabilityTests(SchedulerTestCase):
    def test_a_row_outlives_the_connection_that_wrote_it(self) -> None:
        """The whole point of the step: a countdown set before a restart is
        still there after one."""
        self.add("Το χρονόμετρο τελείωσε!", 60, kind=scheduler.TIMER)
        self.conn.close()

        reopened = db.connect(self.db_path)
        self.addCleanup(reopened.close)

        self.assertEqual(
            len(scheduler.tick(self.now + timedelta(seconds=61), reopened)), 1
        )

    def test_schedule_stores_the_text_verbatim_and_norm_folded(self) -> None:
        row_id = scheduler.schedule(
            "Να πάρω ΨΩΜΙ", self.now, conn=self.conn
        )
        row = self.row(row_id)

        self.assertEqual(row["text"], "Να πάρω ΨΩΜΙ")
        self.assertEqual(row["norm"], "να παρω ψωμι")
        self.assertEqual(row["status"], scheduler.PENDING)
        self.assertEqual(row["kind"], scheduler.REMINDER)


if __name__ == "__main__":
    unittest.main()
