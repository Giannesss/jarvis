"""Tests for the Claude brain: the message translation, and when it falls back.

Nothing here reaches the API, and nothing here needs the `anthropic` package
installed -- the SDK import is deferred inside brain._get_client(), which these
tests replace wholesale. That is deliberate rather than incidental: the suite has
to pass on a machine with no key and no SDK, which is also the state of anyone
who leaves BRAIN_PROVIDER at its default.

Two halves are worth pinning, and they are the two that can fail silently:

  * **the translation into the Messages API's shape.** A system message left in
    `messages` is a 400 on Haiku 4.5, and _build_messages() puts one there on
    every turn that recalls anything -- so the ordinary path is the failing one.
  * **which failures fall back to the local model.** A transient one should. A
    bad key must not: a fallback nobody notices means every reply quietly comes
    from qwen3 under Claude's name, which is a wrong answer with nothing on the
    terminal to show for it.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from jarvis import brain


# --- Fakes, standing in for the SDK -----------------------------------------
#
# Shaped like the objects the real client returns (attributes, not dicts) and no
# further: a block has .type and .text, a response has .content, .stop_reason
# and .usage. Same approach as OllamaProviderTests' dict chunks in
# test_brain_stream.py -- close enough to catch a wrong field name, not a
# reimplementation of the SDK.


class Block:
    def __init__(self, text: str, type: str = "text") -> None:
        self.text = text
        self.type = type


class Usage:
    def __init__(self, input_tokens: int = 1200, output_tokens: int = 40) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class Response:
    def __init__(self, *blocks: Block, stop_reason: str = "end_turn") -> None:
        self.content = list(blocks)
        self.stop_reason = stop_reason
        self.usage = Usage()


class Stream:
    """One `with client.messages.stream(...) as stream:` context.

    `fail_after` is how many deltas arrive before it raises, which is the whole
    point of the streaming fallback rule: before the first one a local answer is
    still possible, after it the words are already out of the speakers.
    """

    def __init__(
        self,
        *parts: str,
        stop_reason: str = "end_turn",
        error: Exception | None = None,
        fail_after: int = 0,
    ) -> None:
        self._parts = parts
        self._stop_reason = stop_reason
        self._error = error
        self._fail_after = fail_after

    def __enter__(self) -> "Stream":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    @property
    def text_stream(self):
        for index, part in enumerate(self._parts):
            if self._error is not None and index == self._fail_after:
                raise self._error
            yield part
        if self._error is not None and self._fail_after >= len(self._parts):
            raise self._error

    def get_final_message(self) -> Response:
        return Response(Block("".join(self._parts)), stop_reason=self._stop_reason)


class Messages:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.kwargs: dict = {}
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._result

    def stream(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._result


class Client:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.messages = Messages(result, error)


def status_error(status_code: int) -> Exception:
    """An SDK error carrying an HTTP status, which is what classifies it."""
    error = Exception(f"HTTP {status_code}")
    error.status_code = status_code
    return error


def sdk_error(name: str) -> Exception:
    """An `anthropic.<name>` with no status -- nothing reached Anthropic.

    Built rather than imported, so these tests never need the SDK: _is_transient
    matches on the class's module and name for exactly that reason.
    """
    return type(name, (Exception,), {"__module__": "anthropic"})("no route to host")


class ClaudeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # _history is module state shared with every other brain test.
        before = list(brain._history)
        self.addCleanup(brain._history.__setitem__, slice(None), before)

    @contextlib.contextmanager
    def client(self, **kwargs):
        """Replace _get_client, so `import anthropic` never runs."""
        fake = Client(**kwargs)
        with mock.patch.object(brain, "_get_client", return_value=fake):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                yield fake, out


# --- The translation --------------------------------------------------------


class MessageTranslationTests(ClaudeTestCase):
    def test_system_messages_are_joined_in_order_into_system(self) -> None:
        system, turns = brain._to_messages_api(
            [
                {"role": "system", "content": "πρώτο"},
                {"role": "user", "content": "γεια"},
                {"role": "system", "content": "δεύτερο"},
                {"role": "assistant", "content": "γεια σου"},
            ]
        )

        self.assertEqual(system, "πρώτο\n\nδεύτερο")
        self.assertEqual(
            turns,
            [
                {"role": "user", "content": "γεια"},
                {"role": "assistant", "content": "γεια σου"},
            ],
        )

    def test_no_system_role_survives_in_messages(self) -> None:
        """The one that would 400 on Haiku 4.5, and the ordinary path: every
        turn that recalls anything has a system message in the middle."""
        built = brain._build_messages("τι έχω σήμερα", "Το όνομά σου είναι Γιάννης.")

        system, turns = brain._to_messages_api(built)

        self.assertNotIn("system", [message["role"] for message in turns])
        self.assertIn(brain.SYSTEM_PROMPT, system)
        self.assertIn("Γιάννης", system)
        self.assertIn(brain.MEMORY_PREAMBLE, system)
        # The frozen prompt comes first; the recalled block is appended to it.
        self.assertLess(system.index(brain.SYSTEM_PROMPT), system.index("Γιάννης"))

    def test_the_first_message_is_the_user_and_the_few_shot_survives(self) -> None:
        _system, turns = brain._to_messages_api(brain._build_messages("γεια", None))

        self.assertEqual(turns[0]["role"], "user")
        self.assertEqual(turns[0], brain.FEW_SHOT_EXAMPLES[0])
        self.assertEqual(turns[-1], {"role": "user", "content": "γεια"})
        # Alternating from the first, which is what the API requires of the run
        # of examples the prompt opens with.
        roles = [message["role"] for message in turns[: len(brain.FEW_SHOT_EXAMPLES)]]
        self.assertEqual(roles, ["user", "assistant"] * (len(roles) // 2))

    def test_it_is_pure(self) -> None:
        messages = [
            {"role": "system", "content": "σύστημα"},
            {"role": "user", "content": "γεια"},
        ]
        before = [dict(message) for message in messages]

        brain._to_messages_api(messages)

        self.assertEqual(messages, before)


# --- The whole-reply provider ----------------------------------------------


class AskClaudeTests(ClaudeTestCase):
    def test_it_sends_the_model_the_cap_and_the_split_prompt(self) -> None:
        with self.client(result=Response(Block("Γεια σου."))) as (fake, _out):
            reply = brain._ask_claude(brain._build_messages("γεια", None))

        self.assertEqual(reply, "Γεια σου.")
        self.assertEqual(fake.messages.kwargs["model"], brain.CLAUDE_MODEL)
        self.assertEqual(fake.messages.kwargs["max_tokens"], brain.MAX_REPLY_TOKENS)
        self.assertIn(brain.SYSTEM_PROMPT, fake.messages.kwargs["system"])

    def test_it_sends_no_temperature_no_thinking_no_output_config(self) -> None:
        """A regression here is a 400 the day CLAUDE_MODEL is pointed at a
        5-series model: temperature is rejected there, and an absent `thinking`
        means adaptive thinking rather than none."""
        with self.client(result=Response(Block("ok"))) as (fake, _out):
            brain._ask_claude(brain._build_messages("γεια", None))

        for absent in ("temperature", "thinking", "output_config", "options"):
            self.assertNotIn(absent, fake.messages.kwargs)

    def test_text_is_joined_across_blocks_and_non_text_skipped(self) -> None:
        response = Response(
            Block("Πρώτο. ", "text"),
            Block("σκέψη", "thinking"),
            Block("Δεύτερο.", "text"),
        )
        with self.client(result=response) as (_fake, _out):
            self.assertEqual(
                brain._ask_claude(brain._build_messages("γεια", None)),
                "Πρώτο. Δεύτερο.",
            )

    def test_the_token_cap_trims_to_the_last_sentence(self) -> None:
        """max_tokens is Ollama's "length", so it runs the same trim."""
        cut = Response(
            Block("Πρώτη πρόταση. Δεύτερη μισ"), stop_reason="max_tokens"
        )
        with self.client(result=cut) as (_fake, _out):
            self.assertEqual(
                brain._ask_claude(brain._build_messages("γεια", None)),
                "Πρώτη πρόταση.",
            )

    def test_an_ordinary_stop_is_left_alone(self) -> None:
        with self.client(result=Response(Block("Μισή πρότ"))) as (_fake, _out):
            self.assertEqual(
                brain._ask_claude(brain._build_messages("γεια", None)), "Μισή πρότ"
            )


