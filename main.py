import shlex
from typing import Callable, NamedTuple

from jarvis import (
    brain,
    chunker,
    db,
    diag,
    listener,
    mem,
    memory,
    policy,
    scheduler,
    skills,
    speaker,
    wakeword,
)
from jarvis.config import (
    BARGE_IN_ENABLED,
    BARGE_PREROLL,
    CONVERSATION_MODE,
    CONVERSATION_TIMEOUT,
    NO_SPEECH_TIMEOUT,
    STREAM_REPLIES,
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

# How many times a stalled microphone stream is restarted before giving up
# on wake-word mode. A DirectShow device that goes quiet under a live ffmpeg
# usually comes back when the capture is reopened; one that does not is a
# problem no further restart will fix, and the Enter prompt still works.
MAX_STREAM_RESTARTS = 2

# Said when the brain call itself fails (Ollama down, or the intermittent
# CUDA error). Spoken, not just printed: in wake-word mode nobody is
# watching the terminal, so a print-only failure is indistinguishable from
# Jarvis ignoring the question.
BRAIN_ERROR_REPLY = "Συγγνώμη, δεν μπορώ να απαντήσω αυτή τη στιγμή."

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


class Answer(NamedTuple):
    """One answered utterance.

    `spoke` is False only for the frozen backstop -- "say nothing at all" --
    which the caller has to tell apart from an ordinary reply. `barge` is what
    interrupted it, if anything did.
    """

    spoke: bool
    barge: "listener.BargeResult | None" = None


def _barge_armed() -> bool:
    """Whether talking over Jarvis can stop him, right now.

    Wake-word mode only. Barge-in judges the frames the capture gate refuses
    while he is audible, and those only exist on the persistent stream; the
    Enter-press path opens the microphone per utterance and is not recording at
    all while he talks.
    """
    return BARGE_IN_ENABLED and _wake_word_active


def _say(text: str) -> Answer:
    """Speak one finished line -- a skill reply, a cue -- interruptibly.

    speaker.stop() is the callback because it is the output half of barge-in
    already: idempotent, safe from the reader thread, and it honours a stop
    that arrives before there is anything to stop.
    """
    if not _barge_armed():
        speaker.speak(text)
        return Answer(True)

    listener.arm_barge(speaker.stop)
    try:
        speaker.speak(text)
    finally:
        barge = listener.disarm_barge(collect=BARGE_PREROLL)

    return Answer(True, barge)


def _whole_reply(text: str) -> str:
    """The brain's answer, synthesized only once it is finished."""
    try:
        # recall_safe never raises: a broken or locked database means no
        # memory this turn, not a failed reply.
        return brain.ask(text, memory.recall_safe(text))
    except Exception as e:
        # brain.ask() has already dropped this turn from its history, so the
        # next question starts clean rather than trailing an unanswered one.
        print(f"Σφάλμα κατά την κλήση στο μοντέλο: {e}")
        return BRAIN_ERROR_REPLY


def _stream_reply(text: str) -> Answer:
    """The brain's answer, spoken as it is generated.

    Three moving parts, each owning one question: brain.Turn streams the
    deltas and settles the history, chunker.Chunker decides where a piece is
    worth saying, and speaker.speak_stream turns pieces into one continuous
    sound that can be cut off mid-word.

    The history is committed from what was *heard*, not from what was
    generated -- see brain.Turn. That is the whole reason this is not just
    brain.ask() with a callback.
    """
    turn = brain.start_turn(text, memory.recall_safe(text))
    cut = chunker.Chunker()
    printed = False

    def pieces():
        for delta in turn.deltas():
            yield from cut.feed(delta)

        tail = cut.flush(truncated=turn.truncated)
        if tail:
            yield tail

    def show(piece: str) -> None:
        # Called as each piece reaches the device, so the terminal fills at the
        # pace the speaker does -- and stops where the speaker stopped. Printing
        # from pieces() instead would show sentences a barge-in means nobody
        # ever hears, since generation runs a chunk or two ahead of playback.
        nonlocal printed
        print(("" if printed else "Jarvis: ") + piece, end=" ", flush=True)
        printed = True

    if _barge_armed():
        listener.arm_barge(speaker.stop)

    try:
        result = speaker.speak_stream(pieces(), on_chunk=show)
    except Exception as e:
        # The stream itself failed (Ollama down, the intermittent CUDA error).
        # turn.deltas() has already dropped the turn from history; abandon() is
        # idempotent and makes that true however the failure arrived.
        turn.abandon()
        if printed:
            print()
        print(f"Σφάλμα κατά την κλήση στο μοντέλο: {e}")
        return _say(BRAIN_ERROR_REPLY)
    finally:
        barge = listener.disarm_barge(collect=BARGE_PREROLL) if _barge_armed() else None

    if printed:
        print(" [διακοπή]" if result.aborted else "")

    turn.commit(result.spoken)
    return Answer(True, barge)


def _answer(text: str) -> Answer:
    """Answer one utterance out loud: skill first, brain only when none match."""
    reply = skills.handle(text)
    if reply is not None:
        print(f"Jarvis: {reply}")
        return _say(reply)

    if policy.is_frozen():
        # Belt and braces. policy.intercept() already refuses everything but
        # a shutdown phrase while frozen, so getting here means one that no
        # skill claimed -- the brain still must not answer for it.
        return Answer(False)

    policy.record("brain", "allowed", "no_skill_matched")

    if STREAM_REPLIES and brain.streaming_available():
        return _stream_reply(text)

    reply = _whole_reply(text)
    print(f"Jarvis: {reply}")
    return _say(reply)


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
    # The words that interrupted the last reply, if one was interrupted: they
    # are the start of this command and live nowhere else, since the capture
    # gate refused them while Jarvis was audible. See listener.take/disarm.
    barge_frames: list[tuple[float, bytes]] | None = None
    timeout = NO_SPEECH_TIMEOUT
    gap_retries = 0

    while True:
        text, stop_reason = listener.record_command(
            turn_preroll, timeout, preroll_frames=barge_frames
        )
        turn_preroll = b""
        barge_frames = None
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

        answer = _answer(text)
        if not answer.spoke:
            listener.flush()
            if not CONVERSATION_MODE:
                return True
            continue

        if skills.shutdown_requested:
            return False

        if not CONVERSATION_MODE:
            return True

        if answer.barge is not None:
            # He was talked over. Do *not* flush: the floor would be raised
            # past the second the user is still speaking in, throwing away the
            # rest of the sentence that stopped him. The audible part of it is
            # already out of the pipeline (the capture gate refused it) and
            # comes back as the pre-roll instead.
            barge_frames = answer.barge.frames or None
            diag.log(
                f"[barge] interrupted at {answer.barge.onset_at:.2f}s "
                f"({answer.barge.delta_db:.1f} dB over the floor), "
                f"{len(answer.barge.frames)} frames kept"
            )
            continue

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

    # First line of the run, so one session's diagnostics can be told from
    # the previous one's when the log spans several restarts.
    diag.start_session(f"session start (wake word {WAKE_WORD_ENABLED})")

    _startup_backup()
    # A restart is one of the two documented ways out of the kill switch, and
    # the flag is persisted so a second terminal can reach a running Jarvis.
    # Clearing it here is what keeps the freeze from outliving the process.
    policy.clear_on_startup()
    policy.set_confirm_asker(_ask_confirm)
    listener.preload()

    # After clear_on_startup(), so the catch-up is not swallowed by a freeze
    # left over from the last run, and before the loop, so anything missed
    # while Jarvis was off is heard right after "Jarvis έτοιμος".
    scheduler.start(speaker.speak)

    _wake_word_active = WAKE_WORD_ENABLED
    stream_restarts = 0
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
                except listener.StreamStalled as e:
                    # ffmpeg is still alive; the device stopped feeding it.
                    # Reopening the capture is the one thing that actually
                    # fixes that, and it is cheap -- so try before falling
                    # back to a prompt the user has to be at a keyboard for.
                    if stream_restarts >= MAX_STREAM_RESTARTS:
                        print(f"Η ροή μικροφώνου κόλλησε ξανά ({e}), "
                              f"πάτα Enter αντ' αυτού.")
                        listener.stop_stream()
                        _wake_word_active = False
                        continue

                    stream_restarts += 1
                    diag.log(
                        f"[wake] stream stalled ({e}); restart "
                        f"{stream_restarts}/{MAX_STREAM_RESTARTS}"
                    )
                    print("Η ροή μικροφώνου κόλλησε, επανεκκίνηση...")
                    try:
                        listener.stop_stream()
                        listener.start_stream()
                    except Exception as restart_error:
                        print(f"Απέτυχε ({restart_error}), πάτα Enter αντ' αυτού.")
                        listener.stop_stream()
                        _wake_word_active = False
                    continue
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

            _answer(text)

            if skills.shutdown_requested:
                break
    finally:
        scheduler.stop()
        listener.stop_stream()  # always runs: Ctrl+C, exception, or normal exit


if __name__ == "__main__":
    main()
