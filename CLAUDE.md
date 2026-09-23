# Jarvis

Local, offline Greek-speaking voice assistant. No cloud APIs, no API keys.
Voice output can optionally use Microsoft's online Edge TTS (falls back to
offline Piper if it's unavailable); everything else stays local.

## Roadmap

The build plan lives outside this repo, in a Claude doc:
https://claude.ai/artifact/HdQAkniZAwJQVoMaTqX8q2 — eight phases, with the
model to use and the budget for each. It is the source of truth for what comes
next; this file stays the source of truth for what is already built.

**Current phase: Phase 1 — wake word reliability.** "Hey Jarvis" should trigger
within a couple of seconds on the first try; live tests show it sometimes taking
30-90s, while the hits that do land score 0.93-0.99. Diagnosis before tuning
(`tools/wake_score_probe.py`), then either a custom Greek "Τζάρβις" model or a
mic-gain/`WAKE_THRESHOLD` recalibration from real scores.

Update that line when a phase is finished and the next one starts.

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
- `jarvis/text.py` — Greek normalization (see "Normalization"), the span map
  that makes captures verbatim, and the phrase data shared by `skills.py` and
  `memory.py` (see "Memory"). `skills.py`
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

*When* each level counts is `_StopDecider`'s business, not the loop's — see
"The recording clock". `record_command()` only moves bytes: read a frame,
skip it if it predates the last `flush()`, pad any hole to its real length,
stop when told. It returns `(text, stop_reason)` — `"speech_end"`,
`"no_speech"`, `"gap"`, `"max"`, `"timeout"` or `"stream_end"` — because the
caller must tell "you finished talking" from "you never started" from "what
reached me had holes in it". `"no_speech"` and `"gap"` return without
transcribing at all; running Whisper on pure silence invites it to
hallucinate a phrase out of room noise, and running it on a splice invites a
confident wrong one.

`WAKE_DEBUG=true` prints detection scores (including near-misses below the
threshold, for tuning), the mic level once a second while waiting for speech,
and a stopped-because line carrying the invariant: audio seconds, wall
seconds, and seconds lost to holes. The `[timing] Recording` line always
prints both clocks — `4.24s wall / 3.04s audio` — because the wall-clock
number alone is what hid a 43-second recording and a 0.05-second one for
hours.

Read the ratio in two parts, which is why the debug line separates them.
Every turn starts by waiting ~1s for ffmpeg's buffer to reach the new floor;
that is fixed overhead and says nothing. *After* frames start flowing, audio
should track wall clock at ~1.00 — that is the number that collapsed to 0.35
on the 43-second turn, and the one to look at first if recordings ever feel
wrong again.

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

## The recording clock

Every frame off the stream carries the `stream_time` at which it was
**recorded** — audio seconds since the device opened, counted for gated
frames too. That is the only clock any recording decision may use. Wall
clock survives in exactly two places: one backstop for "audio stopped
arriving", and the timing line.

