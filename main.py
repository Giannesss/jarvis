import shlex
from typing import Callable

from jarvis import brain, db, listener, mem, memory, policy, skills, speaker, wakeword
from jarvis.config import (
    CONVERSATION_MODE,
    CONVERSATION_TIMEOUT,
    NO_SPEECH_TIMEOUT,
    WAKE_BEEP,
    WAKE_WORD_ENABLED,
)
from jarvis.text import is_yes


# Said when a recording came back with holes in it (listener returns "gap").
# Two tries, then back to the wake word: if the microphone is dropping that
# much audio, asking a third time won't fix it and the user is owed silence
# rather than a loop.
GAP_REPLY = "Δεν σε άκουσα καλά, πες το ξανά."
MAX_GAP_RETRIES = 2

# Whether the wake word is driving the loop, which also decides how
# _ask_confirm() listens for an answer. Module-level rather than a local in
# main() because the confirm asker is installed into policy once at startup,
# but wake-word mode can still fall back to the Enter prompt mid-run -- and an
# answer has to be read whichever way is live *at the time of the question*.
_wake_word_active = False


def _ask_confirm(question: str) -> bool:
    """Ask a yes/no question out loud and read the answer back.

    Installed into policy at startup, so policy itself never imports the
    microphone. Silence, a mangled answer, or a recording with holes in it
    are all a no -- see text.is_yes(); the burden is on the yes.
    """
    print(f"Jarvis: {question}")
    speaker.speak(question)

    if _wake_word_active:
        # Same discipline as _converse(): drop whatever was captured around
        # the question so the answer can't be Jarvis's own voice asking it.
        listener.flush()
        answer, _stop_reason = listener.record_command(b"", CONVERSATION_TIMEOUT)
    else:
        answer = listener.listen()

    if not answer:
        return False

    print(f"Εσύ: {answer}")
    return is_yes(answer)


def _reply_to(text: str) -> str | None:
    """Skill first, brain only when no skill matches. None on a brain error
    (already reported), so the caller just moves on to the next turn."""
    reply = skills.handle(text)
    if reply is not None:
        return reply

    if policy.is_frozen():
        # Belt and braces. policy.intercept() already refuses everything but
        # a shutdown phrase while frozen, so getting here means one that no
        # skill claimed -- the brain still must not answer for it.
        return None

    policy.record("brain", "allowed", "no_skill_matched")

    try:
        # recall_safe never raises: a broken or locked database means no
        # memory this turn, not a failed reply.
        return brain.ask(text, memory.recall_safe(text))
    except Exception as e:
        print(f"Σφάλμα κατά την κλήση στο τοπικό μοντέλο: {e}")
        return None


def _converse(preroll: bytes) -> bool:
    """Everything after the wake word fires: a single command, or a whole
    back-and-forth when CONVERSATION_MODE is on.

    Conversation mode ends on an end phrase (skills.is_conversation_end),
    on CONVERSATION_TIMEOUT seconds of silence, or when the stream dies —
    each time returning to the wake word with a cue so it's audible which
    state Jarvis is in. Returns False only if Jarvis was asked to shut
    down entirely ("κλείσε"), which stops the outer loop too."""
    listener.acknowledge()  # beep, then flush: the wake word isn't recorded

    # The pre-roll ends with the wake word itself, so it's only usable on
    # the WAKE_BEEP=false path (no beep, nothing flushed). Follow-up turns
    # never replay it.
    turn_preroll = b"" if WAKE_BEEP else preroll
    timeout = NO_SPEECH_TIMEOUT
    gap_retries = 0

    while True:
        text, stop_reason = listener.record_command(turn_preroll, timeout)
        turn_preroll = b""
        timeout = CONVERSATION_TIMEOUT

        if text is None:
            if stop_reason == "gap" and gap_retries < MAX_GAP_RETRIES:
                # Too much of that utterance never arrived to transcribe it
                # honestly (see listener._StopDecider). Ask again rather than
                # answer a spliced one.
                gap_retries += 1
                speaker.speak(GAP_REPLY)
                listener.flush()
                timeout = NO_SPEECH_TIMEOUT  # a re-ask starts the turn over
                continue

            if stop_reason == "no_speech":
                speaker.beep_done()  # back to waiting for the wake word
            return True

        gap_retries = 0
        print(f"Εσύ: {text}")

        if skills.is_conversation_end(text):
            speaker.speak("Εντάξει.")
            return True

        reply = _reply_to(text)
        if reply is None:
            listener.flush()
            if not CONVERSATION_MODE:
                return True
            continue

        print(f"Jarvis: {reply}")
        speaker.speak(reply)

        if skills.shutdown_requested:
            return False

        if not CONVERSATION_MODE:
            return True

        # Drop whatever was captured around the reply, so the next turn
        # can't record Jarvis's own voice as the user's command.
        listener.flush()


