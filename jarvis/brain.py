import time

import ollama

from jarvis.config import OLLAMA_MODEL

SYSTEM_PROMPT = (
    "You are Jarvis, a concise and helpful voice assistant. "
    "Keep replies short and conversational, since they will be spoken aloud. "
    "Respond in the same language the user speaks to you in. "
    "Answer directly without showing your reasoning process."
)

# Keep the model resident in Ollama between requests instead of unloading
# after the default 5-minute idle timeout.
KEEP_ALIVE = "30m"

# Hard cap on generated tokens so replies stay short enough to speak aloud.
MAX_REPLY_TOKENS = 200

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
    return reply
