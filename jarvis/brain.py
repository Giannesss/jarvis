import ollama

from jarvis.config import OLLAMA_MODEL

SYSTEM_PROMPT = (
    "You are Jarvis, a concise and helpful voice assistant. "
    "Keep replies short and conversational, since they will be spoken aloud. "
    "Respond in the same language the user speaks to you in. "
    "Answer directly without showing your reasoning process."
)

_history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]


def ask(user_text: str) -> str:
    _history.append({"role": "user", "content": user_text})

    response = ollama.chat(model=OLLAMA_MODEL, messages=_history)
    reply = response["message"]["content"]

    _history.append({"role": "assistant", "content": reply})
    return reply
