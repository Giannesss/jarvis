import time

import ollama

from jarvis.config import BRAIN_PROVIDER, OLLAMA_MODEL

SYSTEM_PROMPT = (
    "You are Jarvis, a voice assistant. Always reply in simple, natural Greek, "
    "in at most two short sentences. No filler words, no pleasantries, no "
    "reasoning shown. Do not talk about yourself unless the user explicitly "
    "asks about you."
)

# Keep the model resident in Ollama between requests instead of unloading
# after the default 5-minute idle timeout.
KEEP_ALIVE = "30m"

# Hard cap on generated tokens so replies stay short enough to speak aloud.
MAX_REPLY_TOKENS = 80

# How many non-system messages to keep, so history can't grow unbounded.
MAX_HISTORY_MESSAGES = 6

_history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]


def _ask_ollama(messages: list[dict]) -> str:
    t0 = time.perf_counter()
    response = ollama.chat(
        model=OLLAMA_MODEL,
        messages=messages,
        think=False,
        keep_alive=KEEP_ALIVE,
        options={"num_predict": MAX_REPLY_TOKENS},
    )
    print(f"[timing] Ollama response: {time.perf_counter() - t0:.2f}s")
    return response["message"]["content"]


# Add "claude" / "openai" entries here later (not implemented yet).
_PROVIDERS = {
    "ollama": _ask_ollama,
}

if BRAIN_PROVIDER not in _PROVIDERS:
    raise ValueError(
        f"Unknown BRAIN_PROVIDER {BRAIN_PROVIDER!r}; expected one of "
        f"{sorted(_PROVIDERS)}. Check your .env file."
    )


def ask(user_text: str) -> str:
    _history.append({"role": "user", "content": user_text})

    reply = _PROVIDERS[BRAIN_PROVIDER](_history)

    _history.append({"role": "assistant", "content": reply})
    _history[:] = [_history[0]] + _history[1:][-MAX_HISTORY_MESSAGES:]
    return reply
