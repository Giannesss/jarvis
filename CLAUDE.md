# Jarvis

Local, offline Greek-speaking voice assistant. No cloud APIs, no API keys.
Voice output can optionally use Microsoft's online Edge TTS (falls back to
offline Piper if it's unavailable); everything else stays local.

## Run

```
.\.venv\Scripts\python.exe main.py
```

Requires Ollama running locally with the configured model pulled, and the Piper
voice model downloaded into `models/`. Enter at the prompt to record, `exit`/`quit` to leave.

## Pipeline (as implemented)

1. **Record** — `jarvis/listener.py` shells out to FFmpeg to capture `RECORD_SECONDS`
   (6s) from a hardcoded DirectShow mic (`MICROPHONE_NAME`) into a temp WAV.
2. **Speech-to-text** — same file, transcribes that WAV with `faster_whisper`
   (`base` model, CPU, int8, `language="el"`).
3. **LLM reply** — `jarvis/brain.py` sends the running chat history to a local
   Ollama model (`ollama.chat`) with a system prompt telling it to be concise
   and reply in whatever language the user used.
4. **Text-to-speech** — `jarvis/speaker.py` synthesizes the reply with Piper
   and plays it via `winsound`.

`main.py` wires these together in a loop, trying `jarvis/skills.py` first and
only falling back to the brain when a skill doesn't match (see "Skills").

## Files

- `main.py` — CLI loop tying listener → skills → brain → speaker together.
- `jarvis/listener.py` — FFmpeg recording + Whisper transcription.
- `jarvis/skills.py` — local Greek voice commands, handled without the LLM.
- `jarvis/brain.py` — Ollama chat call + conversation history + system prompt.
- `jarvis/speaker.py` — Piper TTS + playback.
- `jarvis/config.py` — loads `.env` (via `python-dotenv`) into `OLLAMA_MODEL` and `PIPER_MODEL_PATH`.
- `jarvis/text.py` — Greek normalization (see "Normalization") and the phrase
  data shared by `skills.py` and `memory.py` (see "Memory"). `skills.py`
  re-exports it under the old names, so `_normalize` and `SHUTDOWN_PHRASES`
  still work there.
- `jarvis/db.py` — SQLite schema, connections, backups.
- `jarvis/memory.py` — parsing speech into rows, and recalling rows as context.
- `jarvis/mem.py` — `python -m jarvis.mem`, also backing `:mem` at the prompt.
- `native_mic_test.py`, `wavein_capture_test.py`, `windows_capture_test.py` — throwaway
  experiments trying different Windows mic-capture APIs; not part of the app.

## Config

All settings live in `.env` (copy from `.env.example`): `OLLAMA_MODEL`, `PIPER_MODEL_PATH`.
The mic device name, record duration, and FFmpeg path are hardcoded constants at the
top of `jarvis/listener.py`.

## Providers

`brain.py` and `speaker.py` dispatch to a provider function based on
`BRAIN_PROVIDER` and `TTS_ENGINE` (both in `.env`, see `.env.example`).

- Brain: `BRAIN_PROVIDER` selects from `_PROVIDERS` in `jarvis/brain.py`.
  Only `"ollama"` is implemented. An unknown value raises `ValueError` at
  import time (startup), before the app's request-level error handling can
  swallow it. `"claude"` / `"openai"` are placeholders for later — adding
  them means a new function plus a new dict entry, no other changes.
- Voice: `TTS_ENGINE` selects from `_VOICE_PROVIDERS` in `jarvis/speaker.py`.
  Only `"edge"` is implemented there; `"piper"` (or any unrecognized value)
  isn't in the dict and is used directly as the always-available fallback.
  `"elevenlabs"` is a placeholder for later, added the same way.
  The Piper voice itself is a lazy singleton (`_get_voice()`), loaded on first
  use rather than at import — with `TTS_ENGINE=edge` it is only ever needed if
  Edge synthesis fails, and importing `speaker.py` (which `skills.py` does
  transitively) must stay cheap. Same idiom as `listener._get_model()` and
  `wakeword._get_model()`; the `piper` import is deferred alongside the load.
- Wake word: `WAKE_WORD_ENGINE` selects from `_ENGINES` in
  `jarvis/wakeword.py`. Only `"openwakeword"` is implemented; an unknown
  value raises `ValueError` at import time, same as `BRAIN_PROVIDER`.

