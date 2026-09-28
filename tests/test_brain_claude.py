"""Tests for jarvis/brain.py's Claude provider.

test_brain_history.py and test_brain_stream.py patch _PROVIDERS/_STREAM_PROVIDERS
entirely, so they exercise Turn/ask()'s bookkeeping without ever calling into a
real provider function. This file is the one that actually calls _ask_claude,
_stream_claude and _split_system, and the one that pins the import-time guard
that fails fast when BRAIN_PROVIDER=claude has no key.

Nothing here reaches the network or requires the `anthropic` package to be
importable during the ordinary run: _get_claude_client is patched directly in
every test but the import-guard ones, so the real `import anthropic` inside it
never executes. The guard tests reload jarvis.brain with BRAIN_PROVIDER=claude,
which does need `anthropic` importable -- same as it will be on a real run
that actually uses it, since it is now a normal (non-optional) dependency in
requirements.txt.
"""

from __future__ import annotations

import importlib
import unittest
from unittest import mock

from jarvis import brain, config


class SplitSystemTests(unittest.TestCase):
    def test_system_messages_join_in_order_convo_keeps_the_rest(self) -> None:
        messages = [
            {"role": "system", "content": "You are Jarvis."},
            {"role": "system", "content": "Memory: likes coffee."},
            {"role": "user", "content": "Γεια"},
            {"role": "assistant", "content": "Γεια σου!"},
        ]

        system, convo = brain._split_system(messages)

        self.assertEqual(system, "You are Jarvis.\n\nMemory: likes coffee.")
        self.assertEqual(
            convo,
            [
                {"role": "user", "content": "Γεια"},
                {"role": "assistant", "content": "Γεια σου!"},
            ],
        )

    def test_no_system_messages_gives_empty_string_and_keeps_convo(self) -> None:
        messages = [{"role": "user", "content": "Γεια"}]

        system, convo = brain._split_system(messages)

        self.assertEqual(system, "")
        self.assertEqual(convo, messages)


def _fake_response(text="Γεια σου!", stop_reason="end_turn", in_tok=10, out_tok=5):
    usage = mock.Mock(input_tokens=in_tok, output_tokens=out_tok)
    block = mock.Mock(type="text", text=text)
    return mock.Mock(content=[block], usage=usage, stop_reason=stop_reason)


class AskClaudeTests(unittest.TestCase):
    def test_extracts_text_and_passes_system_separately(self) -> None:
        client = mock.Mock()
        client.messages.create.return_value = _fake_response("Καλημέρα!")

        with mock.patch.object(brain, "_get_claude_client", return_value=client):
            reply = brain._ask_claude(
                [
                    {"role": "system", "content": "prompt"},
                    {"role": "user", "content": "τι κάνεις"},
                ]
            )

        self.assertEqual(reply, "Καλημέρα!")
        kwargs = client.messages.create.call_args.kwargs
        self.assertEqual(kwargs["system"], "prompt")
        self.assertEqual(kwargs["messages"], [{"role": "user", "content": "τι κάνεις"}])
        self.assertEqual(kwargs["model"], config.CLAUDE_MODEL)
        self.assertEqual(kwargs["max_tokens"], brain.MAX_REPLY_TOKENS)

    def test_max_tokens_stop_reason_trims_to_last_sentence(self) -> None:
        client = mock.Mock()
        client.messages.create.return_value = _fake_response(
            "Μια πρόταση. Και μια κομμένη", stop_reason="max_tokens"
        )

        with mock.patch.object(brain, "_get_claude_client", return_value=client):
            reply = brain._ask_claude([{"role": "user", "content": "πες μου κάτι"}])

        self.assertEqual(reply, "Μια πρόταση.")

    def test_end_turn_stop_reason_is_left_untrimmed(self) -> None:
        client = mock.Mock()
        client.messages.create.return_value = _fake_response(
            "Μια πλήρης πρόταση.", stop_reason="end_turn"
        )

        with mock.patch.object(brain, "_get_claude_client", return_value=client):
            reply = brain._ask_claude([{"role": "user", "content": "πες μου κάτι"}])

        self.assertEqual(reply, "Μια πλήρης πρόταση.")

    def test_non_text_content_blocks_are_ignored(self) -> None:
        response = mock.Mock(
            content=[mock.Mock(type="tool_use"), mock.Mock(type="text", text="ok")],
            usage=mock.Mock(input_tokens=1, output_tokens=1),
            stop_reason="end_turn",
        )
        client = mock.Mock()
        client.messages.create.return_value = response

        with mock.patch.object(brain, "_get_claude_client", return_value=client):
            reply = brain._ask_claude([{"role": "user", "content": "hi"}])

        self.assertEqual(reply, "ok")


