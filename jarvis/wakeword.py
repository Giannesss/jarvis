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

from jarvis.config import WAKE_MODEL_PATH, WAKE_THRESHOLD, WAKE_WORD_ENGINE

# A name that matches no pretrained model, so download_models() fetches only
# the shared feature models (see _get_model) and no wake-word model at all.
_NO_PRETRAINED = "__custom__"

_model: Any | None = None
_score_key: str | None = None


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


def _detect_openwakeword(frame: bytes) -> bool:
    import numpy as np

    model = _get_model()
    audio = np.frombuffer(frame, dtype=np.int16)
    scores = model.predict(audio)
    return scores.get(_score_key, 0.0) >= WAKE_THRESHOLD


# Add other engines here later, same idiom as brain.py's _PROVIDERS.
_ENGINES = {
    "openwakeword": _detect_openwakeword,
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
    return _ENGINES[WAKE_WORD_ENGINE](frame)
