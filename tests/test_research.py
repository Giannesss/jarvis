"""Tests for the research skill (Phase 5 step 1): trigger parsing, the
expertise table's upsert, the web-search call, and the skill wired through
policy's CONFIRM path end to end.

Four layers, each testable without the others:

  * memory.parse_research() -- pure, offline, no I/O.
  * memory.save_expertise()/get_expertise() -- a real temp database, no
    network.
  * brain.research() / brain._final_text() -- brain._get_client() replaced,
    same idiom as tests/test_brain_claude.py, so the anthropic package is
    never needed to run this file.
  * skills._handle_research()/_research_matches() through policy.dispatch()
    -- the seam that proves the CONFIRM permission actually gates it, not
    just that the pieces exist in isolation (same reasoning as
    test_memory_skills.py's docstring).
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import brain, db, memory, policy, skills


# --- Trigger parsing ---------------------------------------------------------


class ParseResearchTests(unittest.TestCase):
    def test_plain_trigger(self) -> None:
        topic, is_refresh = memory.parse_research("Κάνε έρευνα για τα μαθηματικά")
        self.assertEqual(topic, "τα μαθηματικά")
        self.assertFalse(is_refresh)

    def test_ereynise_form(self) -> None:
        topic, is_refresh = memory.parse_research(
            "Ερεύνησε τις τιμές διαμερισμάτων στην Αθήνα"
        )
        self.assertEqual(topic, "τις τιμές διαμερισμάτων στην Αθήνα")
        self.assertFalse(is_refresh)

    def test_psakse_sto_internet_form(self) -> None:
        topic, is_refresh = memory.parse_research(
            "Ψάξε στο ίντερνετ για το πρόγραμμα σπουδών"
        )
        self.assertEqual(topic, "το πρόγραμμα σπουδών")
        self.assertFalse(is_refresh)

    def test_refresh_trigger(self) -> None:
        topic, is_refresh = memory.parse_research(
            "Ξανακάνε έρευνα για τα μαθηματικά"
        )
        self.assertEqual(topic, "τα μαθηματικά")
        self.assertTrue(is_refresh)

    def test_leading_jarvis_address_is_skipped(self) -> None:
        topic, is_refresh = memory.parse_research(
            "Τζάρβις, κάνε έρευνα για το ελληνικό ποδόσφαιρο"
        )
        self.assertEqual(topic, "το ελληνικό ποδόσφαιρο")
        self.assertFalse(is_refresh)

    def test_topic_is_verbatim_not_folded(self) -> None:
        # Norm.group() reads the capture back out of the raw text, the same
        # guarantee memory.parse()'s captures carry -- see CLAUDE.md
        # "Verbatim captures". A folded topic ("εκπα" instead of "ΕΚΠΑ")
        # would search worse.
        topic, _ = memory.parse_research("Κάνε έρευνα για το ΕΚΠΑ")
        self.assertEqual(topic, "το ΕΚΠΑ")

    def test_unrelated_text_is_not_a_trigger(self) -> None:
        self.assertIsNone(memory.parse_research("Τι ώρα είναι"))

    def test_recall_question_is_not_a_trigger(self) -> None:
        # "θυμάσαι" must never be swallowed by this -- it belongs to
        # MEMORY_RECALL_PHRASES.
        self.assertIsNone(memory.parse_research("Θυμάσαι τι σου είπα;"))

    def test_memory_save_trigger_is_not_a_research_trigger(self) -> None:
        self.assertIsNone(memory.parse_research("Θυμήσου ότι με λένε Γιάννη"))

    def test_no_topic_after_the_verb_is_not_a_trigger(self) -> None:
        # "Κάνε έρευνα" alone has no (?P<topic>.+) to capture -- the pattern
        # requires at least one character after it.
        self.assertIsNone(memory.parse_research("Κάνε έρευνα"))


# --- The expertise table -----------------------------------------------------


class ExpertiseStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = db.connect(Path(self._tmp.name) / "jarvis.db")
        self.addCleanup(self.conn.close)

    def test_save_then_get_round_trips(self) -> None:
        memory.save_expertise("Μαθηματικά ΕΚΠΑ", "Πρώτη περίληψη.", self.conn)
        row = memory.get_expertise("Μαθηματικά ΕΚΠΑ", self.conn)
        self.assertEqual(row["topic"], "Μαθηματικά ΕΚΠΑ")
        self.assertEqual(row["summary"], "Πρώτη περίληψη.")

    def test_get_is_accent_and_case_insensitive(self) -> None:
        memory.save_expertise("Μαθηματικά ΕΚΠΑ", "Περίληψη.", self.conn)
        row = memory.get_expertise("μαθηματικα εκπα", self.conn)
        self.assertIsNotNone(row)

    def test_researching_the_same_topic_again_updates_in_place(self) -> None:
        first_id = memory.save_expertise("Μαθηματικά ΕΚΠΑ", "Παλιά περίληψη.", self.conn)
        second_id = memory.save_expertise(
            "μαθηματικα εκπα", "Νέα, ενημερωμένη περίληψη.", self.conn
        )
        self.assertEqual(first_id, second_id)
        row = memory.get_expertise("Μαθηματικά ΕΚΠΑ", self.conn)
        self.assertEqual(row["summary"], "Νέα, ενημερωμένη περίληψη.")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM expertise").fetchone()["n"],
            1,
        )

    def test_unrelated_topics_do_not_collide(self) -> None:
        memory.save_expertise("Μαθηματικά ΕΚΠΑ", "Α.", self.conn)
        memory.save_expertise("Ιστορία ΕΚΠΑ", "Β.", self.conn)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM expertise").fetchone()["n"],
            2,
        )

    def test_found_by_keyword_search_on_topic(self) -> None:
        memory.save_expertise(
            "Εξετάσεις εισαγωγής ΕΚΠΑ", "Οι εξετάσεις γίνονται τον Ιούνιο.", self.conn
        )
        hits = memory.search("εξετάσεις εισαγωγής", self.conn)
        self.assertTrue(any("Ιούνιο" in hit for hit in hits))

    def test_found_by_keyword_search_on_summary_content(self) -> None:
        # norm covers topic *and* summary, unlike topic_key -- a question
        # about a word that only appears inside the summary should still
        # surface the row.
        memory.save_expertise(
            "Το μαγαζί μου", "Οι πελάτες προτιμούν εσπρέσο τον χειμώνα.", self.conn
        )
        hits = memory.search("εσπρέσο", self.conn)
        self.assertTrue(any("μαγαζί" in hit for hit in hits))


# --- brain.research() --------------------------------------------------------
#
# Same idiom as tests/test_brain_claude.py: _get_client() is replaced
# wholesale, so `import anthropic` never runs and this file needs no key, no
# SDK and no network to pass.


class Block:
    def __init__(self, type: str, text: str | None = None) -> None:
        self.type = type
        self.text = text


class Usage:
    def __init__(self, input_tokens: int = 40, output_tokens: int = 20) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class Response:
    def __init__(self, *blocks: Block) -> None:
        self.content = list(blocks)
        self.usage = Usage()


class Messages:
    def __init__(self, result: Response, kwargs_out: dict) -> None:
        self._result = result
        self._kwargs_out = kwargs_out

    def create(self, **kwargs):
        self._kwargs_out.update(kwargs)
        return self._result


class Client:
    def __init__(self, result: Response, kwargs_out: dict) -> None:
        self.messages = Messages(result, kwargs_out)


class FinalTextTests(unittest.TestCase):
    def test_only_text_after_the_last_search_result_counts(self) -> None:
        content = [
            Block("text", "Θα ψάξω για αυτό..."),
            Block("server_tool_use"),
            Block("web_search_tool_result"),
            Block("text", "Πρώτο κομμάτι."),
            Block("text", "Δεύτερο κομμάτι."),
        ]
        self.assertEqual(brain._final_text(content), "Πρώτο κομμάτι.\nΔεύτερο κομμάτι.")

    def test_no_search_result_falls_back_to_every_text_block(self) -> None:
        content = [Block("text", "Απλό κείμενο, καμία αναζήτηση.")]
        self.assertEqual(brain._final_text(content), "Απλό κείμενο, καμία αναζήτηση.")

    def test_empty_content_is_empty_string(self) -> None:
        self.assertEqual(brain._final_text([]), "")

    def test_two_search_rounds_keeps_only_the_final_answer(self) -> None:
        content = [
            Block("web_search_tool_result"),
            Block("text", "Θα ψάξω για κάτι ακόμα..."),
            Block("web_search_tool_result"),
            Block("text", "Η τελική περίληψη."),
        ]
        self.assertEqual(brain._final_text(content), "Η τελική περίληψη.")


class ResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(brain, "ANTHROPIC_API_KEY", "sk-ant-fake")
        patcher.start()
        self.addCleanup(patcher.stop)

    @contextlib.contextmanager
    def client(self, result: Response):
        kwargs: dict = {}
        fake = Client(result, kwargs)
        with mock.patch.object(brain, "_get_client", return_value=fake):
            yield kwargs

    def test_missing_key_raises_before_reaching_the_client(self) -> None:
        with mock.patch.object(brain, "ANTHROPIC_API_KEY", ""):
            with mock.patch.object(brain, "_get_client") as get_client:
                with self.assertRaises(brain.ResearchUnavailable):
                    brain.research("κάτι")
            get_client.assert_not_called()

    def test_returns_the_final_summary(self) -> None:
        response = Response(
            Block("server_tool_use"),
            Block("web_search_tool_result"),
            Block("text", "Η περίληψη των ευρημάτων."),
        )
        with self.client(response):
            with contextlib.redirect_stdout(io.StringIO()):
                result = brain.research("τα μαθηματικά")
        self.assertEqual(result, "Η περίληψη των ευρημάτων.")

    def test_sends_the_web_search_tool(self) -> None:
        response = Response(Block("text", "Περίληψη."))
        with self.client(response) as kwargs:
            with contextlib.redirect_stdout(io.StringIO()):
                brain.research("ένα θέμα")
        self.assertEqual(kwargs["tools"][0]["type"], "web_search_20250305")
        self.assertEqual(kwargs["tools"][0]["max_uses"], brain.RESEARCH_MAX_SEARCHES)
        self.assertEqual(kwargs["model"], brain.CLAUDE_RESEARCH_MODEL)
        self.assertNotIn("system", kwargs.get("messages", [{}])[0])

    def test_empty_summary_raises(self) -> None:
        response = Response(Block("server_tool_use"), Block("web_search_tool_result"))
        with self.client(response):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(brain.ResearchUnavailable):
                    brain.research("κάτι")


# --- Wired through the skill and policy's CONFIRM gate -----------------------


class ResearchSkillTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(db, "DB_PATH", Path(self._tmp.name) / "jarvis.db")
        patcher.start()
        self.addCleanup(patcher.stop)

        policy._frozen = False

        asker_patcher = mock.patch.object(policy, "_confirm_asker", None)
        asker_patcher.start()
        self.addCleanup(asker_patcher.stop)

        research_patcher = mock.patch.object(brain, "research")
        self.fake_research = research_patcher.start()
        self.fake_research.return_value = "Μια σύντομη δοκιμαστική περίληψη."
        self.addCleanup(research_patcher.stop)


class ResearchSkillMatchingTests(ResearchSkillTestCase):
    def test_matches_a_research_trigger(self) -> None:
        self.assertTrue(skills._research_matches("κάνε έρευνα για τα μαθηματικά"))

    def test_does_not_match_unrelated_text(self) -> None:
        self.assertFalse(skills._research_matches("τι ώρα είναι"))


class ResearchSkillDispatchTests(ResearchSkillTestCase):
    def test_no_confirm_asker_installed_is_a_no(self) -> None:
        # Same rule as every other CONFIRM path: an unattended process must
        # not be able to approve on the user's behalf.
        reply = skills.handle("κάνε έρευνα για τα μαθηματικά")
        self.assertEqual(reply, policy.DECLINED_REPLY)
        self.fake_research.assert_not_called()

    def test_confirmed_runs_research_and_saves_it(self) -> None:
        policy.set_confirm_asker(lambda question: True)
        reply = skills.handle("κάνε έρευνα για τα μαθηματικά")

        self.fake_research.assert_called_once_with("τα μαθηματικά")
        self.assertIn("Έκανα έρευνα για τα μαθηματικά", reply)
        self.assertIn("Μια σύντομη δοκιμαστική περίληψη.", reply)

        conn = db.connect(db.DB_PATH)
        try:
            row = conn.execute("SELECT * FROM expertise").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["topic"], "τα μαθηματικά")

    def test_declined_does_not_run_research(self) -> None:
        policy.set_confirm_asker(lambda question: False)
        reply = skills.handle("ερεύνησε τις εξετάσεις εισαγωγής")
        self.assertEqual(reply, policy.DECLINED_REPLY)
        self.fake_research.assert_not_called()

    def test_refresh_trigger_is_worded_differently(self) -> None:
        policy.set_confirm_asker(lambda question: True)
        reply = skills.handle("ξανακάνε έρευνα για τα μαθηματικά")
        self.assertIn("Ξανάκανα την έρευνα για τα μαθηματικά", reply)

    def test_unavailable_reason_is_spoken_verbatim(self) -> None:
        self.fake_research.side_effect = brain.ResearchUnavailable("Δεν έχω κλειδί.")
        policy.set_confirm_asker(lambda question: True)
        reply = skills.handle("κάνε έρευνα για κάτι")
        self.assertEqual(reply, "Δεν έχω κλειδί.")

    def test_unexpected_error_is_a_generic_apology_not_a_crash(self) -> None:
        self.fake_research.side_effect = RuntimeError("boom")
        policy.set_confirm_asker(lambda question: True)
        with contextlib.redirect_stdout(io.StringIO()):
            reply = skills.handle("κάνε έρευνα για κάτι")
        self.assertEqual(reply, "Δεν μπόρεσα να κάνω την έρευνα τώρα.")

    def test_audit_row_reflects_the_confirmation(self) -> None:
        policy.set_confirm_asker(lambda question: True)
        skills.handle("κάνε έρευνα για τα μαθηματικά")

        conn = db.connect(db.DB_PATH)
        try:
            row = conn.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["action"], "research")
        self.assertEqual(row["decision"], "allowed")
        self.assertEqual(row["reason"], "user_confirmed")


if __name__ == "__main__":
    unittest.main()
