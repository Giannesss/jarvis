from jarvis import brain, listener, speaker


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

        try:
            reply = brain.ask(text)
        except Exception as e:
            print(f"Σφάλμα κατά την κλήση στο τοπικό μοντέλο: {e}")
            continue

        print(f"Jarvis: {reply}")
        speaker.speak(reply)


if __name__ == "__main__":
    main()