# --- The streaming provider -------------------------------------------------


class StreamClaudeTests(ClaudeTestCase):
    def drain(self, messages, state):
        return list(brain._stream_claude(messages, state))

    def test_deltas_arrive_in_order_and_the_stop_reason_comes_back(self) -> None:
        state: dict = {}
        with self.client(result=Stream("Γεια", " σου", "!")) as (fake, _out):
            pieces = self.drain(brain._build_messages("γεια", None), state)

        self.assertEqual(pieces, ["Γεια", " σου", "!"])
        self.assertEqual(state["done_reason"], "end_turn")
        self.assertEqual(fake.messages.kwargs["max_tokens"], brain.MAX_REPLY_TOKENS)

    def test_max_tokens_is_reported_as_length(self) -> None:
        """Turn.truncated reads Ollama's word, so this provider translates."""
        state: dict = {}
        with self.client(result=Stream("μισή πρότ", stop_reason="max_tokens")) as (
            _fake,
            _out,
        ):
            self.drain(brain._build_messages("γεια", None), state)

        self.assertEqual(state["done_reason"], brain.STOP_TRUNCATED)

    def test_a_stream_with_no_text_still_reports_a_stop_reason(self) -> None:
        state: dict = {}
        with self.client(result=Stream()) as (_fake, _out):
            self.assertEqual(self.drain(brain._build_messages("γεια", None), state), [])

        self.assertEqual(state["done_reason"], "end_turn")

    def test_empty_deltas_are_not_yielded(self) -> None:
        state: dict = {}
        with self.client(result=Stream("Γεια", "", " σου")) as (_fake, _out):
            pieces = self.drain(brain._build_messages("γεια", None), state)

        self.assertEqual(pieces, ["Γεια", " σου"])

    def test_it_reaches_the_turn_through_the_registry(self) -> None:
        """start_turn -> Turn.deltas picks the provider out of
        _STREAM_PROVIDERS by BRAIN_PROVIDER, so the wiring is what makes the
        streamed Claude path reachable at all."""
        with mock.patch.object(brain, "BRAIN_PROVIDER", "claude"):
            self.assertTrue(brain.streaming_available())
            with self.client(result=Stream("Γεια", " σου.")) as (_fake, _out):
                turn = brain.start_turn("γεια")
                self.assertEqual("".join(turn.deltas()), "Γεια σου.")
                turn.commit("Γεια σου.")

        self.assertEqual(brain._history[-1], {"role": "assistant", "content": "Γεια σου."})


