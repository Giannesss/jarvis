"""Cutting a token stream into pieces worth speaking.

Pure: no clock, no network, no I/O. Driven from a list in
tests/test_chunker.py, the same way listener._StopDecider is.

**Why this is not just "split on full stops".** Measured on this machine,
warm, qwen3:8b:

    first token            0.10 - 0.81s
    first sentence done    5.42s, 6.09s
    whole reply done       6.17s, 7.96s

In the second of those the first sentence *was* the whole reply -- 0.08s
apart -- because SYSTEM_PROMPT asks for "μία ή δύο σύντομες προτάσεις" and
the model obeys. Splitting on sentences alone therefore buys almost nothing
on the replies this assistant actually produces: the Edge TTS call moves off
the critical path (~0.5s) and that is all.

So the first chunk is allowed to be smaller than a sentence -- broken at a
comma once it is long enough to be worth saying -- and every chunk after it
is a whole sentence. That gets the first words out at roughly 2-3s instead
of 6s. It costs prosody at exactly one seam, once per reply, which is why it
is only ever the *first* chunk and why STREAM_EARLY_FIRST_CHUNK can turn it
off.

Three things the splitter has to get right, all of them things Whisper and
the model will actually produce:

  * **Both semicolons.** brain._SENTENCE_ENDINGS holds U+037E, the formal
    Greek question mark. Almost nobody types it and models emit U+003B, the
    ASCII semicolon, instead. There it cost a missed trim; here it would
    cost a missed chunk boundary, which is audible.
  * **Decimals are not sentences.** "3.5 βαθμοί" must not break after the 3.
  * **Abbreviations are not sentences.** "π.χ." and "κ.λπ." end in a full
    stop that ends nothing.
"""

from __future__ import annotations

from jarvis.config import (
    STREAM_EARLY_FIRST_CHUNK,
    STREAM_FIRST_CHUNK_MAX_CHARS,
    STREAM_FIRST_CHUNK_MIN_CHARS,
    STREAM_MIN_CHUNK_CHARS,
)

# Sentence ends. Both semicolons on purpose (see the module docstring), plus
# the ano teleia, which is a real sentence break in Greek and is absent from
# brain._SENTENCE_ENDINGS.
SENTENCE_ENDINGS = ".!?…;;·"

# Breaks the *first* chunk may additionally use, to get audio out sooner.
CLAUSE_ENDINGS = ",·"

# Abbreviations whose trailing full stop ends nothing. Matched against the
# last whitespace-delimited token, lower-cased and accent-bearing as written:
# these are compared literally, not through text.normalize(), because they
# are being matched against model output rather than against speech.
ABBREVIATIONS = frozenset(
    {
        "π.χ.",
        "κ.λπ.",
        "κλπ.",
        "κ.ά.",
        "κ.α.",
        "κ.τ.λ.",
        "κ.ο.κ.",
        "δηλ.",
        "βλ.",
        "σελ.",
        "αρ.",
        "κα.",
        "κ.",
        "δρ.",
        "μ.μ.",
        "π.μ.",
        "μ.χ.",
        "π.χ",
    }
)


def _ends_with_abbreviation(text: str) -> bool:
    """True if text ends in an abbreviation's full stop rather than a
    sentence's."""
    tail = text.rsplit(None, 1)[-1] if text.split() else ""
    if not tail:
        return False
    if tail.lower() in ABBREVIATIONS:
        return True
    # A token carrying an interior dot ("κ.τ.λ", "Ο.Τ.Ε.") is an abbreviation
    # whatever it is, and a single letter before a dot ("Α.") is an initial.
    core = tail.rstrip(".")
    return "." in core or len(core) == 1


def _is_boundary(text: str, index: int) -> bool:
    """Is text[index] the end of a sentence, given everything before it?"""
    char = text[index]
    if char not in SENTENCE_ENDINGS:
        return False

    if char == ".":
        before = text[index - 1] if index else ""
        after = text[index + 1] if index + 1 < len(text) else ""
        if before.isdigit() and after.isdigit():
            return False  # 3.5
        if _ends_with_abbreviation(text[: index + 1]):
            return False

    # A run of the same mark ("...", "!!") ends once, at its last character.
    if index + 1 < len(text) and text[index + 1] == char:
        return False

    return True


class Chunker:
    """Accumulates deltas, hands back pieces worth synthesizing.

    feed() returns whatever became speakable; flush() returns the tail.
    """

    def __init__(
        self,
        early_first_chunk: bool | None = None,
        first_min: int | None = None,
        first_max: int | None = None,
        min_chars: int | None = None,
    ) -> None:
        self.early_first_chunk = (
            STREAM_EARLY_FIRST_CHUNK if early_first_chunk is None else early_first_chunk
        )
        self.first_min = STREAM_FIRST_CHUNK_MIN_CHARS if first_min is None else first_min
        self.first_max = STREAM_FIRST_CHUNK_MAX_CHARS if first_max is None else first_max
        self.min_chars = STREAM_MIN_CHUNK_CHARS if min_chars is None else min_chars

        self._buffer = ""
        self._emitted = 0

    @property
    def pending(self) -> str:
        """What is held but not yet speakable. Discarded on a truncated end."""
        return self._buffer

    def feed(self, delta: str) -> list[str]:
        if not delta:
            return []

        self._buffer += delta

        chunks: list[str] = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            piece, self._buffer = self._buffer[:cut].strip(), self._buffer[cut:].lstrip()
            if piece:
                chunks.append(piece)
                self._emitted += 1

        return chunks

    def flush(self, truncated: bool = False) -> str | None:
        """End of generation.

        `truncated` is the model hitting MAX_REPLY_TOKENS mid-sentence. The
        tail is dropped rather than spoken, which is exactly what
        brain._trim_to_last_sentence did for the non-streaming path -- and
        here it is free, because an unterminated tail was never emitted in
        the first place.
        """
        tail, self._buffer = self._buffer.strip(), ""
        if truncated or not tail:
            return None
        self._emitted += 1
        return tail

    # --- where to cut -----------------------------------------------------

    def _find_cut(self) -> int | None:
        """Index just past the end of the next speakable chunk, or None."""
        text = self._buffer

        for index in range(len(text)):
            if not _is_boundary(text, index):
                continue

            end = index + 1
            # Keep a closing quote or bracket with the sentence it ends.
            while end < len(text) and text[end] in '"»)]':
                end += 1

            if len(text[:end].strip()) >= self.min_chars:
                return end
            # Too short to be worth its own synthesis call -- «Ναι.» alone
            # would spend a 0.4s Edge round trip on one syllable and land
            # clipped. Merge it into the next sentence by looking further;
            # if none arrives, flush() speaks it at the end anyway.

        if self._emitted or not self.early_first_chunk:
            return None

        # The first chunk only: break early so speech can start before the
        # sentence is finished.
        if len(text) >= self.first_min:
            for index in range(self.first_min, len(text)):
                if text[index] in CLAUSE_ENDINGS:
                    return index + 1

        if len(text) >= self.first_max:
            cut = text.rfind(" ", 0, self.first_max)
            if cut > self.first_min:
                return cut

        return None
