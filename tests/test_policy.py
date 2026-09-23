"""Tests for jarvis/policy.py: the kill switch, deny-by-default, the confirm
path, and the audit log.

This is the safety layer, so the tests that matter most are the negative ones
-- what must *not* happen. A kill switch that freezes but can't be unlocked,
or unlocks when it shouldn't, is worse than none at all.

Every test runs against a throwaway database (db.DB_PATH is patched, as in
test_db.py), and each one resets the module-level frozen flag, since it is
process state that would otherwise leak between tests.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
from jarvis import db, policy, skills
from jarvis.policy import Permission, Skill
from jarvis.text import normalize


def _reset_frozen() -> None:
    """Clear the in-process frozen cache without performing an unlock.

    Not policy.thaw(): that is a real decision and writes a real audit row,
    which would seed every test's log with an entry it did not cause. The
    database itself is fresh per test; only this module-level flag leaks.
    """
    policy._frozen = False


class PolicyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "jarvis.db"

        patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        _reset_frozen()
        policy.set_confirm_asker(None)
        self.addCleanup(policy.set_confirm_asker, None)
        self.addCleanup(_reset_frozen)

        skills.shutdown_requested = False
        self.addCleanup(lambda: setattr(skills, "shutdown_requested", False))

    def audit(self) -> list:
        conn = db.connect(self.db_path)
        try:
            return conn.execute("SELECT * FROM audit ORDER BY id").fetchall()
        finally:
            conn.close()


def _noop(_arg: str) -> str:
    return "έγινε"


class KillSwitchTests(PolicyTestCase):
    def test_kill_phrase_freezes(self) -> None:
        self.assertEqual(skills.handle("σταμάτα τα πάντα"), policy.FREEZE_REPLY)
        self.assertTrue(policy.is_frozen())

    def test_kill_phrase_matches_inside_a_sentence(self) -> None:
        # Substring, deliberately: a false positive is recoverable, a kill
        # switch that didn't fire is not.
        self.assertEqual(
            skills.handle("Τζάρβις σταμάτα τα πάντα τώρα"), policy.FREEZE_REPLY
        )
        self.assertTrue(policy.is_frozen())

    def test_kill_phrase_survives_whisper_punctuation(self) -> None:
        # normalize() does not strip punctuation and Whisper inserts it
        # mid-phrase; a plain substring check would miss this one.
        self.assertEqual(skills.handle("Σταμάτα, τα πάντα."), policy.FREEZE_REPLY)
        self.assertTrue(policy.is_frozen())

    def test_kill_phrase_matches_without_accents(self) -> None:
        self.assertEqual(skills.handle("σταματα τα παντα"), policy.FREEZE_REPLY)

    def test_alternate_kill_phrases(self) -> None:
        for phrase in ("σταμάτησε τα πάντα", "πάγωσε τα πάντα"):
            with self.subTest(phrase=phrase):
                policy.thaw("cli")
                self.assertEqual(skills.handle(phrase), policy.FREEZE_REPLY)
                self.assertTrue(policy.is_frozen())

    def test_ordinary_speech_does_not_freeze(self) -> None:
        for phrase in ("σταμάτα το χρονόμετρο", "τα πάντα είναι εντάξει", "σταμάτα"):
            with self.subTest(phrase=phrase):
                skills.handle(phrase)
                self.assertFalse(policy.is_frozen(), phrase)


class FuzzyKillSwitchTests(PolicyTestCase):
    """The kill switch through a transcription the recognizer mangled.

    The negatives are the half that matters. Loosening the trigger trades a
    missed freeze for the risk of one nobody asked for -- the same trade as
    memory's fuzzy triggers, but with the asymmetry the other way round, so
    the ending's budget is generous and the mandatory "τα πάντα" is what
    keeps it honest.
    """

    # Both said aloud as «σταμάτα τα πάντα»; both normalize to
    # "στα ματα τα παντα", a word break inside the verb that no amount of
    # separator tolerance *between* words would have caught.
    LIVE = ("Στα μάτα τα πάντα", "Λέω στα μάτα τα πάντα")

    # Endings the recognizer has not invented yet, but will.
    MANGLED = (
        "σταματάω τα πάντα",
        "σταματήστε τα πάντα",
        "στα μάτια τα πάντα",
        "πάγωσαι τα πάντα",
        "στάμα τα τα πάντα",
        "σταμάτα τα πά ντα",   # the object split, not the verb
        "Τζάρβις, στα μάτα τα πάντα τώρα",
    )

    NEGATIVES = (
        "σταμάτα το χρονόμετρο",
        "τα πάντα είναι εντάξει",
        "σταμάτα",
        "κοίτα με στα μάτια",
        "μου αρέσουν τα πάντα σε αυτό το μάθημα",
        "σταμάτησα να καπνίζω",
        "θυμήσου ότι τα πάντα ρει",
        "ο Ηράκλειτος είπε τα πάντα ρει",
        "σταμάτησε η βροχή",
        "πάγωσε το ψυγείο",
        "πάγωσε ο υπολογιστής",
        "στα μάτια σου τα πάντα",
        "σταμάτα τη μουσική",
        "βάλε μου παγωτό",
        "το σταμάτημα ήταν απότομο",
        "ξέρεις τα πάντα για τα μαθηματικά",
        "μου τα είπε όλα, τα πάντα",
        "άνοιξε τα μάτια σου",
        "τι ώρα είναι",
    )

    def test_the_live_transcriptions_freeze(self) -> None:
        for phrase in self.LIVE:
            with self.subTest(phrase=phrase):
                _reset_frozen()
                self.assertEqual(skills.handle(phrase), policy.FREEZE_REPLY)
                self.assertTrue(policy.is_frozen())

    def test_a_mangled_ending_freezes(self) -> None:
        for phrase in self.MANGLED:
            with self.subTest(phrase=phrase):
                _reset_frozen()
                self.assertEqual(skills.handle(phrase), policy.FREEZE_REPLY)
                self.assertTrue(policy.is_frozen())

    def test_ordinary_speech_still_does_not_freeze(self) -> None:
        for phrase in self.NEGATIVES:
            with self.subTest(phrase=phrase):
                _reset_frozen()
                skills.handle(phrase)
                self.assertFalse(policy.is_frozen(), phrase)

    def test_the_object_is_what_holds_the_line(self) -> None:
        # "κιτα με στα ματια" contains the verb: "στα ματια" matches the stem
        # "σταματ" across the space with an ending 1 edit from "α". It does
        # not fire because no "τα πάντα" follows -- and it does fire the
        # moment one does. This is the guard, not the budget.
        self.assertFalse(policy.fuzzy_kill(normalize("κοίτα με στα μάτια")))
        self.assertTrue(policy.fuzzy_kill(normalize("κοίτα στα μάτια τα πάντα")))

    def test_the_object_must_follow_the_verb_immediately(self) -> None:
        # Pinning the boundary, not celebrating it: adjacency is what keeps
        # "στα μάτια σου τα πάντα" out, and the price is that an adverb in
        # the middle misses. «σταμάτα τώρα τα πάντα» does not freeze -- it
        # did not before this change either, so nothing regressed, but it is
        # the first thing to revisit if a live miss looks like this.
        self.assertFalse(policy.fuzzy_kill(normalize("σταμάτα τώρα τα πάντα")))

    def test_an_exact_phrase_never_reaches_the_fuzzy_path(self) -> None:
        # Guard 2 from memory's fuzzy triggers: everything that freezes today
        # keeps its exact path, and the audit log tells the two apart.
        skills.handle("σταμάτα τα πάντα")
        self.assertEqual(self.audit()[-1]["reason"], "voice")

    def test_a_fuzzy_freeze_is_logged_as_such(self) -> None:
        skills.handle("Στα μάτα τα πάντα")
        row = self.audit()[-1]
        self.assertEqual(row["action"], "kill_switch")
        self.assertEqual(row["decision"], "frozen")
        self.assertEqual(row["reason"], "voice_fuzzy")


class FrozenBehaviourTests(PolicyTestCase):
    def test_frozen_refuses_an_ordinary_skill(self) -> None:
        skills.handle("σταμάτα τα πάντα")
        self.assertEqual(skills.handle("Τι ώρα είναι;"), policy.FROZEN_REPLY)

    def test_frozen_refuses_a_memory_save(self) -> None:
        skills.handle("σταμάτα τα πάντα")
        self.assertEqual(
            skills.handle("Θυμήσου ότι με λένε Γιάννης"), policy.FROZEN_REPLY
        )
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute("SELECT * FROM profile").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [], "a frozen Jarvis must not write to memory")

    def test_shutdown_still_works_while_frozen(self) -> None:
        skills.handle("σταμάτα τα πάντα")
        self.assertEqual(skills.handle("Κλείσε"), "Αντίο!")
        self.assertTrue(skills.shutdown_requested)

    def test_frozen_does_not_reach_the_brain(self) -> None:
        """_reply_to()'s frozen check is a backstop: intercept() already
        refuses everything but a shutdown phrase, and the shutdown skill
        claims those, so nothing normally falls through to the brain while
        frozen. Pinned anyway -- it is the last thing standing between a
        frozen Jarvis and the model if a skill ever declines one."""
        skills.handle("σταμάτα τα πάντα")

        with mock.patch.object(main.skills, "handle", return_value=None), \
                mock.patch.object(main.brain, "ask") as ask:
            self.assertIsNone(main._reply_to("κλείσε"))
        ask.assert_not_called()

    def test_the_brain_is_reached_and_logged_when_not_frozen(self) -> None:
        with mock.patch.object(main.brain, "ask", return_value="απάντηση") as ask:
            self.assertEqual(main._reply_to("Πες μου ένα ανέκδοτο"), "απάντηση")
        ask.assert_called_once()

        row = self.audit()[-1]
        self.assertEqual(
            (row["action"], row["decision"], row["reason"]),
            ("brain", "allowed", "no_skill_matched"),
        )

    def test_freezing_twice_is_harmless(self) -> None:
        skills.handle("σταμάτα τα πάντα")
        self.assertEqual(skills.handle("σταμάτα τα πάντα"), policy.FREEZE_REPLY)
        self.assertTrue(policy.is_frozen())


class UnlockTests(PolicyTestCase):
    def test_restart_clears_the_freeze(self) -> None:
        skills.handle("σταμάτα τα πάντα")
        self.assertTrue(policy.is_frozen())

        policy.clear_on_startup()
        self.assertFalse(policy.is_frozen())
        self.assertIsNotNone(skills.handle("Τι ώρα είναι;"))

    def test_cli_unlock_thaws(self) -> None:
        skills.handle("σταμάτα τα πάντα")
        lines: list[str] = []
        self.assertEqual(policy.run(["unlock"], out=lines.append), 0)
        self.assertFalse(policy.is_frozen())
        self.assertIn("Ξεπάγωσε.", lines)

    def test_cli_unlock_reaches_another_process(self) -> None:
        """The whole point of persisting the flag: a second terminal's unlock
        has to be visible to the already-running Jarvis, so the in-process
        cache must not win over the database."""
        skills.handle("σταμάτα τα πάντα")
        self.assertTrue(policy._frozen, "precondition: frozen in this process")

        # Stand in for the second terminal: its own connection, writing the
        # same row this process will read back.
        other = db.connect(self.db_path)
        try:
            policy.run(["unlock"], conn=other, out=lambda _line: None)
        finally:
            other.close()

        self.assertFalse(policy.is_frozen())
        self.assertIsNotNone(skills.handle("Τι ώρα είναι;"))

    def test_cli_freeze_then_status(self) -> None:
        lines: list[str] = []
        policy.run(["freeze"], out=lines.append)
        policy.run(["status"], out=lines.append)
        self.assertIn("Παγωμένος.", lines)
        self.assertTrue(policy.is_frozen())

    def test_unlock_when_not_frozen_says_so(self) -> None:
        lines: list[str] = []
        self.assertEqual(policy.run(["unlock"], out=lines.append), 0)
        self.assertIn("Δεν ήταν παγωμένος.", lines)

    def test_frozen_state_survives_an_unreadable_database(self) -> None:
        """If the flag cannot be read, the last known value stands. Forgetting
        a freeze is the expensive direction."""
        skills.handle("σταμάτα τα πάντα")
        with mock.patch.object(policy, "_read_state", return_value=None):
            self.assertTrue(policy.is_frozen())


class DenyByDefaultTests(PolicyTestCase):
    def test_a_skill_registered_without_a_permission_is_blocked(self) -> None:
        ran = []
        skill = Skill(
            name="forgot",
            phrases=(),
            description="Registered without naming a permission.",
            handler=lambda arg: ran.append(arg) or "δεν έπρεπε",
            matches=lambda _arg: True,
        )
        self.assertIs(skill.permission, Permission.BLOCKED)
        self.assertEqual(policy.dispatch(skill, "οτιδήποτε"), policy.BLOCKED_REPLY)
        self.assertEqual(ran, [], "a blocked skill must never run its handler")

    def test_blocked_without_a_matcher_never_runs(self) -> None:
        ran = []
        skill = Skill(
            name="silent",
            phrases=(),
            description="Blocked and unmatchable.",
            handler=lambda arg: ran.append(arg) or "δεν έπρεπε",
        )
        self.assertIsNone(policy.dispatch(skill, "οτιδήποτε"))
        self.assertEqual(ran, [])

    def test_an_unrecognised_permission_is_blocked(self) -> None:
        skill = Skill(
            name="weird",
            phrases=(),
            description="Permission is not a Permission.",
            handler=_noop,
            permission="safe",  # a string, not the enum
            matches=lambda _arg: True,
        )
        self.assertEqual(policy.dispatch(skill, "οτιδήποτε"), policy.BLOCKED_REPLY)
        self.assertEqual(self.audit()[-1]["reason"], "unknown_permission")

    def test_confirm_skill_without_a_matcher_is_a_registration_error(self) -> None:
        with self.assertRaises(ValueError):
            Skill(
                name="half",
                phrases=(),
                description="CONFIRM with no way to recognise itself.",
                handler=_noop,
                permission=Permission.CONFIRM,
                confirm_prompt="Να το κάνω;",
            )

    def test_confirm_skill_without_a_prompt_is_a_registration_error(self) -> None:
        with self.assertRaises(ValueError):
            Skill(
                name="half",
                phrases=(),
                description="CONFIRM with nothing to ask.",
                handler=_noop,
                permission=Permission.CONFIRM,
                matches=lambda _arg: True,
            )


class ConfirmTests(PolicyTestCase):
    def _skill(self, ran: list) -> Skill:
        return Skill(
            name="delete_thing",
            phrases=(),
            description="Stands in for a Phase 7 file delete.",
            handler=lambda arg: ran.append(arg) or "Το έσβησα.",
            permission=Permission.CONFIRM,
            matches=lambda arg: "σβήσε" in arg,
            confirm_prompt="Να το σβήσω;",
        )

    def test_yes_runs_it(self) -> None:
        ran: list = []
        policy.set_confirm_asker(lambda _q: True)
        self.assertEqual(policy.dispatch(self._skill(ran), "σβήσε το"), "Το έσβησα.")
        self.assertEqual(ran, ["σβήσε το"])
        self.assertEqual(self.audit()[-1]["reason"], "user_confirmed")

    def test_no_does_not(self) -> None:
        ran: list = []
        policy.set_confirm_asker(lambda _q: False)
        self.assertEqual(
            policy.dispatch(self._skill(ran), "σβήσε το"), policy.DECLINED_REPLY
        )
        self.assertEqual(ran, [], "a declined action must not run")
        self.assertEqual(self.audit()[-1]["decision"], "declined")

    def test_no_asker_installed_denies(self) -> None:
        ran: list = []
        self.assertEqual(
            policy.dispatch(self._skill(ran), "σβήσε το"), policy.DECLINED_REPLY
        )
        self.assertEqual(ran, [])

    def test_a_raising_asker_denies(self) -> None:
        ran: list = []
        policy.set_confirm_asker(mock.Mock(side_effect=RuntimeError("no mic")))
        self.assertEqual(
            policy.dispatch(self._skill(ran), "σβήσε το"), policy.DECLINED_REPLY
        )
        self.assertEqual(ran, [])

    def test_not_matching_asks_nothing(self) -> None:
        ran: list = []
        asker = mock.Mock(return_value=True)
        policy.set_confirm_asker(asker)
        self.assertIsNone(policy.dispatch(self._skill(ran), "τι ώρα είναι"))
        asker.assert_not_called()
        self.assertEqual(ran, [])

    def test_the_question_asked_is_the_skills_own(self) -> None:
        asker = mock.Mock(return_value=False)
        policy.set_confirm_asker(asker)
        policy.dispatch(self._skill([]), "σβήσε το")
        asker.assert_called_once_with("Να το σβήσω;")


class AuditTests(PolicyTestCase):
    def test_a_safe_skill_is_logged(self) -> None:
        skills.handle("Τι ώρα είναι;")
        row = self.audit()[-1]
        self.assertEqual(row["action"], "time")
        self.assertEqual(row["decision"], "allowed")
        self.assertEqual(row["reason"], "safe")

    def test_a_skill_that_did_not_match_is_not_logged(self) -> None:
        skills.handle("Πες μου ένα ανέκδοτο")
        self.assertEqual(self.audit(), [], "only the skill that replied is logged")

    def test_the_freeze_is_logged(self) -> None:
        skills.handle("σταμάτα τα πάντα")
        row = self.audit()[-1]
        self.assertEqual((row["action"], row["decision"]), ("kill_switch", "frozen"))

    def test_a_refusal_while_frozen_is_logged_without_the_utterance(self) -> None:
        skills.handle("σταμάτα τα πάντα")
        skills.handle("Θυμήσου ότι ο κωδικός μου είναι abc12345")

        row = self.audit()[-1]
        self.assertEqual((row["action"], row["decision"]), ("request", "frozen"))
        for value in (row["action"], row["decision"], row["reason"]):
            self.assertNotIn("κωδικ", value or "")
            self.assertNotIn("abc12345", value or "")

    def test_no_audit_row_ever_holds_spoken_text(self) -> None:
        for phrase in (
            "Τι ώρα είναι;",
            "σταμάτα τα πάντα",
            "Θυμήσου ότι με λένε Γιάννης",
        ):
            skills.handle(phrase)

        for row in self.audit():
            self.assertIn(row["decision"], policy.DECISIONS)
            self.assertIn(row["reason"], policy.REASONS)
            # Actions are skill names or fixed words, never an utterance.
            self.assertNotIn(" ", row["action"])

    def test_a_failed_audit_write_does_not_break_the_turn(self) -> None:
        with mock.patch.object(policy, "_execute", side_effect=RuntimeError("locked")):
            self.assertTrue(skills.handle("Τι ώρα είναι;").startswith("Η ώρα είναι"))


class RegistryTests(PolicyTestCase):
    def test_every_registered_skill_is_safe(self) -> None:
        # If one of these ever stops being SAFE it needs a matcher and a
        # spoken prompt, which is a deliberate decision, not a default.
        for skill in skills.SKILLS:
            with self.subTest(skill=skill.name):
                self.assertIs(skill.permission, Permission.SAFE)

    def test_registry_order_is_unchanged(self) -> None:
        self.assertEqual(
            [skill.name for skill in skills.SKILLS],
            ["shutdown", "memory_save", "memory_recall", "timer", "time", "date",
             "open_site_or_app"],
        )

    def test_skills_reexports_the_policy_types(self) -> None:
        self.assertIs(skills.Permission, Permission)
        self.assertIs(skills.Skill, Skill)


if __name__ == "__main__":
    unittest.main()