# --- The fallback -----------------------------------------------------------


class FallbackTests(ClaudeTestCase):
    @contextlib.contextmanager
    def local(self, reply: str = "Τοπική απάντηση."):
        with mock.patch.object(brain, "_ask_ollama", return_value=reply) as ollama:
            yield ollama

    def test_a_rate_limit_is_answered_by_the_local_model(self) -> None:
        with self.local() as ollama:
            with self.client(error=status_error(429)) as (_fake, out):
                reply = brain._ask_claude(brain._build_messages("γεια", None))

        self.assertEqual(reply, "Τοπική απάντηση.")
        self.assertEqual(ollama.call_count, 1)
        # Announced, not silent: speaker.py prints its Edge->Piper line too.
        self.assertIn("τοπικού μοντέλου", out.getvalue())

    def test_a_server_error_is_answered_by_the_local_model(self) -> None:
        with self.local() as ollama:
            with self.client(error=status_error(503)) as (_fake, _out):
                brain._ask_claude(brain._build_messages("γεια", None))

        self.assertEqual(ollama.call_count, 1)

    def test_no_internet_is_answered_by_the_local_model(self) -> None:
        """The roadmap's offline fallback, and the reason this exists."""
        with self.local() as ollama:
            with self.client(error=sdk_error("APIConnectionError")) as (_fake, _out):
                brain._ask_claude(brain._build_messages("γεια", None))

        self.assertEqual(ollama.call_count, 1)

    def test_a_timeout_is_answered_by_the_local_model(self) -> None:
        with self.local() as ollama:
            with self.client(error=sdk_error("APITimeoutError")) as (_fake, _out):
                brain._ask_claude(brain._build_messages("γεια", None))

        self.assertEqual(ollama.call_count, 1)

    def test_a_bad_key_raises_and_never_reaches_the_local_model(self) -> None:
        """The one that must not fall back. A 401 will answer the same way next
        time, and a quiet qwen3 reply under Claude's name is a wrong answer with
        nothing on the terminal to show for it."""
        with self.local() as ollama:
            with self.client(error=status_error(401)) as (_fake, out):
                with self.assertRaises(Exception):
                    brain._ask_claude(brain._build_messages("γεια", None))

        self.assertEqual(ollama.call_count, 0)
        self.assertNotIn("τοπικού μοντέλου", out.getvalue())

    def test_a_forbidden_key_raises(self) -> None:
        with self.local() as ollama:
            with self.client(error=status_error(403)) as (_fake, _out):
                with self.assertRaises(Exception):
                    brain._ask_claude(brain._build_messages("γεια", None))

        self.assertEqual(ollama.call_count, 0)

    def test_a_bad_request_raises(self) -> None:
        with self.local() as ollama:
            with self.client(error=status_error(400)) as (_fake, _out):
                with self.assertRaises(Exception):
                    brain._ask_claude(brain._build_messages("γεια", None))

        self.assertEqual(ollama.call_count, 0)

    def test_an_unclassifiable_failure_raises(self) -> None:
        """A bug of ours never reached the network, so it is not transient --
        and a spoken error is cheaper than a silently different provider."""
        with self.local() as ollama:
            with self.client(error=TypeError("our own bug")) as (_fake, _out):
                with self.assertRaises(TypeError):
                    brain._ask_claude(brain._build_messages("γεια", None))

        self.assertEqual(ollama.call_count, 0)

    def test_the_switch_turns_it_off(self) -> None:
        with mock.patch.object(brain, "CLAUDE_FALLBACK_OLLAMA", False):
            with self.local() as ollama:
                with self.client(error=status_error(429)) as (_fake, _out):
                    with self.assertRaises(Exception):
                        brain._ask_claude(brain._build_messages("γεια", None))

        self.assertEqual(ollama.call_count, 0)

    def test_a_stream_that_fails_before_the_first_delta_falls_back(self) -> None:
        def local_stream(messages, state):
            state["done_reason"] = "stop"
            yield "Τοπική"
            yield " απάντηση."

        state: dict = {}
        with mock.patch.object(brain, "_stream_ollama", local_stream):
            with self.client(error=status_error(429)) as (_fake, out):
                pieces = list(
                    brain._stream_claude(brain._build_messages("γεια", None), state)
                )

        self.assertEqual(pieces, ["Τοπική", " απάντηση."])
        self.assertEqual(state["done_reason"], "stop")
        self.assertIn("τοπικού μοντέλου", out.getvalue())

    def test_a_stream_that_fails_after_a_delta_does_not_fall_back(self) -> None:
        """The words are already out of the speakers; starting over on the local
        model would say the first one twice."""
        failing = Stream("Γεια", " σου", error=status_error(429), fail_after=1)

        state: dict = {}
        with mock.patch.object(brain, "_stream_ollama") as ollama:
            with self.client(result=failing) as (_fake, out):
                with self.assertRaises(Exception):
                    list(brain._stream_claude(brain._build_messages("γεια", None), state))

        ollama.assert_not_called()
        self.assertNotIn("τοπικού μοντέλου", out.getvalue())

    def test_an_interrupted_stream_leaves_no_unanswered_question(self) -> None:
        """The same guarantee Turn already gives for Ollama, over this path."""
        failing = Stream("Γεια", error=status_error(429), fail_after=1)
        length = len(brain._history)

        with mock.patch.object(brain, "BRAIN_PROVIDER", "claude"):
            with self.client(result=failing) as (_fake, _out):
                turn = brain.start_turn("γεια")
                with self.assertRaises(Exception):
                    list(turn.deltas())

        self.assertEqual(len(brain._history), length)


