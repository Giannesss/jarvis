import time
from typing import Iterator

import ollama

from jarvis import diag
from jarvis.config import BRAIN_PROVIDER, OLLAMA_MODEL

SYSTEM_PROMPT = (
    "Είσαι ο Τζάρβις, ένας φιλικός φωνητικός βοηθός. Μιλάς στον κόσμο σαν "
    "άνθρωπος, όχι σαν μηχανή: απαντάς σε ένα ή δύο σύντομες προτάσεις, σαν "
    "να μιλάς φυσικά. Μην αναφέρεις ποτέ 'τον χρήστη' ή 'το σύστημα', και "
    "μην περιγράφεις τον εαυτό σου ως τεχνητή νοημοσύνη εκτός αν σε ρωτήσουν "
    "ρητά. Μην γράφεις λίστες ή markdown, μιλάς σκέτο κείμενο όπως στον "
    "προφορικό λόγο. Κάνε μια σύντομη διευκρινιστική ερώτηση μόνο όταν "
    "πραγματικά χρειάζεσαι περισσότερες πληροφορίες για να απαντήσεις."
    "\n\n"
    "Λες μόνο όσα ξέρεις στ' αλήθεια. Για τη ζωή, το πρόγραμμα, τις σχολές, "
    "τα μαθήματα, τις εργασίες, τις υποχρεώσεις και τις επιχειρήσεις αυτού "
    "που σου μιλάει, επιτρέπεται να πεις μόνο όσα υπάρχουν στα όσα θυμάσαι ή "
    "όσα σου είπε ο ίδιος μέσα σε αυτή τη συζήτηση. Μην βγάζεις ποτέ από το "
    "μυαλό σου ονόματα, ημερομηνίες, ώρες, προθεσμίες, νούμερα, τζίρους ή "
    "άλλες λεπτομέρειες, ούτε καν σαν παράδειγμα ή σαν υπόθεση. "
    "Δεν έχεις πρόσβαση στο ίντερνετ: δεν ξέρεις τον καιρό, τις ειδήσεις, "
    "τιμές, email, μηνύματα ή οτιδήποτε συμβαίνει αυτή τη στιγμή έξω. "
    "Αν σε ρωτήσουν κάτι που δεν το ξέρεις ή δεν στο έχουν πει, πες το "
    "καθαρά και σύντομα — «δεν το ξέρω αυτό», «δεν μου το έχεις πει», «δεν "
    "έχω πρόσβαση σε αυτό» — και σταμάτα εκεί, χωρίς να μαντέψεις. Είναι "
    "πάντα καλύτερο να πεις ότι δεν ξέρεις, παρά να πεις κάτι που απλώς "
    "ακούγεται σωστό. Γενικές γνώσεις για τον κόσμο μπορείς να τις λες "
    "κανονικά."
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
    {"role": "user", "content": "Τι καιρό θα κάνει αύριο;"},
    {
        "role": "assistant",
        "content": "Δεν έχω πρόσβαση στον καιρό, οπότε δεν μπορώ να σου πω.",
    },
    {"role": "user", "content": "Πόσα πούλησε το μαγαζί μου τον Μάρτιο;"},
    {
        "role": "assistant",
        "content": "Δεν το ξέρω αυτό, δεν μου το έχεις πει ποτέ.",
    },
]

# Prefixed to the recalled memory block so the model treats it as background
# it already knows, rather than as something the user just said.
MEMORY_PREAMBLE = (
    "Τι θυμάσαι για αυτόν που σου μιλάει (χρησιμοποίησέ το μόνο αν βοηθάει, "
    "μην το απαριθμείς):"
)

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


def _stage(response, count_key: str, duration_key: str) -> str:
    """One Ollama stage as "<n> tok / <s>s", tolerant of missing fields.

    Counters are reported per stage because a slow turn is either a large
    prompt being re-prefilled or a long generation, and the two have
    different fixes. Prefill is the one that grows silently: brain.ask()
    splices a fresh memory block in after the frozen prefix every turn, so
    everything after it misses Ollama's KV cache and is prefilled again.
    """
    count = response.get(count_key)
    tokens = f"{count} tok" if count is not None else "? tok"
    ns = response.get(duration_key)
    return f"{tokens} / {ns / 1e9:.2f}s" if ns else tokens


def _ask_ollama(messages: list[dict]) -> str:
    t0 = time.perf_counter()
    response = ollama.chat(
        model=OLLAMA_MODEL,
        messages=messages,
        think=False,
        keep_alive=KEEP_ALIVE,
        options={"num_predict": MAX_REPLY_TOKENS, "temperature": TEMPERATURE},
    )
    diag.log(
        f"[timing] Ollama response: {time.perf_counter() - t0:.2f}s "
        f"(prompt {_stage(response, 'prompt_eval_count', 'prompt_eval_duration')}"
        f", gen {_stage(response, 'eval_count', 'eval_duration')})"
    )
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


def _build_messages(user_text: str, memory_block: str | None) -> list[dict]:
    """Append the user turn to history and splice in this turn's memory.

    The memory block is a *transient* system message just after the frozen
    prefix, and deliberately never appended to _history: remembered context is
    rebuilt fresh every turn, so stale recalls can't pile up and the history
    trim can't silently drop half of one.
    """
    _history.append({"role": "user", "content": user_text})

    if not memory_block:
        return _history

    return (
        _history[:_PREFIX_LEN]
        + [{"role": "system", "content": f"{MEMORY_PREAMBLE}\n{memory_block}"}]
        + _history[_PREFIX_LEN:]
    )