The distinction is not academic. ffmpeg's dshow input (`-audio_buffer_size
1000`) hands over **~1 second of audio at a time, in a sub-millisecond
burst**, then nothing for a second; the device takes ~1.4s to produce its
first frame at all; and the capture gate drops frames that never arrive.
Measured, not assumed — 12.6 frames/s overall, 57 of 63 inter-frame gaps
under 1ms, max gap 1.008s.

Counting *frames*, as this used to, measured neither the audio nor the
clock, and produced two symptoms that looked unrelated:

- **43.06s for one utterance.** The stop was `187 frames` — 14.96s of audio
  — collected from a stream delivering about a third of real time.
  187 × 0.2303s = 43.07s. The wall-clock backstop that should have caught
  it lived inside the `queue.Empty` branch, which a trickle of frames keeps
  out of reach. It is checked every pass now.
- **0.05s, nothing captured, "Δεν κατάλαβα τι είπες".** A burst already
  sitting in the queue satisfied the whole end-of-speech rule — 2 loud
  frames + 13 quiet = 1.2s of stale audio, drained in ~8ms — before the
  user had said anything. `_frame_db()` costs 0.06ms against an 80ms
  real-time budget, so the consumer outruns the producer by ~1300×.

One clock fixes both, because the mapping needs no delay constant:

```
recorded_wall = _stream_origin + stream_time
```

Audio is *produced* in real time even though it *arrives* in bursts, so
buffering changes when a frame shows up, never when it was spoken.
`_stream_origin` is estimated as a rolling minimum of `wall_at_read -
stream_time` (= the true origin plus that frame's arrival delay, so the
minimum is the tightest estimate), windowed so one early sample can't pin it
forever if the driver later drops audio.

**Holes are classified, never papered over.** `_StopDecider` is a pure
state machine — `feed(ts, level_db) -> stop reason or None`, no clock, no
queue, no I/O, driven from a list in `tests/test_record_timing.py`. A
contiguous frame is the ordinary case; a hole up to `MAX_GAP_SECONDS` is
padded with real silence and counted as quiet (quiet is what the microphone
heard, and concatenating across a hole welds words together for Whisper to
transcribe as one); a longer hole, or more than `MAX_LOST_SECONDS` of them
in one utterance, returns `"gap"` and the turn is re-asked instead of
transcribed. Same principle as the fuzzy triggers' third guard: a silent
wrong answer costs more than another try. `main.py` caps that at
`MAX_GAP_RETRIES` (2) before going back to the wake word.

**The capture gate asks about recorded time too.** `_should_capture()` asks
`speaker.was_speaking(recorded_wall)`, not `speaker.is_speaking()`. The old
question was unanswerable: by the time a frame reaches the gate, the audio
in it is a second old, so a 0.4s cooldown against a ~1.0-1.4s pipeline delay
muted the wrong second in *both* directions — it dropped live audio while
Jarvis's own voice, buffered from before he stopped, sailed through and
started a "recording" off his own reply. `speaker.ECHO_PAD` now means only
what it says (the room's echo), and is deliberately not applied before an
interval starts: audio recorded just before Jarvis opened his mouth is the
user's. Intervals are recorded around playback only, not around Edge TTS
synthesis — a second on the network makes no sound and shouldn't mute the
microphone.

**`flush()` is a watermark, not a drain.** It raises `_capture_floor` to the
current recorded time, and every consumer skips frames below it. Draining
alone never worked: ffmpeg still held the same second in its own buffer and
handed it over immediately afterwards. The floor drops that second wherever
it is sitting, while anything recorded *after* the flush survives — which
draining could never manage.

**openWakeWord gets a `reset()` on every hole.** It scores each frame from
the ~1.5s of audio before it, so audio welded across a hole leaves that
window holding two different moments spliced together. The retrigger
cooldown restarts with it, since the buffer is empty either way.

## Conversation mode

`CONVERSATION_MODE` (on by default, wake-word mode only). After a reply,
`main.py`'s `_converse()` loops `record_command()` → skills/brain → `speak()`
without needing the wake word again, calling `listener.flush()` after every
reply so Jarvis never records its own voice as the next command. (The capture
gate already drops frames *recorded* while he was audible; the flush raises
the floor past everything older than the end of the reply. See "The recording
clock" — both are answering the same question on the same clock now.)

A turn that comes back `"gap"` is re-asked rather than answered: Jarvis says
«Δεν σε άκουσα καλά, πες το ξανά.» and records again, up to `MAX_GAP_RETRIES`
(2) times. Past that, asking a third time won't fix a microphone dropping
that much audio, so it goes back to the wake word.

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
7. **fallback to a plain note — always taken, unless the trigger itself was
   fuzzy** (see "Endings are tolerated, stems are not")

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
`memory.spoken_profile()` instead: *"Θυμάμαι ότι το όνομά σου είναι Γιάννης,
η σχολή σου είναι το ΕΚΠΑ και η πόλη σου είναι Αθήνα."*

It speaks the facts themselves — *"Θυμάμαι ότι το όνομά σου είναι Γιάννης
και η πόλη σου είναι Αθήνα."* `_PROFILE_LABELS` maps each key
`PROFILE_PATTERNS` can write to a spoken phrase with a `{value}` slot; a test
asserts the two stay in step, since a new pattern without a phrase would be
stored and then silently left out of the answer. An empty profile invites a
first save rather than listing nothing.

It used to name the keys and deliberately withhold every value, because
values were captured out of the normalized string and a name spoke as
`γιαννι`. That is fixed at the source (see "Verbatim captures"), so the
constraint is gone.

Each phrase is written so the value needs no agreement with it. The patterns
consume the preposition — `μένω στην Αθήνα` stores `Αθήνα` — so `"μένεις
{value}"` would be ungrammatical and `"μένεις στη {value}"` would be guessing
the article's gender. `"η πόλη σου είναι {value}"` needs neither.

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

