from jarvis import brain, listener, skills, speaker, wakeword
from jarvis.config import WAKE_WORD_ENABLED


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
                    preroll, segment_start = listener.listen_for_wake_word()
                except Exception as e:
                    print(f"Σφάλμα ανίχνευσης ({e}), πάτα Enter αντ' αυτού.")
                    listener.stop_stream()  # tear down before listen() ever opens the mic
                    wake_word_active = False
                    continue
                text = listener.record_command(preroll, segment_start)
            else:
                command = input("> ")
                if command.strip().lower() in ("exit", "quit"):
                    break
                text = listener.listen()

            if not text:
                continue

            print(f"Εσύ: {text}")

            reply = skills.handle(text)
            if reply is None:
                try:
                    reply = brain.ask(text)
                except Exception as e:
                    print(f"Σφάλμα κατά την κλήση στο τοπικό μοντέλο: {e}")
                    continue

            print(f"Jarvis: {reply}")
            speaker.speak(reply)

            if skills.shutdown_requested:
                break
    finally:
        listener.stop_stream()  # always runs: Ctrl+C, exception, or normal exit


if __name__ == "__main__":
    main()