def _startup_backup() -> None:
    """Snapshot the memory database before this session can touch it.

    Wrapped whole: a failed backup is worth reporting but must never stop
    Jarvis from starting.
    """
    try:
        path = db.backup()
        if path is not None:
            print(f"[memory] Αντίγραφο ασφαλείας: {path}")
    except Exception as e:
        print(f"Σφάλμα αντιγράφου μνήμης: {e}")


def _run_admin_command(command: str, runner: Callable[[list[str]], int]) -> None:
    """`:mem ...` / `:policy ...` at the Enter prompt, each sharing its own
    module's argv dispatcher with its standalone `python -m` entry point."""
    try:
        argv = shlex.split(command)[1:]
    except ValueError as e:
        print(f"Σφάλμα εντολής: {e}")
        return

    if not argv:
        argv = ["--help"]

    try:
        runner(argv)
    except Exception as e:
        print(f"Σφάλμα εντολής: {e}")


def main() -> None:
    global _wake_word_active

    _startup_backup()
    # A restart is one of the two documented ways out of the kill switch, and
    # the flag is persisted so a second terminal can reach a running Jarvis.
    # Clearing it here is what keeps the freeze from outliving the process.
    policy.clear_on_startup()
    policy.set_confirm_asker(_ask_confirm)
    listener.preload()

    _wake_word_active = WAKE_WORD_ENABLED
    if _wake_word_active:
        try:
            wakeword.preload()
            listener.start_stream()
        except Exception as e:
            print(f"Ανίχνευση λέξης-κλειδί μη διαθέσιμη ({e}), πάτα Enter αντ' αυτού.")
            listener.stop_stream()  # no-op if it never started; guards partial startup
            _wake_word_active = False

    if _wake_word_active:
        print("Jarvis έτοιμος. Πες «Hey Jarvis» για να μιλήσεις (Ctrl+C για έξοδο).")
    else:
        print("Jarvis έτοιμος. Πάτα Enter για να μιλήσεις ('exit' για έξοδο).")

    try:
        while True:
            if _wake_word_active:
                try:
                    preroll = listener.listen_for_wake_word()
                except Exception as e:
                    print(f"Σφάλμα ανίχνευσης ({e}), πάτα Enter αντ' αυτού.")
                    listener.stop_stream()  # tear down before listen() opens the mic
                    _wake_word_active = False
                    continue

                if not _converse(preroll):
                    break
                continue

            command = input("> ")
            if command.strip().lower() in ("exit", "quit"):
                break

            # Typed admin, so the database and the kill switch are reachable
            # without a second terminal. Never recorded, never sent to the
            # brain -- and ":policy unlock" deliberately cannot be spoken.
            if command.strip().startswith(":mem"):
                _run_admin_command(command.strip(), mem.run)
                continue

            if command.strip().startswith(":policy"):
                _run_admin_command(command.strip(), policy.run)
                continue

            text = listener.listen()
            if not text:
                continue

            print(f"Εσύ: {text}")

            reply = _reply_to(text)
            if reply is None:
                continue

            print(f"Jarvis: {reply}")
            speaker.speak(reply)

            if skills.shutdown_requested:
                break
    finally:
        listener.stop_stream()  # always runs: Ctrl+C, exception, or normal exit


if __name__ == "__main__":
    main()
