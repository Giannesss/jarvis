"""Wake-word detection providers. See CLAUDE.md "Providers" pattern and
jarvis/listener.py for how detect() is fed audio frames.

Only "openwakeword" is implemented (its pretrained "hey_jarvis" model — the
English phrase "Hey Jarvis", not "Τζάρβις"; see the wake-word plan for why).
"""

from __future__ import annotations

from typing import Any

from jarvis.config import WAKE_THRESHOLD, WAKE_WORD_ENGINE

_WAKEWORD_NAME = "hey_jarvis"

_model: Any | None = None


def _get_model() -> Any:
    global _model

    if _model is None:
        # Deferred: numpy/openwakeword aren't required just to import this
        # module (e.g. via listener.py/main.py) when wake-word mode is off
        # or those packages aren't installed — only when actually used.
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        download_models([_WAKEWORD_NAME])
        _model = Model(wakeword_models=[_WAKEWORD_NAME], inference_framework="onnx")

    return _model


def _detect_openwakeword(frame: bytes) -> bool:
    import numpy as np

    model = _get_model()
    audio = np.frombuffer(frame, dtype=np.int16)
    scores = model.predict(audio)
    return scores.get(_WAKEWORD_NAME, 0.0) >= WAKE_THRESHOLD


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
