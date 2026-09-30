import time
from typing import Iterator

import ollama

from jarvis import diag
from jarvis.config import (
    ANTHROPIC_API_KEY,
    BRAIN_PROVIDER,
    CLAUDE_FALLBACK_OLLAMA,
    CLAUDE_MODEL,
    CLAUDE_RESEARCH_MODEL,
    OLLAMA_MODEL,
    RESEARCH_MAX_SEARCHES,
    RESEARCH_MAX_TOKENS,
)

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


# --- The Claude brain -------------------------------------------------------
#
# Two entry points, the same two Ollama has (_ask_claude / _stream_claude), so
# ask() and Turn never learn which brain answered. What is genuinely different
# is the message format, and that difference is one pure function.

# What "the token cap cut this off mid-sentence" is called here. Ollama got
# there first and Turn.truncated reads its word, so the Messages API's
# "max_tokens" is translated at this provider's edge rather than downstream.
STOP_TRUNCATED = "length"

# Printed when a turn is answered by the local model instead. Announced, never
# silent -- see _may_fall_back().
CLAUDE_FALLBACK_NOTICE = "Το Claude δεν απάντησε ({reason}), χρήση τοπικού μοντέλου."

# HTTP statuses worth trying the local model for: a timeout, a conflict, a rate
# limit, or the API itself being unwell.
_TRANSIENT_STATUSES = frozenset({408, 409, 429})

# The SDK failures that carry no status, because nothing reached Anthropic at
# all. Matched by module and class *name* rather than by class, so this module
# never has to import `anthropic` in order to classify an error -- which is what
# lets the test suite (and anyone on the default BRAIN_PROVIDER) run with the
# SDK not installed at all.
_TRANSIENT_ERROR_NAMES = frozenset(
    {"APIConnectionError", "APITimeoutError", "APIConnectionTimeoutError"}
)

# Built on first use, not at import. With BRAIN_PROVIDER=ollama the SDK is never
# needed, and importing this module has to stay cheap -- skills.py pulls it in
# transitively. Same lazy-singleton idiom as speaker._get_voice() and
# listener._get_model(), with the import deferred alongside it for the same
# reason.
_client = None


def _get_client():
    global _client

    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    return _client


def _to_messages_api(messages: list[dict]) -> tuple[str, list[dict]]:
    """Ollama's flat message list, in the shape the Messages API wants.

    Ollama accepts a system message anywhere in the list. The Messages API takes
    one top-level `system` and a `messages` list that must not carry the role at
    all -- a mid-conversation system message is an Opus 5 / 4.8 feature and a
    400 on Haiku 4.5. That matters because it is exactly where _build_messages()
    splices this turn's recalled memory.

    So every system message is joined into `system`, in order: SYSTEM_PROMPT
    first, then the transient memory block. That is the right place for it
    rather than a workaround -- MEMORY_PREAMBLE exists precisely so the model
    reads recalled facts as background it already knows instead of as something
    the user just said.

    Pure, and it never touches _history: what it is handed is the list
    _build_messages() already returned, so the frozen-prefix and
    transient-memory contract that ask() and Turn rest on is untouched.
    """
    system = "\n\n".join(
        message["content"] for message in messages if message["role"] == "system"
    )
    turns = [message for message in messages if message["role"] != "system"]
    return system, turns


def _normalize_stop(stop_reason: str | None) -> str | None:
    return STOP_TRUNCATED if stop_reason == "max_tokens" else stop_reason


def _usage(usage) -> str:
    """Token counts for the diagnostic log, tolerant of missing fields.

    Same role as _stage() on the Ollama side: a slow turn is either a large
    prompt or a long generation, and the two have different fixes. Counts and
    timings only -- the log holds no transcribed text, whichever brain answered.
    """
    sent = getattr(usage, "input_tokens", None)
    back = getattr(usage, "output_tokens", None)
    return (
        f"in {sent if sent is not None else '?'} tok, "
        f"out {back if back is not None else '?'} tok"
    )


def _is_transient(error: Exception) -> bool:
    """Whether this failure is worth answering from the local model instead.

    The asymmetry is the whole rule. A transient failure -- no internet, a rate
    limit, a 5xx -- is exactly what the roadmap's offline fallback is for. A 401
    or a 400 is not: it will never fix itself, and answering from qwen3 anyway
    would mean every reply quietly comes from the local model while you believe
    you are talking to Claude. That is a wrong answer nobody can see, which is
    the expensive kind; a spoken error is the cheap one.

    So anything unclassifiable is deliberately *not* transient -- a bug of our
    own that never reached the network included.
    """
    status = getattr(error, "status_code", None)
    if status is not None:
        return status in _TRANSIENT_STATUSES or status >= 500

    return (
        type(error).__module__.split(".")[0] == "anthropic"
        and type(error).__name__ in _TRANSIENT_ERROR_NAMES
    )