No new dependencies or API-key handling have been added for the
placeholders — that's future work when one is actually implemented.

## Wake word

Off by default (`WAKE_WORD_ENABLED`). When on, `main.py` calls
`listener.start_stream()` once and then loops
`listen_for_wake_word()` → `acknowledge()` → `record_command()` instead of
prompting for Enter. Any failure starting or running detection falls back to
the Enter-press flow for the rest of the run, so `listen()` stays the
always-available path.

After a detection, `listen_for_wake_word()` calls `wakeword.reset()` and
ignores detections for `WAKE_RETRIGGER_COOLDOWN` (1s) while still feeding
frames. openWakeWord scores each frame from the ~1.5s of audio before it, so
one spoken wake word keeps several consecutive frames above the threshold;
draining the audio queues isn't enough, because that buffer lives inside the
model.

`acknowledge()` plays a short beep and then flushes both queues, so
`record_command()` starts from nothing and the wake word never reaches
Whisper. `WAKE_BEEP=false` restores the older pre-roll path instead: no beep,
no flush, and the ~1s captured before the trigger is prepended so words said
in the same breath as the wake word survive — at the cost of the wake word
being in the audio. Follow-up turns in conversation mode never use a pre-roll.

`record_command()` decides speech vs. silence from each 80ms frame's own RMS
level (`_frame_db()` vs `SILENCE_THRESHOLD_DB`), not from ffmpeg's
`silencedetect` lines. `silencedetect` only reports a silence after
`SILENCE_DURATION` of it has already passed, and the stream reader can only
stamp a line with its arrival time, so a natural pause right after the wake
word read as end-of-speech and ended the recording before the user spoke.
Measuring frames is drift-free and needs no clock rebasing. It waits for
speech to actually start (`SPEECH_ONSET_FRAMES` loud frames) and only then
stops on `SILENCE_DURATION` of continuous quiet. It returns
`(text, stop_reason)` — `"speech_end"`, `"no_speech"`, `"max"` or
`"stream_end"` — because the caller must tell "you finished talking" from
"you never started". `"no_speech"` returns without transcribing at all;
running Whisper on pure silence just invites it to hallucinate a phrase out
of room noise.

`WAKE_DEBUG=true` prints detection scores (including near-misses below the
threshold, for tuning), the mic level once a second while waiting for speech,
and why each recording stopped.

`WAKE_MODEL_PATH` picks the model and accepts two forms, because
openWakeWord's `Model()` already handles both:

- a **pretrained model name** (the default, `"hey_jarvis"`) — downloaded on
  first use, and the score is keyed by that name;
- a **path to a custom `.onnx` model** (e.g. `models/tzarvis.onnx` for the
  Greek "Τζάρβις") — the score is keyed by the file's stem instead.

`wakeword.py` derives `_score_key` the same way at load time. A custom model
must be `.onnx`, not `.tflite`, since `Model()` is constructed with
`inference_framework="onnx"`. A path that doesn't exist is treated as a
pretrained name and raises `ValueError`, which `main.py` catches into the
Enter-press fallback.

`download_models()` is called either way — it also fetches the shared
melspectrogram/embedding models that every wake-word model runs on top of.
For a custom model it's passed a name matching nothing, so it fetches those
shared models and no pretrained wake-word model.

`WAKE_THRESHOLD` (0-1) is the score cutoff. Expect a custom Greek model to
need tuning here: Greek has only four usable TTS voices to synthesize
training data from, so it won't be as robust as the pretrained models.

## Conversation mode

`CONVERSATION_MODE` (on by default, wake-word mode only). After a reply,
`main.py`'s `_converse()` loops `record_command()` → skills/brain → `speak()`
without needing the wake word again, calling `listener.flush()` after every
reply so Jarvis never records its own voice as the next command. (The capture
gate already mutes while `speaker.speak()` runs; the flush also clears what
was queued just before it started talking.)

It goes back to waiting for the wake word when:

- an end phrase is said — `skills.is_conversation_end()`, normalized the same
  accent/case-insensitive way as the rest of `skills.py`. `"τέλος Τζάρβις"`
  and `"αντίο Τζάρβις"` match anywhere in the utterance; bare `"τέλος"` only
  as the whole utterance, otherwise a sentence like *"στο τέλος της μέρας"*
  would end the conversation. Jarvis says "Εντάξει." on the way out.