class _FakeAnthropicStream:
    """Stands in for the context manager anthropic.messages.stream() returns."""

    def __init__(self, parts: list[str], stop_reason: str) -> None:
        self.text_stream = iter(parts)
        self._final = mock.Mock(
            stop_reason=stop_reason,
            usage=mock.Mock(input_tokens=1, output_tokens=len(parts)),
        )

    def __enter__(self) -> "_FakeAnthropicStream":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def get_final_message(self):
        return self._final


class StreamClaudeTests(unittest.TestCase):
    def test_deltas_are_yielded_in_order_and_done_reason_recorded(self) -> None:
        client = mock.Mock()
        client.messages.stream.return_value = _FakeAnthropicStream(
            ["Γεια", " σου"], stop_reason="end_turn"
        )

        with mock.patch.object(brain, "_get_claude_client", return_value=client):
            state: dict = {}
            pieces = list(
                brain._stream_claude([{"role": "user", "content": "γεια"}], state)
            )

        self.assertEqual(pieces, ["Γεια", " σου"])
        self.assertEqual(state["done_reason"], "end_turn")

    def test_max_tokens_stop_reason_is_translated_to_length(self) -> None:
        """Turn.truncated only ever checks for "length" (Ollama's word for
        this) -- Anthropic's "max_tokens" has to be translated here so that
        check stays provider-agnostic."""
        client = mock.Mock()
        client.messages.stream.return_value = _FakeAnthropicStream(
            ["μια"], stop_reason="max_tokens"
        )

        with mock.patch.object(brain, "_get_claude_client", return_value=client):
            state: dict = {}
            list(brain._stream_claude([{"role": "user", "content": "πες"}], state))

        self.assertEqual(state["done_reason"], "length")

    def test_system_and_convo_are_split_before_streaming(self) -> None:
        client = mock.Mock()
        client.messages.stream.return_value = _FakeAnthropicStream(
            ["ok"], stop_reason="end_turn"
        )

        with mock.patch.object(brain, "_get_claude_client", return_value=client):
            list(
                brain._stream_claude(
                    [
                        {"role": "system", "content": "prompt"},
                        {"role": "user", "content": "γεια"},
                    ],
                    {},
                )
            )

        kwargs = client.messages.stream.call_args.kwargs
        self.assertEqual(kwargs["system"], "prompt")
        self.assertEqual(kwargs["messages"], [{"role": "user", "content": "γεια"}])


class ImportGuardTests(unittest.TestCase):
    """BRAIN_PROVIDER=claude with no key must fail at import time, not on the
    first turn -- same reasoning as the unknown-provider check it sits beside.
    """

    def tearDown(self) -> None:
        # Leave the real module in place for whatever runs next, the same way
        # tests/test_speaker_lazy.py restores jarvis.speaker.
        importlib.reload(brain)

    def test_missing_key_raises_at_import(self) -> None:
        with mock.patch.object(config, "BRAIN_PROVIDER", "claude"):
            with mock.patch.object(config, "ANTHROPIC_API_KEY", ""):
                with self.assertRaises(ValueError):
                    importlib.reload(brain)

    def test_present_key_imports_cleanly(self) -> None:
        with mock.patch.object(config, "BRAIN_PROVIDER", "claude"):
            with mock.patch.object(config, "ANTHROPIC_API_KEY", "sk-ant-test-key"):
                importlib.reload(brain)

        self.assertEqual(brain.BRAIN_PROVIDER, "claude")
        self.assertIn("claude", brain._PROVIDERS)
        self.assertIn("claude", brain._STREAM_PROVIDERS)


if __name__ == "__main__":
    unittest.main()
