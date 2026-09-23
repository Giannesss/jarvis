"""Wake-word detection providers. See CLAUDE.md "Providers" pattern and
jarvis/listener.py for how detect() is fed audio frames.

Only "openwakeword" is implemented. WAKE_MODEL_PATH picks which model it
listens for: either the name of a pretrained model (the default,
"hey_jarvis" — the English phrase "Hey Jarvis", not "Τζάρβις") or a path to
a custom .onnx model trained locally.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jarvis.config import (
    WAKE_DEBUG,
    WAKE_MODEL_PATH,
    WAKE_SCORE_FLOOR,
    WAKE_THRESHOLD,
    WAKE_WORD_ENGINE,
)

# A name that matches no pretrained model, so download_models() fetches only
# the shared feature models (see _get_model) and no wake-word model at all.
_NO_PRETRAINED = "__custom__"

_model: Any | None = None
_score_key: str | None = None

# Frames scored since the last reset(), printed beside each score under
# WAKE_DEBUG. The score alone cannot answer the question the log is asked
# most often — "was that one utterance or several?" — because a hit is a
# *ramp*, not a spike: measured on data/wake_probe.csv, every one of nine
# clean detections rose 0.03 -> 0.30 -> 0.71 -> 0.98 over 4-5 frames and
# spanned 11 frames (0.88s) above the debug floor. A climb is therefore the
# normal shape and says nothing on its own. Consecutive indices mean one
# utterance; a jump between them means silence the floor hid. (12.5 frames
# to the second; FRAME_SECONDS itself lives in listener.py, which imports
# this module, so it is not imported back.)
_frames_since_reset = 0


def _get_model() -> Any:
    global _model, _score_key

    if _model is None:
        # Deferred: numpy/openwakeword aren't required just to import this
        # module (e.g. via listener.py/main.py) when wake-word mode is off
        # or those packages aren't installed — only when actually used.
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        is_custom = Path(WAKE_MODEL_PATH).exists()

        # download_models() always fetches the shared melspectrogram/embedding
        # models that every wake-word model runs on top of; its argument only
        # selects which *pretrained* wake-word models to also fetch, and a name
        # matching none is silently skipped. So a custom model still needs this
        # call, just without asking for a pretrained model alongside it.
        download_models([_NO_PRETRAINED if is_custom else WAKE_MODEL_PATH])

        # Model() names each score by the file's stem when given a path, and by
        # the name itself when given a pretrained model name.
        _score_key = Path(WAKE_MODEL_PATH).stem if is_custom else WAKE_MODEL_PATH
        _model = Model(wakeword_models=[WAKE_MODEL_PATH], inference_framework="onnx")

    return _model


# Under WAKE_DEBUG, scores at or above this are printed even when they don't
# reach WAKE_THRESHOLD, so a threshold that's set too high is visible as
# "it scored 0.41" rather than as silence. WAKE_SCORE_FLOOR (0.001) replaces
# the 0.1 this used to be: the interesting failure scores ~0.000, not ~0.05,
# and 0.1 hid exactly the case worth seeing. See config.WAKE_SCORE_FLOOR.
#
# Four decimals, not three, for the same reason: at this floor the question
# is "0.0012 or 0.0400", which .3f rounds into the same-looking number.


def _detect_openwakeword(frame: bytes) -> bool:
    global _frames_since_reset

    import numpy as np

    model = _get_model()
    audio = np.frombuffer(frame, dtype=np.int16)
    scores = model.predict(audio)
    score = scores.get(_score_key, 0.0)
    _frames_since_reset += 1

    if WAKE_DEBUG and score >= WAKE_SCORE_FLOOR:
        hit = "HIT" if score >= WAKE_THRESHOLD else "   "
        print(
            f"[wake] {hit} {_score_key}={score:.4f} "
            f"f{_frames_since_reset} (threshold {WAKE_THRESHOLD})"
        )

    return score >= WAKE_THRESHOLD


def _reset_openwakeword() -> None:
    global _frames_since_reset

    _frames_since_reset = 0

    # Only if the model was actually loaded: resetting is pointless before
    # the first detect() call and would trigger the download/load early.
    if _model is not None:
        _model.reset()


# Add other engines here later, same idiom as brain.py's _PROVIDERS. Each
# entry is (detect, reset).
_ENGINES = {
    "openwakeword": (_detect_openwakeword, _reset_openwakeword),
}

if WAKE_WORD_ENGINE not in _ENGINES:
    raise ValueError(
        f"Unknown WAKE_WORD_ENGINE {WAKE_WORD_ENGINE!r}; expected one of "
        f"{sorted(_ENGINES)}. Check your .env file."
    )


def preload() -> None:
    """Load the wake-word model (and trigger its one-time download) now
    instead of on the first detect() call."""
    _get_model()


def detect(frame: bytes) -> bool:
    """frame must be one 2560-byte chunk: 1280 samples of 16-bit mono PCM
    at 16kHz (80ms). Returns True on the frame the wake word fires."""
    return _ENGINES[WAKE_WORD_ENGINE][0](frame)


def reset() -> None:
    """Clear the model's internal audio/feature buffer.

    openWakeWord scores each frame using the ~1.5s of audio before it, so a
    single spoken wake word keeps several consecutive frames above the
    threshold. Without this, the same utterance re-fires detection on the
    next listen_for_wake_word() call — flushing the audio queues isn't
    enough, because the buffer lives inside the model. Call it after every
    detection (and pair it with WAKE_RETRIGGER_COOLDOWN, which gives the
    emptied buffer time to refill with fresh audio)."""
    _ENGINES[WAKE_WORD_ENGINE][1]()