- `CONVERSATION_TIMEOUT` seconds (6) pass with no speech — `record_command()`
  returns `"no_speech"` and a short beep (`speaker.beep_done()`) signals the
  switch back.

`"κλείσε"` is unchanged: still a full shutdown via `skills.shutdown_requested`,
which `_converse()` reports by returning `False` to break the outer loop.

Phrase lists are spelled naturally (`"τέλος Τζάρβις"`, accents and final `ς`
included) and normalized at import by `text.phrases()`. Writing one out in
its normalized form instead would break silently the next time
`normalize()` changes — which is exactly what the iotacism fold did to the
phrases that were spelled `"τελοσ"`. See "Normalization".

## Skills

`jarvis/skills.py` handles a fixed set of Greek voice commands locally,
before the transcribed text ever reaches the brain: `main.py` calls
`skills.handle(text)` first and only calls `brain.ask(text, memory_block)`
when that returns `None`. Matching is accent-insensitive, case-insensitive,
and tolerant of extra words (simple substring matching on normalized text),
since the input comes from speech recognition.

Implemented, in the order `handle()` tries them: shutting Jarvis down; saving
to memory and reading it back (both in "Memory" below); a spoken timer that
announces itself (print + `speaker.speak`) when it fires; current time / date;
and opening a website (`SKILL_SITES` in `config.py`) or a local app
(`SKILL_APPS` in `config.py`) by name. Only the sites/apps listed in
`config.py` can ever be opened — `skills.py` never builds a command from
spoken text, and app launches always use a fixed argv list (never a shell).

The order is not cosmetic. Shutdown is first so `"κλείσε"` can never be
intercepted by another skill, and the memory save runs before the memory
recall because their trigger phrases overlap — see "Memory" for that one.

Shutdown is signalled via a `skills.shutdown_requested` flag (set by the
skill, checked by `main.py` after speaking the reply) rather than by
`handle()`'s return value, since `handle()` is otherwise always `str | None`.

Timers run on a daemon `threading.Timer` so a pending one can't hang process
exit. `speaker.speak()` is guarded by a lock so a timer announcement firing
while Jarvis is already talking waits its turn instead of cutting off or
overlapping the current audio.

## Memory

Persistent, local, SQLite. `data/jarvis.db` (gitignored), created on first
use. `jarvis/db.py` owns the schema and connections; `jarvis/memory.py` turns
speech into rows and rows back into context; `jarvis/mem.py` is the CLI.

Opened WAL-mode with a 5s busy timeout, so `python -m jarvis.mem` in a second
terminal works *while Jarvis is running* — including wake-word mode, where
there is no prompt to type at.

**Writing.** `skills.handle()` calls `_handle_memory_save(text)` with the raw
utterance (not the normalized one — notes are stored the way they were said).
`memory.parse()` is a pure function with no I/O and no LLM: a ladder of Greek
patterns, first match wins.

1. reminder with a relative time (`υπενθύμισέ μου σε 2 ώρες…`)
2. reminder with an absolute time (`…στις 5 το απόγευμα…`)
3. exam — needs an exam word **and** a parseable date
4. course — gated on `μάθημα`/`εξάμηνο`, or `κάνω γυμναστική` becomes a course
5. profile fact (name, studies, job, city, age, school, preferences)
6. business (`η επιχείρησή μου…`)
7. **fallback to a plain note — always taken**

Nothing is ever discarded or guessed into the wrong table. Profile rows are
keyed, so saying your name twice updates it; every other table appends.
Captured values are stored verbatim, articles included (`η σχολή μου είναι το
ΕΚΠΑ` stores `το εκπα`), consistent across all profile patterns.

`businesses.name` is nullable and is `NULL`, never `""`, when no business was
named — `NULL` says "no name given" unambiguously. Rows classified by a head
phrase the pattern consumed (`businesses`, `courses`) get that category word
appended to their `norm`, or an unnamed business would be unfindable by the
very word that filed it there.