# --- Configuration ----------------------------------------------------------


class KeyCheckTests(unittest.TestCase):
    """A missing key fails at startup, where it is one clear message, rather
    than per question, where it is a spoken «δεν μπορώ να απαντήσω» forever."""

    def test_claude_without_a_key_raises_and_names_the_variable(self) -> None:
        with self.assertRaises(ValueError) as caught:
            brain._check_key("claude", "")

        self.assertIn("ANTHROPIC_API_KEY", str(caught.exception))
        self.assertIn(".env", str(caught.exception))

    def test_whitespace_is_not_a_key(self) -> None:
        with self.assertRaises(ValueError):
            brain._check_key("claude", "   \n")

    def test_claude_with_a_key_is_fine(self) -> None:
        self.assertIsNone(brain._check_key("claude", "sk-ant-not-a-real-key"))

    def test_ollama_needs_no_key(self) -> None:
        self.assertIsNone(brain._check_key("ollama", ""))


class RegistryTests(unittest.TestCase):
    def test_claude_is_registered_on_both_paths(self) -> None:
        self.assertIs(brain._PROVIDERS["claude"], brain._ask_claude)
        self.assertIs(brain._STREAM_PROVIDERS["claude"], brain._stream_claude)

    def test_the_ollama_path_is_untouched(self) -> None:
        self.assertIs(brain._PROVIDERS["ollama"], brain._ask_ollama)
        self.assertIs(brain._STREAM_PROVIDERS["ollama"], brain._stream_ollama)


class TransientClassificationTests(unittest.TestCase):
    """The rule itself, away from the plumbing: transient falls back, the rest
    raises, and anything unrecognized raises."""

    def test_transient(self) -> None:
        for status in (408, 409, 429, 500, 502, 503, 529):
            with self.subTest(status=status):
                self.assertTrue(brain._is_transient(status_error(status)))

        for name in sorted(brain._TRANSIENT_ERROR_NAMES):
            with self.subTest(name=name):
                self.assertTrue(brain._is_transient(sdk_error(name)))

    def test_not_transient(self) -> None:
        for status in (400, 401, 403, 404, 413, 422):
            with self.subTest(status=status):
                self.assertFalse(brain._is_transient(status_error(status)))

    def test_an_unrelated_exception_is_not_transient(self) -> None:
        # Right name, wrong module: our own code, not the SDK's.
        home_grown = type("APIConnectionError", (Exception,), {})("boom")
        self.assertFalse(brain._is_transient(home_grown))
        self.assertFalse(brain._is_transient(RuntimeError("boom")))
        self.assertFalse(brain._is_transient(sdk_error("BadRequestError")))


if __name__ == "__main__":
    unittest.main()
