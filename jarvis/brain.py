import time

import ollama

from jarvis.config import BRAIN_PROVIDER, OLLAMA_MODEL

SYSTEM_PROMPT = (
    "Είσαι ο Τζάρβις, ένας φιλικός φωνητικός βοηθός. Μιλάς στον κόσμο σαν "
    "άνθρωπος, όχι σαν μηχανή: απαντάς σε ένα ή δύο σύντομες προτάσεις, σαν "
    "να μιλάς φυσικά. Μην αναφέρεις ποτέ 'τον χρήστη' ή 'το σύστημα', και "
    "μην περιγράφεις τον εαυτό σου ως τεχνητή νοημοσύνη εκτός αν σε ρωτήσουν "
    "ρητά. Μην γράφεις λίστες ή markdown, μιλάς σκέτο κείμενο όπως στον "
    "προφορικό λόγο. Κάνε μια σύντομη διευκρινιστική ερώτηση μόνο όταν "
    "πραγματικά χρειάζεσαι περισσότερες πληροφορίες για να απαντήσεις."
)

# Few-shot examples showing the tone we want. Kept out of history trimming
# (see PREFIX_LEN) so they always stay in context.
FEW_SHOT_EXAMPLES: list[dict] = [
    {"role": "user", "content": "Γεια σου Τζάρβις"},
    {"role": "assistant", "content": "Γεια σου! Τι κάνεις;"},
    {"role": "user", "content": "Ποια είναι η πρωτεύουσα της Αυστραλίας;"},
    {"role": "assistant", "content": "Η Καμπέρα, όχι το Σίδνεϊ όπως πολλοί νομίζουν."},
    {"role": "user", "content": "Τι μπορείς να κάνεις;"},
    {
        "role": "assistant",
        "content": "Μπορώ να σου απαντάω σε ερωτήσεις και να κουβεντιάζουμε, όλα με φωνή.",
    },
    {"role": "user", "content": "Στείλε μου email στη μαμά μου"},
    {
        "role": "assistant",
        "content": "Αυτό δεν μπορώ να το κάνω ακόμα, δεν έχω πρόσβαση σε email.",
    },
]

# Keep the model resident in Ollama between requests instead of unloading
# after the default 5-minute idle timeout.
KEEP_ALIVE = "30m"

# Hard cap on generated tokens so replies stay short enough to speak aloud.
MAX_REPLY_TOKENS = 120

# Moderate temperature: some natural variation without rambling off-topic.
TEMPERATURE = 0.5

# How many non-system, non-few-shot messages to keep, so history can't grow
# unbounded.
MAX_HISTORY_MESSAGES = 6

# Sentence-ending punctuation, including the Greek question mark (U+037E),
# used to trim a reply that got cut off mid-sentence by MAX_REPLY_TOKENS.
_SENTENCE_ENDINGS = ".!?…;"

_history: list[dict] = (
    [{"role": "system", "content": SYSTEM_PROMPT}] + FEW_SHOT_EXAMPLES
)

# Messages before this index (system prompt + few-shot examples) are never
# trimmed from history.
_PREFIX_LEN = len(_history)


def _trim_to_last_sentence(text: str) -> str:
    last_end = max((text.rfind(ch) for ch in _SENTENCE_ENDINGS), default=-1)
    if last_end != -1:
        return text[: last_end + 1].strip()
    # No sentence end: the cap cut the model off mid-word. Drop that partial
    # trailing word, preferring to cut back further to the last comma (a
    # cleaner break) if one is present, then close the sentence.
    trimmed = text.rsplit(" ", 1)[0] if " " in text else ""
    last_comma = trimmed.rfind(",")
    if last_comma != -1:
        trimmed = trimmed[:last_comma]
    return trimmed.strip() + "."


def _ask_ollama(messages: list[dict]) -> str:
    t0 = time.perf_counter()
    response = ollama.chat(
        model=OLLAMA_MODEL,
        messages=messages,
        think=False,
        keep_alive=KEEP_ALIVE,
        options={"num_predict": MAX_REPLY_TOKENS, "temperature": TEMPERATURE},
    )
    print(f"[timing] Ollama response: {time.perf_counter() - t0:.2f}s")
    reply = response["message"]["content"]
    if response.get("done_reason") == "length":
        reply = _trim_to_last_sentence(reply)
    return reply


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
    _history[:] = _history[:_PREFIX_LEN] + _history[_PREFIX_LEN:][-MAX_HISTORY_MESSAGES:]
    return reply