def _trim_history() -> None:
    _history[:] = _history[:_PREFIX_LEN] + _history[_PREFIX_LEN:][-MAX_HISTORY_MESSAGES:]


def ask(user_text: str, memory_block: str | None = None) -> str:
    """One whole reply, synthesized and spoken only once it is finished.

    Still the path when STREAM_REPLIES is off, and the path every skill-free
    non-streaming caller takes. See start_turn() for the streamed one.
    """
    messages = _build_messages(user_text, memory_block)

    try:
        reply = _PROVIDERS[BRAIN_PROVIDER](messages)
    except Exception:
        # As far as the conversation is concerned, this turn never happened.
        # Leaving the user message behind would stack unanswered user turns
        # across a run of failures -- Ollama's intermittent CUDA error does
        # exactly that -- and the next successful call would read them as one
        # run-on question. The caller reports the error; history stays clean.
        _history.pop()
        raise

    _history.append({"role": "assistant", "content": reply})
    _trim_history()
    return reply


def _stream_ollama(messages: list[dict], state: dict) -> Iterator[str]:
    """Yield the reply in deltas, reporting how it ended in `state`.

    The stop reason has to come back out of band: a generator's return value is
    invisible to a `for` loop, and the caller needs "did the token cap cut this
    off mid-sentence" to decide whether to speak the tail at all.
    """
    t0 = time.perf_counter()
    first_at: float | None = None
    final = None

    for part in ollama.chat(
        model=OLLAMA_MODEL,
        messages=messages,
        think=False,
        stream=True,
        keep_alive=KEEP_ALIVE,
        options={"num_predict": MAX_REPLY_TOKENS, "temperature": TEMPERATURE},
    ):
        delta = part["message"]["content"] or ""
        if delta and first_at is None:
            first_at = time.perf_counter() - t0
        if part.get("done"):
            final = part
        if delta:
            yield delta

    state["done_reason"] = final.get("done_reason") if final is not None else None

    # First token on its own line, because it is the number this whole step
    # exists to move: the non-streaming path could only ever report when the
    # *last* token arrived, and the first one is what decides whether a reply
    # feels immediate. Both clocks are kept, same discipline as the recording
    # timing line.
    first = f"{first_at:.2f}s" if first_at is not None else "never"
    counters = (
        f" (prompt {_stage(final, 'prompt_eval_count', 'prompt_eval_duration')}"
        f", gen {_stage(final, 'eval_count', 'eval_duration')})"
        if final is not None
        else ""
    )
    diag.log(
        f"[timing] Ollama stream: first token {first}, "
        f"done {time.perf_counter() - t0:.2f}s{counters}"
    )


# Add "claude" / "openai" entries here as they gain streaming, same idiom as
# _PROVIDERS. A provider absent from this dict is not a failure: start_turn()
# is simply never used for it and main.py keeps the whole-reply path.
_STREAM_PROVIDERS = {
    "ollama": _stream_ollama,
}


def streaming_available() -> bool:
    return BRAIN_PROVIDER in _STREAM_PROVIDERS


class Turn:
    """One streamed reply, and the history bookkeeping that goes with it.

    The difference from ask() is not the streaming, it is the accounting. A
    streamed reply can be cut off mid-word by a barge-in, so what the model
    generated and what the user actually heard are two different strings -- and
    the one that belongs in the history is the second. Asking the model to
    continue from a sentence nobody heard makes its next answer read as a reply
    to a question that was never answered.

    So a Turn is opened, streamed, and then *settled* exactly once:

        turn = brain.start_turn(text, memory)
        for delta in turn.deltas(): ...
        turn.commit(what_was_actually_spoken)   # or turn.abandon()

    Leaving one unsettled leaves a user message in history with no answer
    after it, which is the state ask()'s except clause exists to avoid.
    """

    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages
        self._state: dict = {}
        self._settled = False
        self.generated = ""

    @property
    def truncated(self) -> bool:
        """The token cap stopped it mid-sentence (see MAX_REPLY_TOKENS)."""
        return self._state.get("done_reason") == "length"

    def deltas(self) -> Iterator[str]:
        provider = _STREAM_PROVIDERS[BRAIN_PROVIDER]
        try:
            for delta in provider(self._messages, self._state):
                self.generated += delta
                yield delta
        except Exception:
            # Same reasoning as ask(): as far as the conversation is concerned
            # this turn never happened. The caller reports the error.
            self.abandon()
            raise

    def commit(self, spoken: str) -> None:
        """Record what was actually said. Nothing said means nothing happened.

        An empty `spoken` pops the user message too, rather than leaving it
        unanswered -- a reply interrupted before its first word is, to the
        conversation, a question that was never asked.
        """
        if self._settled:
            return
        self._settled = True

        text = spoken.strip()
        if not text:
            _history.pop()
            return

        _history.append({"role": "assistant", "content": text})
        _trim_history()

    def abandon(self) -> None:
        """Drop the turn entirely: no answer is coming."""
        if self._settled:
            return
        self._settled = True
        _history.pop()


def start_turn(user_text: str, memory_block: str | None = None) -> Turn:
    """Begin a streamed reply. See Turn for the contract."""
    return Turn(_build_messages(user_text, memory_block))