**Never stored.** `memory.is_sensitive()` rejects before any write, from both
voice and the CLI: password/PIN/API-key/IBAN words outright, and
card/ΑΦΜ/ID words only alongside a digit run (so "η κάρτα βιβλιοθήκης λήγει
τον Μάιο" is still an ordinary note), plus card-length digit runs, long
numeric ids, and `sk-…` shapes.

**Reading.** `memory.recall()` builds the block injected into each brain call,
under a hard `MEMORY_TOKEN_BUDGET` (300, estimated `len/3`). Order: profile
digest, then anything due today, then keyword hits. Lines are added whole and
dropped whole. `brain.ask(text, memory_block)` injects it as a *transient*
system message after the frozen prefix — never appended to `_history`, so it
can't accumulate and the history trim can't cut one in half. `recall_safe()`
swallows every error: a locked database means no memory this turn, never a
failed reply.

Keyword search stems the **query token only** and matches `LIKE '%stem%'`
against stored text, so the stem need only be a prefix of the stored form and
matching is symmetric without a stem column. Single pass, longest suffix
first, `MIN_STEM = 5` with reject-and-continue — a suffix that would leave
fewer than 5 characters is skipped and the next tried. That rule is what
keeps `μαθήματα` from being cut to `μαθη`, which would match μάθηση, μαθητής
and μαθηματικά indiscriminately.

**Recalling out loud.** Separate from the block above, which the user never
hears: `skills.handle()` calls `_handle_memory_recall(text)` before the brain
gets the utterance, and answers directly from `memory.search()` — the same
keyword hits as `recall()`, without the profile digest and today's items it
prepends. `MEMORY_RECALL_LIMIT` (3) caps what's spoken.

`MEMORY_RECALL_PHRASES` triggers it: `"θυμάσαι"`, `"τι ξέρεις"`,
`"τι έχεις"`, `"τι σου είπα"`, `"τι μου είπες"`, `"σου είχα πει"`. Matching is
substring-based, so only the shortest form of each is listed — bare
`"θυμάσαι"` already covers *"τι θυμάσαι"* and *"θυμάσαι αν"*, and `"τι έχεις"`
covers *"τι έχεις για"*. Writing the longer ones out too would add entries
that can never match on their own.

Two phrases are deliberately absent. Bare `"ξέρεις"` would swallow ordinary
questions (*"ξέρεις τι ώρα είναι"*), so only `"τι ξέρεις"` is there.
`"θύμισέ μου"` is worse than it looks: normalized it is `θιμισε μου`, a
substring of the reminder trigger `υπενθύμισέ μου`, so every reminder would
match it.

One ordering dependency comes with this. Bare `"θυμάσαι"` overlaps
`RE_TRIGGER`'s *"να θυμάσαι ότι…"* save form, and is safe only because
`handle()` runs `_handle_memory_save` before `_handle_memory_recall`. That
was incidental before and is load-bearing now; `test_memory_skills.py` pins
it.

**A recall question with no topic** (*"Τι θυμάσαι;"*, *"Τι μου είπες;"*)
leaves no searchable token — every word is a stopword or too short to stem —
so `stems_of()` comes back empty and there is nothing to match. Answering
`"Δεν θυμάμαι κάτι σχετικό."` there reports an empty result for a search that
was never run, so `_handle_memory_recall` routes that case to
`memory.spoken_profile()` instead: *"Θυμάμαι το όνομά σου, τη σχολή σου και
πού μένεις. Ρώτησέ με για κάτι συγκεκριμένο."*

It names the profile **keys** it holds and never reads a stored value aloud,
which is the point — values come back normalized, so a name speaks as
`γιαννι` (see "Known gaps" below). `_PROFILE_LABELS` maps each key
`PROFILE_PATTERNS` can write to a spoken label; a test asserts the two stay in
step, since a new pattern without a label would be stored and then silently
left out of the answer. An empty profile invites a first save rather than
listing nothing.

The recall verbs themselves are in `_STOPWORDS` for the same reason — without
`ειπες` there, *"Τι μου είπες;"* stems to the junk needle `ιπεσ`, runs a real
search, finds nothing, and answers "nothing found" instead of taking this
path.

### Known gaps

1. **Words under 5 characters don't stem.** `πόλη` → `πολι` is 4 characters;
   stripping `ι` would leave `πολ` (3), below `MIN_STEM`, so the token is
   matched literally, and καφές/καφέδες still miss each other entirely.
   The iotacism fold narrows this one to a single direction: `πόλη` and
   `πόλεις` fold to `πολι` and `πολισ`, and since matching is
   `LIKE '%stem%'`, asking about πόλη now finds a note about πόλεις — but
   not the reverse.
2. **Verb inflection isn't handled.** `γράφω` vs `έγραψα` needs both the
   augment (ε-) and a consonant change (φ→ψ); suffix stripping bridges
   neither. Recall is noun/keyword-oriented, which covers most memory
   lookups, but verbs are a real gap.

Also worth knowing: parsed fields are stored normalized, so they read back
both accent-stripped **and** iotacism-folded — a course is `μαθιματικα`, a
name is `γιαννισ`. The fold preserves pronunciation by construction, so TTS
is unaffected (stress placement can still suffer from the missing accents),
but it is one more step away from the written word for anything that is shown
on screen or injected into the brain prompt. Only `notes.text` keeps the
utterance verbatim; the rest are captured out of the normalized string.

Making those captures verbatim would mean matching against the folded text
while slicing the unfolded one — an offset map through `parse()`'s whole
ladder. Not done: the fold's cost here is the same kind as the accent
stripping that was already accepted.

**Managing it.** `:mem <command>` at the Enter prompt and `python -m
jarvis.mem <command>` share one dispatcher (`mem.run`). Commands: `list`,
`show`, `edit`, `del`, `export`, `backup`. `del` reads the row back and
requires an explicit yes — anything unrecognised, including silence, is a no.
`edit` refuses `id`/`created_at`/`norm`, re-runs the sensitivity check, and
refreshes `norm` so an edited row stays findable under its new wording.

**Schema.** `SCHEMA_VERSION` is 2. `db.connect()` migrates an older database
in place on the next start (`db._migrate`); version 2 refolds every stored
`norm`, see "Normalization".

**Backups.** `db.backup()` uses SQLite's backup API (safe against a
concurrent writer) into `BACKUP_DIR` (`data/backups/`), keeping the newest
`BACKUP_KEEP` (5). Runs once at startup, wrapped so a failure never stops
Jarvis from starting, and on `:mem backup`. Filenames are microsecond-stamped
so they are unique and sort chronologically — pruning deletes the oldest by
name, and a coarser stamp let a pruned name be reused and overwritten.

