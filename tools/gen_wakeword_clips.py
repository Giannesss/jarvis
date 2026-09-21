"""Generate Greek training clips for a custom openWakeWord "Τζάρβις" model.

Not part of the app — a one-off data-prep tool, like the *_test.py
experiments at the repo root.

openWakeWord's own `train.py --generate_clips` can't be used for Greek: it
calls piper-sample-generator with no voice parameter (always the English
LibriTTS-R checkpoint), and its adversarial negatives are phonemized with
`lang='en_us'`. So we write the four clip directories it expects ourselves
and run only `--augment_clips` and `--train_model` afterwards:

    <out>/positive_train/  <out>/positive_test/
    <out>/negative_train/  <out>/negative_test/

all 16kHz mono 16-bit WAV.

Greek has only four usable TTS voices (two Piper, two Edge), so speaker
variety is thin — prosody is randomized per clip to compensate, and you
should add real recordings of your own voice on top (see
tools/record_wakeword_clips.py).

Usage:
    .\\.venv\\Scripts\\python.exe tools\\gen_wakeword_clips.py --out clips

Re-running skips clips that already exist, so an interrupted run (Edge TTS
needs internet) can just be restarted.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.listener import FFMPEG_PATH  # noqa: E402

TARGET_RATE = 16000
MAX_SECONDS = 2.0
MAX_SAMPLES = int(TARGET_RATE * MAX_SECONDS)

# The wake word itself, in spelling variants that cover how it's actually
# said (stress on either syllable, softer/harder initial consonant). All of
# these train into one binary model. Deliberately bare — adding a carrier
# like "Έι Τζάρβις" teaches the model to fire on the carrier instead.
#
# English "Jarvis" needs no entry: a Greek speaker says it as "Τζάρβις", and
# the Greek espeak voice already synthesizes exactly that accent. The Latin
# spelling is kept out (espeak-el mangles it) and appears only in the
# negatives below, as near-miss context.
POSITIVE_PHRASES = [
    "Τζάρβις",
    "Τζαρβίς",
    "Τζάρβης",
    "Τζιάρβις",
    "Τζάρβις.",
]

# Phonetic near-misses that must NOT fire. Add anything you say often at
# home that ends in -ις / -ρβις.
NEGATIVE_PHRASES = [
    "σέρβις", "το σέρβις", "σερβίρω", "σερβιτόρος", "σερβιέτα",
    "Σερβία", "σέρβος",
    "τζάμι", "τζάκι", "τζάμπα", "τζίρος", "τζιν", "τζόγος", "τζαζ",
    "τζατζίκι", "τζίτζικας", "τζάμια", "τζόκερ",
    "Τζώρτζης", "Τζόρτζια", "Τζένη", "Τζίμης", "Τζον", "Τζακ",
    "άρβυλο", "αρβύλα", "βάρβαρος", "βαρβάτος", "βαρβαρότητα", "Βαρβάρα",
    "χάρβαλο", "κάρβουνο", "μάρβελ", "γκαρίζω", "τσάρος", "τσάρλι",
    "ζάρια", "ζάχαρη", "ζαβός", "δάρβις", "θάρρος",
    "Γιάννης", "Γιάννη", "Χάρης", "Χάρη", "Άρης",
    "service", "Travis", "Jarvis", "Harvey", "Charlie",
]

PIPER_VOICES = [
    ("joy", "models/el_GR-joy-medium.onnx"),
    ("rapunzelina", "models/el_GR-rapunzelina-low.onnx"),
]

EDGE_VOICES = [
    ("athina", "el-GR-AthinaNeural"),
    ("nestoras", "el-GR-NestorasNeural"),
]

# Edge TTS is network-bound, so it contributes a minority of the clips.
EDGE_FRACTION = 0.2
EDGE_CONCURRENCY = 8

SPLITS = ("positive_train", "positive_test", "negative_train", "negative_test")


def _resample_to_target(audio: np.ndarray, src_rate: int) -> np.ndarray:
    if src_rate == TARGET_RATE:
        return audio
    divisor = math.gcd(src_rate, TARGET_RATE)
    resampled = resample_poly(
        audio.astype(np.float32), TARGET_RATE // divisor, src_rate // divisor
    )
    return np.clip(resampled, -32768, 32767).astype(np.int16)


def _write_wav(path: Path, audio: np.ndarray, src_rate: int) -> None:
    audio = _resample_to_target(audio, src_rate)[:MAX_SAMPLES]
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(TARGET_RATE)
        wav_file.writeframes(audio.tobytes())


# --- Piper -----------------------------------------------------------------


def _load_piper_voices(repo_root: Path):
    from piper import PiperVoice

    voices = []
    for name, relative in PIPER_VOICES:
        path = repo_root / relative
        if not path.exists():
            print(f"[skip] Piper voice not found: {path}")
            continue
        print(f"[load] Piper voice {name} ({path.name})")
        voices.append((name, PiperVoice.load(path)))

    if not voices:
        raise SystemExit("No Piper voices found under models/ — nothing to generate.")
    return voices


def _piper_clip(voice, phrase: str, rng: random.Random) -> tuple[np.ndarray, int]:
    from piper import SynthesisConfig

    config = SynthesisConfig(
        length_scale=rng.uniform(0.75, 1.30),
        noise_scale=rng.uniform(0.55, 0.85),
        noise_w_scale=rng.uniform(0.60, 1.00),
        normalize_audio=True,
    )
    chunks = list(voice.synthesize(phrase, config))
    audio = np.concatenate([chunk.audio_int16_array for chunk in chunks])
    return audio, chunks[0].sample_rate


def _generate_piper(out_dir: Path, phrases, count: int, voices, rng) -> None:
    for index in range(count):
        voice_name, voice = voices[index % len(voices)]
        path = out_dir / f"piper_{voice_name}_{index:06d}.wav"
        if path.exists():
            continue
        audio, rate = _piper_clip(voice, rng.choice(phrases), rng)
        _write_wav(path, audio, rate)
        if (index + 1) % 500 == 0:
            print(f"  [{out_dir.name}] piper {index + 1}/{count}")


# --- Edge TTS --------------------------------------------------------------


def _decode_mp3(mp3_path: Path) -> np.ndarray:
    """Decode to 16kHz mono s16le with ffmpeg, the same tool speaker.py uses
    to play Edge output — piped, so no intermediate wav file."""
    result = subprocess.run(
        [
            FFMPEG_PATH, "-y", "-loglevel", "error",
            "-i", str(mp3_path),
            "-f", "s16le", "-ar", str(TARGET_RATE), "-ac", "1", "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    return np.frombuffer(result.stdout, dtype=np.int16)


async def _edge_clip(
    semaphore: asyncio.Semaphore,
    path: Path,
    voice: str,
    phrase: str,
    rate: str,
    pitch: str,
) -> None:
    import edge_tts

    async with semaphore:
        mp3_path = Path(tempfile.gettempdir()) / f"oww_{path.stem}.mp3"
        try:
            await edge_tts.Communicate(phrase, voice, rate=rate, pitch=pitch).save(
                str(mp3_path)
            )
            audio = _decode_mp3(mp3_path)
        except Exception as e:
            print(f"  [warn] Edge TTS failed for {path.name}: {e}")
            return
        finally:
            mp3_path.unlink(missing_ok=True)

        _write_wav(path, audio, TARGET_RATE)


async def _generate_edge(out_dir: Path, phrases, count: int, rng) -> None:
    semaphore = asyncio.Semaphore(EDGE_CONCURRENCY)
    tasks = []

    for index in range(count):
        voice_name, voice = EDGE_VOICES[index % len(EDGE_VOICES)]
        path = out_dir / f"edge_{voice_name}_{index:06d}.wav"
        if path.exists():
            continue
        tasks.append(
            _edge_clip(
                semaphore,
                path,
                voice,
                rng.choice(phrases),
                rate=f"{rng.randint(-20, 20):+d}%",
                pitch=f"{rng.randint(-15, 15):+d}Hz",
            )
        )

    if not tasks:
        return

    print(f"  [{out_dir.name}] edge {len(tasks)} clips...")
    await asyncio.gather(*tasks)


# --- Driver ----------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="clips", help="output directory")
    parser.add_argument("--n-samples", type=int, default=20000, help="train clips per class")
    parser.add_argument("--n-samples-val", type=int, default=2000, help="test clips per class")
    parser.add_argument("--skip-edge", action="store_true", help="Piper only, no internet needed")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    out_root = Path(args.out)
    for split in SPLITS:
        (out_root / split).mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.seed)
    voices = _load_piper_voices(repo_root)

    plan = [
        ("positive_train", POSITIVE_PHRASES, args.n_samples),
        ("positive_test", POSITIVE_PHRASES, args.n_samples_val),
        ("negative_train", NEGATIVE_PHRASES, args.n_samples),
        ("negative_test", NEGATIVE_PHRASES, args.n_samples_val),
    ]

    for split, phrases, total in plan:
        edge_count = 0 if args.skip_edge else int(total * EDGE_FRACTION)
        piper_count = total - edge_count
        print(f"[{split}] {piper_count} piper + {edge_count} edge")

        _generate_piper(out_root / split, phrases, piper_count, voices, rng)
        if edge_count:
            asyncio.run(_generate_edge(out_root / split, phrases, edge_count, rng))

    print("\nDone. Clip counts:")
    for split in SPLITS:
        print(f"  {split}: {len(list((out_root / split).glob('*.wav')))}")
    print(
        "\nNext: add your own recordings with tools/record_wakeword_clips.py,"
        f"\nthen zip {out_root}/ and upload it to Colab."
    )


if __name__ == "__main__":
    main()