def _may_fall_back(error: Exception) -> bool:
    """Decide, and say so on the terminal when the answer is yes.

    The announcement is not decoration: speaker.py prints «Edge TTS απέτυχε ...,
    χρήση Piper.» for the same reason. A fallback nobody is told about is a
    different assistant answering under Claude's name.
    """
    if not (CLAUDE_FALLBACK_OLLAMA and _is_transient(error)):
        return False

    reason = type(error).__name__
    print(CLAUDE_FALLBACK_NOTICE.format(reason=reason))
    diag.log(f"[timing] Claude failed ({reason}), answering from Ollama")
    return True


def _ask_claude(messages: list[dict]) -> str:
    """One whole reply from the Messages API. Same contract as _ask_ollama.

    Three things are deliberately *not* sent, and the reason is the same for all
    three: CLAUDE_MODEL is a knob, and a request that is only valid for today's
    value of it is a trap.

      * `temperature` -- removed on Sonnet 5 and Opus 5, a 400 there. The prompt
        already does this job (SYSTEM_PROMPT asks for one or two short
        sentences), which is why the Ollama path's TEMPERATURE has no twin here.
      * `thinking` -- omitted, which on Haiku 4.5 means none at all. Naming it
        would have to be model-specific: the 5-series reads an absent parameter
        as adaptive thinking, and a 120-token spoken reply has no latency budget
        for reasoning.
      * `output_config` -- its `effort` is rejected outright on Haiku 4.5.

    MAX_REPLY_TOKENS carries over unchanged: same cap, same reason, and the same
    _trim_to_last_sentence() when it bites.
    """
    system, turns = _to_messages_api(messages)

    t0 = time.perf_counter()
    try:
        response = _get_client().messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_REPLY_TOKENS,
            system=system,
            messages=turns,
        )
    except Exception as error:
        if not _may_fall_back(error):
            raise
        return _ask_ollama(messages)

    diag.log(
        f"[timing] Claude response: {time.perf_counter() - t0:.2f}s "
        f"({_usage(getattr(response, 'usage', None))})"
    )

    # Never content[0].text: a reply can arrive as several text blocks, and a
    # non-text block among them has to be skipped rather than crashed on.
    reply = "".join(block.text for block in response.content if block.type == "text")
    if _normalize_stop(response.stop_reason) == STOP_TRUNCATED:
        reply = _trim_to_last_sentence(reply)
    return reply


# --- Research (Phase 5 step 1) ----------------------------------------------
#
# A one-off lookup, not a conversational turn: no history, no memory block,
# and never touches _history. Always the Claude API, whatever BRAIN_PROVIDER
# is set to -- this is the one place "δεν έχεις πρόσβαση στο ίντερνετ" in
# SYSTEM_PROMPT stops being true (see CLAUDE.md "Grounding"), and Ollama has
# nothing to fall back to, unlike _ask_claude/_stream_claude. A missing key is
# therefore a per-*call* refusal here rather than the import-time failure
# BRAIN_PROVIDER=claude gets from _check_key(): research is opt-in on its own.


class ResearchUnavailable(Exception):
    """Raised before reaching the client at all (no key) or when a search
    came back with nothing to say, so the research skill can speak a plain
    Greek reason instead of crashing on a bare SDK error."""


RESEARCH_SYSTEM_PROMPT = (
    "Κάνεις έρευνα στο διαδίκτυο για τον χρήστη πάνω σε ένα θέμα. "
    "Χρησιμοποίησε το εργαλείο αναζήτησης όσες φορές χρειάζεται. Στο τέλος "
    "γράψε ΜΟΝΟ μια δομημένη περίληψη στα Ελληνικά, 3 έως 6 σύντομες "
    "προτάσεις με τα πιο σημαντικά και πιο πρόσφατα ευρήματα -- σκέτες "
    "προτάσεις σαν προφορικός λόγος, όχι λίστα με παύλες ή markdown. Μην "
    "γράψεις τίποτα πριν ή μετά την περίληψη -- όχι «θα ψάξω για...», όχι "
    "«ελπίζω να βοήθησε» -- πήγαινε κατευθείαν στα ευρήματα."
)


def _final_text(content) -> str:
    """The summary itself, not the "I'll search for..." preambles the model
    writes between search calls.

    Everything *after* the last web_search_tool_result block is what the
    model wrote once it had all its evidence in hand; text before that is it
    thinking out loud about what to search next, not the structured summary
    the caller asked for. Falls back to every text block when there was no
    search result at all (index stays -1), so a reply that never searched is
    still returned rather than silently dropped.
    """
    last_result = -1
    for i, block in enumerate(content):
        if getattr(block, "type", None) == "web_search_tool_result":
            last_result = i

    parts = [
        block.text
        for i, block in enumerate(content)
        if i > last_result and getattr(block, "type", None) == "text"
    ]
    return "\n".join(part.strip() for part in parts if part and part.strip())


