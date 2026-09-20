from jarvis import brain, listener, skills, speaker


def main() -> None:
    listener.preload()
    print("Jarvis έτοιμος. Πάτα Enter για να μιλήσεις ('exit' για έξοδο).")

    while True:
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


if __name__ == "__main__":
    main()