### Verbatim captures

Every display column holds what the user actually said — accents, capitals
and Whisper's own spelling included. `norm` beside it is still folded, and is
the only thing search ever touches. The two are now different things on
purpose; a test pins that.

This was a real gap, not a theoretical one: `Γιάννης` was saved and spoken
back as `Γιάννι`. Captures were being sliced out of the normalized string, so
the folded form landed in the column meant to be readable, and `_PROFILE_LABELS`
existed to avoid ever saying one out loud.

The fix is `text.normalize_spans()`, which returns `normalize(text)` plus, for
each output character, the `(start, end)` of the input that produced it.
`memory.Norm` wraps the two: patterns match `.norm` exactly as before, and
`.group(match, name)` reads the capture back out of `.raw`. Slicing a `Norm`
(stripping the trigger) returns another `Norm` over the same raw text, so a
capture taken afterwards still points at the right place.

The map is needed because `normalize()` is **not length-preserving**.
Lower-casing, accent stripping and the final sigma are one character in, one
out; `fold_iotacism` is not, since `ει`/`οι`/`υι` each collapse to a single
`ι`. A normalized index is therefore not an index into the raw text — the two
cannot be sliced in parallel. This is the whole reason the map exists, and
`test_text.py` pins both the digraph case and `normalize_spans()` never
disagreeing with `normalize()`.

No schema change was involved. The display columns (`profile.value`,
`notes.text`, `courses.name`, `exams.course`/`topic`, `businesses.name`/`note`,
`reminders.text`) already existed and already meant this; they were simply
being filled from the wrong string. `SCHEMA_VERSION` stays at 2.

Rows written *before* this hold folded values and cannot be repaired
automatically — the fold is lossy, so `γιαννι` could have been η, ι, υ, ει, οι
or υι. Re-save them, or fix them with `:mem edit` (which has always rebuilt
`norm` from the display columns, so the CLI path was never affected).

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

`text.normalize_spans()` is the same transformation plus a map back to the
input, for the callers that have to recover what was *said* rather than what
was matched. See "Verbatim captures" under "Memory".

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

### Punctuation is *not* normalized away

Whisper also punctuates what it hears, and sometimes splits one word into
two. The same hand test that produced "Θυμίσου" later produced "Θυμί σου,
ό,τι με λένε Γιάννη" — a space inside the verb, a comma after it, and "ό,τι"
for "ότι". `RE_TRIGGER` missed all three, the utterance fell through to the
brain, and Ollama role-played having saved the name; only the next recall
showed the database was empty.

`normalize()` does **not** strip punctuation, and the fix for that did not
change it. The asymmetry with the iotacism fold is deliberate:

- **Iotacism is folded globally**, in `normalize()`, because the spelling it
  destroys carries no information anyone downstream uses. η and ι are the
  same sound; nothing in the ladder needs to tell them apart, so folding
  once at the entry point is strictly simpler than teaching every pattern
  about it.
- **Punctuation is tolerated locally**, in the trigger patterns only,
  because commas and periods *are* load-bearing further down the same
  ladder. `RE_COURSE_OF` uses them as the boundary marking where a course
  name ends (`[^,.]+?`, plus `,` and `.` in its lookahead); stripping them
  in `normalize()` would run "στα Μαθηματικά, στις 12 Ιουνίου" together into
  one course name. Stripping them would also rewrite every stored `norm`
  column, needing a schema version 3 migration for no gain.

So the tolerance lives where the recognizer's noise actually lands: three
named separators in `memory.py` — `_GAP` (`[\s,.·]+`, between two words of a
trigger phrase), `_JOIN` (`[\s,]*`, inside one word that may have been
split) and `_OTI` (`ο[\s,]*τι`, matching "ότι" and "ό,τι" alike) — replace
every hand-written `\s+` in `RE_TRIGGER`, in `RE_STRANDED_PARTICLE` (the
particle strip in `parse()`, which missed `ο,τι` for the same reason), and
in `_REMIND_VERB`. The rule to carry forward: a separator inside a phrase
matched against speech should be `_GAP`, not `\s+`.

