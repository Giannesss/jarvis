"""Tests for brain.start_turn: streamed deltas, and the history that follows.

The streaming is the easy half. The half worth pinning is the bookkeeping: a
streamed reply can be cut off mid-word, so what the model generated and what
the user heard are two different strings, and only the second belongs in the
history. Committing the generated text instead would have the model believe it
said things nobody heard -- and then answer follow-ups as though it had.

The rule this file exists to hold:

  * a full reply commits what was said;
  * an interrupted one commits the part that was heard;
  * nothing heard at all pops the user's message too, because a question
    answered by silence is, to the conversation, a question never asked. That
    is the same reasoning ask()'s except clause already followed -- an
    unanswered user turn left in history stacks up and the next successful
    call reads the pile as one run-on question.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from jarvis import brain


def history_tail(count: int) -> list[tuple[str, str]]:
    """The last `count` (role, content) pairs, for readable assertions."""
    return [
        (message["role"], message["content"]) for message in brain._history[-count:]
    ]


class BrainStreamTestCase(unittest.TestCase):
    def setUp(self) -> None:
        before = list(brain._history)
        self.addCleanup(brain._history.__setitem__, slice(None), before)

    @staticmethod
    def deltas(*parts: str, done_reason: str = "stop"):
        """A streaming provider that yields `parts`."""

        def provider(messages, state):
            provider.messages = list(messages)
            for part in parts:
                yield part
            state["done_reason"] = done_reason

        provider.messages = []
        return provider

    @contextlib.contextmanager
    def provider(self, streamer):
        with mock.patch.dict(brain._STREAM_PROVIDERS, {"ollama": streamer}):
            yield streamer

    def drain(self, turn: brain.Turn) -> str:
        return "".join(turn.deltas())


class StreamingTests(BrainStreamTestCase):
    def test_deltas_arrive_in_order_and_accumulate(self) -> None:
        with self.provider(self.deltas("Γεια", " σου", "!")):
            turn = brain.start_turn("γεια")
            pieces = list(turn.deltas())

        self.assertEqual(pieces, ["Γεια", " σου", "!"])
        self.assertEqual(turn.generated, "Γεια σου!")

    def test_the_user_message_is_in_the_prompt_before_a_single_delta(self) -> None:
        streamer = self.deltas("ok")
        with self.provider(streamer):
            turn = brain.start_turn("τι κάνεις;")
            self.drain(turn)
            turn.commit("ok")

        self.assertEqual(streamer.messages[-1], {"role": "user", "content": "τι κάνεις;"})

    def test_the_memory_block_is_injected_after_the_frozen_prefix(self) -> None:
        # Same contract as ask(): transient, never appended to history, so a
        # stale recall cannot pile up and the trim cannot cut one in half.
        streamer = self.deltas("ok")
        with self.provider(streamer):
            turn = brain.start_turn("γεια", "Προφίλ: όνομα=γιαννησ")
            self.drain(turn)
            turn.commit("ok")

        injected = streamer.messages[brain._PREFIX_LEN]
        self.assertEqual(injected["role"], "system")
        self.assertIn("γιαννησ", injected["content"])
        self.assertNotIn(
            "Προφίλ: όνομα=γιαννησ",
            [message["content"] for message in brain._history],
        )

    def test_truncated_is_reported_from_the_final_chunk(self) -> None:
        with self.provider(self.deltas("μισή πρότ", done_reason="length")):
            turn = brain.start_turn("γεια")
            self.drain(turn)
            self.assertTrue(turn.truncated)
            turn.commit("μισή πρότ")

        with self.provider(self.deltas("όλη η πρόταση.")):
            turn = brain.start_turn("γεια")
            self.drain(turn)
            self.assertFalse(turn.truncated)
            turn.commit("όλη η πρόταση.")


class HistoryTests(BrainStreamTestCase):
    def test_a_finished_reply_commits_what_was_said(self) -> None:
        with self.provider(self.deltas("Καλά", " εσύ;")):
            turn = brain.start_turn("τι κάνεις;")
            self.drain(turn)
            turn.commit("Καλά εσύ;")

        self.assertEqual(
            history_tail(2),
            [("user", "τι κάνεις;"), ("assistant", "Καλά εσύ;")],
        )

    def test_an_interrupted_reply_commits_only_what_was_heard(self) -> None:
        with self.provider(self.deltas("Πρώτη πρόταση.", " Δεύτερη πρόταση.")):
            turn = brain.start_turn("πες μου δύο πράγματα")
            self.drain(turn)
            turn.commit("Πρώτη πρόταση.")

        self.assertEqual(
            history_tail(2),
            [("user", "πες μου δύο πράγματα"), ("assistant", "Πρώτη πρόταση.")],
        )

    def test_nothing_heard_drops_the_turn_entirely(self) -> None:
        length = len(brain._history)
        with self.provider(self.deltas("Μια απάντηση που δεν ακούστηκε.")):
            turn = brain.start_turn("κάτι")
            self.drain(turn)
            turn.commit("")

        self.assertEqual(len(brain._history), length)
        self.assertNotIn("κάτι", [message["content"] for message in brain._history])

    def test_abandon_drops_the_turn(self) -> None:
        length = len(brain._history)
        with self.provider(self.deltas("ό,τι να ναι")):
            turn = brain.start_turn("κάτι")
            turn.abandon()

        self.assertEqual(len(brain._history), length)

    def test_settling_twice_changes_nothing(self) -> None:
        # deltas() abandons on its own error and main.py abandons again in its
        # handler; the second call must be a no-op rather than eating a message.
        with self.provider(self.deltas("ok")):
            turn = brain.start_turn("κάτι")
            self.drain(turn)
            turn.commit("ok")
            length = len(brain._history)
            turn.commit("ok ξανά")
            turn.abandon()

        self.assertEqual(len(brain._history), length)
        self.assertEqual(history_tail(1), [("assistant", "ok")])

    def test_a_failure_mid_stream_leaves_no_unanswered_question(self) -> None:
        def explode(messages, state):
            yield "Αρχή"
            raise RuntimeError("ollama died")

        length = len(brain._history)
        with self.provider(explode):
            turn = brain.start_turn("κάτι")
            with self.assertRaises(RuntimeError):
                self.drain(turn)

        self.assertEqual(len(brain._history), length)

    def test_history_stays_bounded(self) -> None:
        with self.provider(self.deltas("ok")):
            for index in range(10):
                turn = brain.start_turn(f"ερώτηση {index}")
                self.drain(turn)
                turn.commit("ok")

        after_prefix = brain._history[brain._PREFIX_LEN:]
        self.assertLessEqual(len(after_prefix), brain.MAX_HISTORY_MESSAGES)
        # The frozen prefix (system prompt + few-shot examples) is never cut.
        self.assertEqual(brain._history[0]["role"], "system")
        self.assertEqual(len(brain._history) - len(after_prefix), brain._PREFIX_LEN)


class OllamaProviderTests(BrainStreamTestCase):
    """The one test that drives the real _stream_ollama, against a fake client."""

    def test_it_yields_content_and_reports_how_it_ended(self) -> None:
        chunks = [
            {"message": {"content": "Γεια"}},
            {"message": {"content": " σου"}},
            {"message": {"content": ""}, "done": True, "done_reason": "length",
             "prompt_eval_count": 120, "eval_count": 8},
        ]

        state: dict = {}
        with mock.patch.object(brain, "ollama") as client:
            client.chat.return_value = iter(chunks)
            with contextlib.redirect_stdout(io.StringIO()):
                pieces = list(brain._stream_ollama([{"role": "user", "content": "γεια"}], state))

        self.assertEqual(pieces, ["Γεια", " σου"])
        self.assertEqual(state["done_reason"], "length")
        # stream=True is the whole point; without it the call blocks until the
        # reply is finished and there is nothing to chunk.
        self.assertTrue(client.chat.call_args.kwargs["stream"])
        self.assertEqual(
            client.chat.call_args.kwargs["options"]["num_predict"], brain.MAX_REPLY_TOKENS
        )

    def test_a_stream_with_no_tokens_still_reports_a_stop_reason(self) -> None:
        state: dict = {}
        with mock.patch.object(brain, "ollama") as client:
            client.chat.return_value = iter([{"message": {"content": ""}, "done": True,
                                              "done_reason": "stop"}])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(list(brain._stream_ollama([], state)), [])

        self.assertEqual(state["done_reason"], "stop")


if __name__ == "__main__":
    unittest.main()
