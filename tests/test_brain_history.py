"""Tests for jarvis/brain.py's history when the provider call fails.

ask() appends the user message to _history *before* calling the provider, so
a raising provider used to leave that turn behind with no assistant reply.
One failure was harmless; Ollama's intermittent CUDA error is not a single
failure, and a run of them stacked consecutive user messages that the next
successful call read as one run-on question.

Nothing here reaches Ollama: the provider is replaced in _PROVIDERS, which is
also what keeps these tests honest about _history being module state -- each
one restores what it appended.
"""

from __future__ import annotations

import unittest
from unittest import mock

from jarvis import brain


class BrainHistoryOnErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        # _history is module-level and shared; snapshot and restore it so the
        # order these tests run in can never matter.
        self._snapshot = list(brain._history)
        self.addCleanup(lambda: brain._history.__setitem__(slice(None), self._snapshot))

    def _provider(self, **kwargs):
        return mock.patch.dict(brain._PROVIDERS, {brain.BRAIN_PROVIDER: mock.Mock(**kwargs)})

    def test_failed_turn_leaves_history_untouched(self):
        before = list(brain._history)

        with self._provider(side_effect=RuntimeError("CUDA error: out of memory")):
            with self.assertRaises(RuntimeError):
                brain.ask("τι ώρα είναι")

        self.assertEqual(brain._history, before)

    def test_error_still_reaches_the_caller(self):
        """The pop must not swallow the error: main._answer() reports it."""
        with self._provider(side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                brain.ask("γεια")

    def test_failures_do_not_stack_user_turns(self):
        with self._provider(side_effect=RuntimeError("CUDA error")):
            for _ in range(3):
                with self.assertRaises(RuntimeError):
                    brain.ask("γεια")

        with self._provider(return_value="Γεια σου."):
            self.assertEqual(brain.ask("γεια σου"), "Γεια σου.")

        # One exchange, not four: the three failures left nothing behind.
        self.assertEqual(
            [m["role"] for m in brain._history[brain._PREFIX_LEN:]],
            ["user", "assistant"],
        )

    def test_memory_block_failure_also_pops(self):
        """The memory_block path builds `messages` as a new list rather than
        aliasing _history, so the pop has to target _history itself."""
        before = list(brain._history)

        with self._provider(side_effect=RuntimeError("CUDA error")):
            with self.assertRaises(RuntimeError):
                brain.ask("τι έχω σήμερα", "Το όνομά σου είναι Γιάννης.")

        self.assertEqual(brain._history, before)


if __name__ == "__main__":
    unittest.main()
