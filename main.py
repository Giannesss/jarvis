from jarvis import brain, listener, skills, speaker, wakeword
from jarvis.config import (
    CONVERSATION_MODE,
    CONVERSATION_TIMEOUT,
    NO_SPEECH_TIMEOUT,
    WAKE_BEEP,
    WAKE_WORD_ENABLED,
)


def _reply_to(text: str) -> str | None:
    """Skill first, brain only when no skill matches. None on a brain error
    (already reported), so the caller just moves on to the next turn."""
    reply = skills.handle(text)
    if reply is not None:
        return reply

    try:
        return brain.ask(text)
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

    while True:
        text, stop_reason = listener.record_command(turn_preroll, timeout)
        turn_preroll = b""
        timeout = CONVERSATION_TIMEOUT

        if text is None:
            if stop_reason == "no_speech":
                speaker.beep_done()  # back to waiting for the wake word
            return True

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


def main() -> None:
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
