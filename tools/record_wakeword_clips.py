"""Record real "Τζάρβις" clips through the Razer mic for wake-word training.

Not part of the app — a one-off data-prep tool, companion to
tools/gen_wakeword_clips.py.

Synthetic Greek clips only come from four TTS voices, none of which sound
like you in your room. These recordings carry your actual mic response and
room reverb, so 50-100 of them help more than another 10k synthetic clips.

Vary things deliberately across takes: close (0.5m) / normal (2m) / across
the room; quiet and loud; fast and slow; some with the TV on.

Usage:
    .\\.venv\\Scripts\\python.exe tools\\record_wakeword_clips.py --out clips

Enter records one take, `r` re-records the last one, `q` stops. Clips are
split ~90/10 into positive_train/ and positive_test/.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.listener import FFMPEG_PATH, MICROPHONE_NAME  # noqa: E402

TAKE_SECONDS = 1.5
TARGET_RATE = 16000

# Every 10th take goes to the test split.
TEST_EVERY = 10


def _record(path: Path) -> bool:
    """Capture one fixed-length take straight to 16kHz mono WAV. Returns
    False if ffmpeg failed (e.g. the mic name no longer matches)."""
    command = [
        FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "dshow",
        "-audio_buffer_size", "1000",
        "-i", f"audio={MICROPHONE_NAME}",
        "-t", str(TAKE_SECONDS),
        "-ar", str(TARGET_RATE),
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(path),
    ]

    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(f"Σφάλμα μικροφώνου: {result.stderr.strip() or 'άγνωστο σφάλμα'}")
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="clips", help="clip directory root")
    parser.add_argument("--phrase", default="Τζάρβις", help="what to say")
    args = parser.parse_args()

    out_root = Path(args.out)
    train_dir = out_root / "positive_train"
    test_dir = out_root / "positive_test"
    for directory in (train_dir, test_dir):
        directory.mkdir(parents=True, exist_ok=True)

    # Continue numbering rather than overwriting an earlier session's takes.
    existing = len(list(train_dir.glob("real_*.wav"))) + len(list(test_dir.glob("real_*.wav")))
    take = existing

    print(f"Λέγε «{args.phrase}» — {TAKE_SECONDS}s ανά λήψη.")
    print("Enter = ηχογράφηση, r = επανάληψη τελευταίας, q = έξοδος.")
    if existing:
        print(f"({existing} λήψεις υπάρχουν ήδη, συνεχίζουμε από εκεί.)")

    last_path: Path | None = None

    while True:
        choice = input(f"[{take}] > ").strip().lower()

        if choice in ("q", "quit", "exit"):
            break

        if choice in ("r", "repeat"):
            if last_path is None:
                print("Καμία λήψη ακόμα.")
                continue
            print("Ηχογράφηση (ξανά)...")
            _record(last_path)
            continue

        directory = test_dir if take % TEST_EVERY == TEST_EVERY - 1 else train_dir
        path = directory / f"real_{take:04d}.wav"

        print("Ηχογράφηση...")
        if not _record(path):
            break

        last_path = path
        take += 1
        print(f"  -> {directory.name}/{path.name}")

    print(f"\nΣύνολο: {take - existing} νέες λήψεις σε {out_root}/")
    print(f"  positive_train: {len(list(train_dir.glob('real_*.wav')))} real")
    print(f"  positive_test:  {len(list(test_dir.glob('real_*.wav')))} real")


if __name__ == "__main__":
    main()