## Normalization

`text.normalize()` is what every phrase list, every pattern and every stored
search key is compared through. It lower-cases, strips accents, folds the
final `ς` to `σ`, and folds the **iotacism** vowels: η, ι, υ, ει, οι and υι
are all pronounced /i/ in Modern Greek, so they are all folded to `ι`.

The fold exists because Whisper transcribes sound, not spelling. A spoken
"Θυμήσου ότι με λένε Γιάννη" came back as "Θυμ**ί**σου ...", missed
`memory.RE_TRIGGER`, fell through to the brain and was never saved. Every
spelling of the sound now normalizes to `θιμισου`.

Three digraphs are deliberately kept: `ου` is /u/, and the `υ` of `αυ`/`ευ`
(and `ηυ`) is a consonant. Folding those would merge words that do not rhyme
— `που` would become `πι`. `fold_iotacism()` matches them first and passes
them through.

Two consequences worth knowing:

- **Nothing compared against normalized text may be written out in its
  normalized form.** `"τελοσ"`, `"ανοιξε"` and `"θυμησου"` were all correct
  before the fold and are all wrong after it. Phrase lists go through
  `text.phrases()` / `normalize()` at import (`skills.py`, `text.py`), and
  patterns go through `memory._re()`, which folds the *regex source* — safe
  because the fold only touches Greek letters, never a metacharacter.
  `_re()` also strips accents (but does not lower-case: `\S` is not `\s`),
  so a pattern can be written in ordinary Greek.
- **Stored `norm` columns were written unfolded before this.** `db.connect()`
  migrates them at schema version 2, refolding each one in place;
  `fold_iotacism(old_norm)` is exactly what the new `normalize()` would
  produce, since folding is the only step added, which matters because a
  row's `norm` often can't be rebuilt from the row's own columns.

## Language

The user speaks Greek. Jarvis should answer in Greek by default.

## Working rules

- Explain changes briefly before/after making them.
- Ask before deleting any file.
- Never edit `.env`.
- Remind me to commit once something works.