def research(topic: str) -> str:
    """Run several web searches on `topic` and return a structured summary.

    web_search is a *server* tool: Anthropic runs the searches and feeds the
    results back to the model inside this one request, so there is no
    client-side tool-use loop to drive here, unlike a tool this process would
    have to execute itself.
    """
    if not ANTHROPIC_API_KEY.strip():
        raise ResearchUnavailable(
            "Χρειάζομαι ένα ANTHROPIC_API_KEY στο .env για να ψάξω στο "
            "διαδίκτυο."
        )

    t0 = time.perf_counter()
    response = _get_client().messages.create(
        model=CLAUDE_RESEARCH_MODEL,
        max_tokens=RESEARCH_MAX_TOKENS,
        system=RESEARCH_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Θέμα έρευνας: {topic}"}],
        tools=[
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": RESEARCH_MAX_SEARCHES,
            }
        ],
    )
    diag.log(
        f"[timing] Claude research: {time.perf_counter() - t0:.2f}s "
        f"({_usage(getattr(response, 'usage', None))})"
    )

    summary = _final_text(response.content)
    if not summary:
        raise ResearchUnavailable("Η έρευνα δεν επέστρεψε κάτι χρήσιμο.")
    return summary


# Add an "openai" entry here later (not implemented yet).
_PROVIDERS = {
    "ollama": _ask_ollama,
    "claude": _ask_claude,
}

if BRAIN_PROVIDER not in _PROVIDERS:
    raise ValueError(
        f"Unknown BRAIN_PROVIDER {BRAIN_PROVIDER!r}; expected one of "
        f"{sorted(_PROVIDERS)}. Check your .env file."
    )


def _check_key(provider: str, api_key: str) -> None:
    """A missing key is a startup failure, not a per-question one.

    Same reasoning as the unknown-BRAIN_PROVIDER check above, and the same
    place: raising here happens at import, which is startup, before main()'s
    per-request handling can turn it into a spoken «δεν μπορώ να απαντήσω» once
    per question for the rest of the run. Checking a string needs no SDK, so
    this lands well before _get_client() would import one.
    """
    if provider == "claude" and not api_key.strip():
        raise ValueError(
            "BRAIN_PROVIDER=claude needs ANTHROPIC_API_KEY, which is empty or "
            "unset. Add it to your .env file (see .env.example), or set "
            "BRAIN_PROVIDER=ollama to use the local model."
        )


_check_key(BRAIN_PROVIDER, ANTHROPIC_API_KEY)


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


def _stream_claude(messages: list[dict], state: dict) -> Iterator[str]:
    """Yield the reply in deltas, reporting how it ended in `state`.

    Same contract as _stream_ollama, out-of-band stop reason included -- a
    generator's return value is invisible to a `for` loop.

    The fallback is narrower here than in _ask_claude, and deliberately so: it
    is allowed only *before the first delta*. Once a word has reached the
    speakers there is no way to start over on the local model without saying it
    twice, so a failure mid-reply stays a failure -- Turn.deltas() drops the
    turn and main._stream_reply() speaks its error line, which is exactly what
    happens today when Ollama dies mid-stream.
    """
    system, turns = _to_messages_api(messages)

    t0 = time.perf_counter()
    first_at: float | None = None
    final = None

    try:
        with _get_client().messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=MAX_REPLY_TOKENS,
            system=system,
            messages=turns,
        ) as stream:
            for delta in stream.text_stream:
                if not delta:
                    continue
                if first_at is None:
                    first_at = time.perf_counter() - t0
                yield delta

            final = stream.get_final_message()
    except Exception as error:
        if first_at is not None or not _may_fall_back(error):
            raise
        # _stream_ollama sets state["done_reason"] itself, so the caller still
        # learns how this reply ended -- from whichever brain produced it.
        yield from _stream_ollama(messages, state)
        return

    state["done_reason"] = _normalize_stop(getattr(final, "stop_reason", None))

    # First token on its own, for the reason the Ollama line keeps it there: it
    # is the number streaming exists to move, and the only one that says whether
    # a reply felt immediate.
    first = f"{first_at:.2f}s" if first_at is not None else "never"
    diag.log(
        f"[timing] Claude stream: first token {first}, "
        f"done {time.perf_counter() - t0:.2f}s "
        f"({_usage(getattr(final, 'usage', None))})"
    )


# Add an "openai" entry here as it gains streaming, same idiom as _PROVIDERS. A
# provider absent from this dict is not a failure: start_turn() is simply never
# used for it and main.py keeps the whole-reply path.
_STREAM_PROVIDERS = {
    "ollama": _stream_ollama,
    "claude": _stream_claude,
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
