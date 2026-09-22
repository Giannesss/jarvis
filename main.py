import shlex

from jarvis import brain, db, listener, mem, memory, skills, speaker, wakeword
from jarvis.config import (
    CONVERSATION_MODE,
    CONVERSATION_TIMEOUT,
    NO_SPEECH_TIMEOUT,
    WAKE_BEEP,
    WAKE_WORD_ENABLED,
)


# Said when a recording came back with holes in it (listener returns "gap").
# Two tries, then back to the wake word: if the microphone is dropping that
# much audio, asking a third time won't fix it and the user is owed silence
# rather than a loop.
GAP_REPLY = "Δεν σε άκουσα καλά, πες το ξανά."
MAX_GAP_RETRIES = 2


def _reply_to(text: str) -> str | None:
    """Skill first, brain only when no skill matches. None on a brain error
    (already reported), so the caller just moves on to the next turn."""
    reply = skills.handle(text)
    if reply is not None:
        return reply

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


def _run_mem_command(command: str) -> None:
    """`:mem ...` at the Enter prompt, sharing jarvis/mem.py's dispatcher with
    the standalone `python -m jarvis.mem`."""
    try:
        argv = shlex.split(command)[1:]
    except ValueError as e:
        print(f"Σφάλμα εντολής: {e}")
        return

    if not argv:
        argv = ["--help"]

    try:
        mem.run(argv)
    except Exception as e:
        print(f"Σφάλμα μνήμης: {e}")


def main() -> None:
    _startup_backup()
    listener.preload()

    wake_word_active = WAKE_WORD_ENABLED
    if wake_word_active:
        try:
            wakeword.preload()
            listener.start_stream()
        except Exception as e:
            print(f"Ανίχνευση λέξης-κλειδί μη διαθέσιμη ({e}), πάτα Enter αντ' αυτού.")
            listener.stop_stream()  # no-op if it never started; guards partial startup
            wake_word_active = False

    if wake_word_active:
        print("Jarvis έτοιμος. Πες «Hey Jarvis» για να μιλήσεις (Ctrl+C για έξοδο).")
    else:
        print("Jarvis έτοιμος. Πάτα Enter για να μιλήσεις ('exit' για έξοδο).")

    try:
        while True:
            if wake_word_active:
                try:
                    preroll = listener.listen_for_wake_word()
                except Exception as e:
                    print(f"Σφάλμα ανίχνευσης ({e}), πάτα Enter αντ' αυτού.")
                    listener.stop_stream()  # tear down before listen() opens the mic
                    wake_word_active = False
                    continue

                if not _converse(preroll):
                    break
                continue

            command = input("> ")
            if command.strip().lower() in ("exit", "quit"):
                break

            # Typed memory admin, so the database is reachable without a
            # second terminal. Never recorded, never sent to the brain.
            if command.strip().startswith(":mem"):
                _run_mem_command(command.strip())
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