`WhisperPunctuationTests` in `tests/test_memory_parse.py` pins this, as
`IotacismTests` pins the fold. Its strings are written as transcriptions,
not as sentences anyone would type — testing this with clean text is how the
bug survived in the first place.

### Endings are tolerated, stems are not

The same spoken "Θυμήσου ότι με λένε Γιάννη" has now failed three times in
three different ways: `Θυμίσου` (another spelling of the same sound),
`Θυμί σου` (a word break nobody spoke), and `Θυμήσω` — a different ending
outright, which neither earlier fix caught.

The pattern underneath all three: **Whisper reproduces the stressed stem and
improvises the unstressed ending.** `θυμ` survived every one. A fourth
spelling of the ending was always going to come, so the ending is matched by
edit distance now instead of by enumeration, while the stem stays exact.

`text.edit_distance()` is a plain two-row Levenshtein — stdlib only, no new
dependency. `difflib` was measured and rejected: `SequenceMatcher.ratio()`
is length-normalized and rewards a long clean prefix, so it scores every
false trigger *above* the true one (`κρατάω`/`κράτα` 0.909, `θυμάσαι`/`να
θυμάσαι` 0.875, `σημειώσεις`/`σημείωσε` 0.800, against `θυμήσω`/`θυμήσου`
0.769). No cutoff separates those. An edit count does, and reads as "one
character" rather than as a tuned constant.

`memory._FUZZY_TRIGGERS` holds each trigger as an **exact core plus an
ending budget**. Three separate guards keep the looseness honest, and only
one of them is a number:

1. **The core is mandatory and exact.** `"να θυμ"` keeps its `να`, so a
   recall question (`"Θυμάσαι τι σου είπα;"`) can never reach the save path
   however wide the budget gets. This matters because the live miss needs a
   budget of 2, and `θυμάσαι` is *also* exactly 2 from `να θυμάσαι` — no
   threshold could separate them, so what protects recall is the shape of
   the match, not its size. That removes the load-bearing weight the
   save-before-recall ordering in `handle()` was carrying alone.
2. **It only runs when `RE_TRIGGER` misses.** Every utterance that matches
   today keeps its exact path, so no pinned behaviour can regress.
3. **A fuzzy trigger may not reach the note fallback.** `parse()` threads
   `notes_ok`; a guessed trigger earns only the *structured* rungs, and must
   be confirmed by a second, independent pattern (profile/exam/course/
   business) before anything is written. Reaching step 7 on a guessed verb
   would be guessing twice, so it falls through to the brain — which is what
   that utterance did before the fuzzy layer existed. The worst case is
   therefore unchanged behaviour, never a wrong row, and a wrong row is the
   expensive one: nobody sees it until a recall reads it back.

Budgets are per phrase, because how much room a verb has depends on what
lives next to it: `θυμήσου`, `να θυμάσαι` and `μην ξεχάσεις` get 2;
`σημείωσε` gets 1, because `σημειώσεις` is an ordinary noun sitting 2 away;
`κράτα` gets none and stays exact-only in `RE_TRIGGER`, since at five
characters `κρατάω` is a single edit from it.

Two consequences to carry forward:

- **A fuzzy-triggered plain note is not saved.** `"Θυμήσω να πάρω ψωμί"` —
  mangled verb *and* unstructured body — still goes to the brain. That is
  guard 3 working, not a gap to close.
- **The reminder rung running first is now load-bearing.** `"Θύμισέ μου"` is
  2 edits from `θυμήσου`, so the fuzzy trigger would claim it; step 0 of the
  ladder gets there first.

`MangledTriggerEndingTests` and `FuzzyTriggerNegativeTests` pin this. The
negatives are the ones that matter — eight sentences that must keep reaching
the brain — because loosening a trigger trades a missed save for the risk of
a silent wrong one.

## Language

The user speaks Greek. Jarvis should answer in Greek by default.

## Working rules

- Explain changes briefly before/after making them.
- Ask before deleting any file.
- Never edit `.env`.
- Remind me to commit once something works.
