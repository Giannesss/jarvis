import time

import ollama

from jarvis.config import OLLAMA_MODEL

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


def ask(user_text: str) -> str:
    _history.append({"role": "user", "content": user_text})

    t0 = time.perf_counter()
    response = ollama.chat(
        model=OLLAMA_MODEL,
        messages=_history,
        think=False,
        keep_alive=KEEP_ALIVE,
        options={"num_predict": MAX_REPLY_TOKENS},
    )
    print(f"[timing] Ollama response: {time.perf_counter() - t0:.2f}s")
    reply = response["message"]["content"]

    _history.append({"role": "assistant", "content": reply})
    _history[:] = [_history[0]] + _history[1:][-MAX_HISTORY_MESSAGES:]
    return reply
