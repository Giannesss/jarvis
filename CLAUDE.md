# Jarvis

Greek-speaking voice assistant, local by default. Speech recognition, memory,
skills, the policy layer and the audit log never leave the machine.

Two parts can optionally use an online service, each behind its own switch and
each with a local fallback:

- **the brain** — `BRAIN_PROVIDER=claude` sends the conversation to Anthropic's
  Messages API and needs `ANTHROPIC_API_KEY`; the default `ollama` runs a local
  model and needs neither a key nor a network. See "Providers".
- **the voice** — `TTS_ENGINE=edge` (the default) sends the reply *text* to
  Microsoft to be spoken, falling back to offline Piper when that fails.

So "no cloud APIs, no API keys", which this line used to say, is now a
configuration rather than a property: true of the brain at its default setting,
and never quite true of the voice. With `BRAIN_PROVIDER=ollama`, nothing you
*say* reaches Anthropic.

**What the Claude brain sends, when it is selected:** the system prompt, the
few-shot examples, up to `MAX_HISTORY_MESSAGES` of the conversation, and the
recalled memory block — which is to say saved facts about you. Nothing else;
there are no tools and no file access. `memory.is_sensitive()` already refuses
to *store* passwords, PINs, cards and IBANs, so they cannot be recalled into a
prompt either — that guard was written for the database and turns out to be the
one that matters here too.

## Roadmap

The build plan lives outside this repo, in a Claude doc:
https://claude.ai/code/artifact/86a745ad-d442-47ac-b841-30d3bc2b070d — eight
phases, with the model to use and the budget for each. It is the source of
truth for what comes next; this file stays the source of truth for what is
already built.

**Phase 6 — a visible Jarvis, started out of order — closed on 2026-10-03.**
Phase 5's step 5 (populating real data — syllabus topics, exam dates, each
business's description) is still open, the same way Phase 2's brain half was
built before Phase 1 closed: the pointer moved on because the user asked to,
not because step 5 is done. It stays open, tracked below rather than quietly
dropped, and nothing about Phase 6 depended on it. Phase 7 — file and
computer organization — is next; see the roadmap doc for its steps.

Phase 6's own five steps (the roadmap's "Steps (do in order)"): an audit of
the current architecture before any UI code; the UI framework decided by an
actual comparison, not assumed; a functional shell first, empty states, no
logic; the microphone/brain/TTS wired in one at a time, testing after each;
then the state machine, real system monitoring, tasks, quick actions and
settings/debug mode, in that order, never all at once.

**All five steps are done.** The audit: everything under `jarvis/` outside
`main.py` itself — `listener.py`, `brain.py`, `speaker.py`/`player.py`,
`skills.py`, `policy.py`, `memory.py`, `scheduler.py`, `db.py`, `diag.py` —
is already decoupled from the CLI (no `print()`s baked into the logic, no
assumption about who's calling) and needs zero changes to work under a GUI.
Only `main.py` is GUI-incompatible, and only because of its shape: a single
blocking loop with no state machine, no threading, and terminal `print()` as
its only output. The framework decision came down to PySide6 (native,
in-process, LGPL — genuinely free, unlike PyQt6's GPL/commercial split) over
a lightweight web-shell (pywebview + a local server): a single-machine,
local-first assistant has no reason to run an HTTP server for itself, and
Qt's signal/slot model is built for exactly the pattern this needs — a
background thread finishes listening/thinking/speaking, the UI thread is
told. See "The GUI shell" for what step 3 built.

**Phase 5 — deep research and expertise — is what came before it, and is
itself done except for step 5.** Five steps: a `research` skill that runs
several searches on a topic through the Claude API with web search enabled; a
*structured summary* of what it found — not a document dump — stored as a
dated "expertise" row; `memory.recall()` surfacing those rows on related
questions the way it already surfaces profile facts and notes; a refresh
trigger («ξανακάνε έρευνα για…») so a stale topic can be redone on request;
and populating real data early — this semester's courses, syllabus topics and
exam dates, and a description of each business.

**Its prerequisite is built, and steps 1-3 of Phase 5 are code, now
hand-tested on live audio too** (see the three live tests below). The roadmap lists Phase 5 as requiring the Claude
brain from Phase 2, so that half of Phase 2 was built first — see below. Step 1
is where «δεν έχεις πρόσβαση στο ίντερνετ» in `SYSTEM_PROMPT` stops being true
for one specific action: a brain reached *over* the network still cannot look
anything up on its own (that sentence still governs ordinary replies), but
`brain.research()` holding the web-search tool can. See "Research" for what is
built: the `research` skill (step 1), the structured summary stored as a dated
`expertise` row (step 2), and `memory.recall()`/`memory.search()` surfacing
those rows the way they already surface profile facts and notes (step 3). Step
4, the refresh trigger, is also built — «ξανακάνε έρευνα για…» upserts the same
row rather than piling up duplicates. Step 5, populating real data (this
semester's courses, syllabus topics and exam dates, each business's
description), is content, not code, and is the last thing left in this
phase — now started: the 1st-semester course list (7 courses, from the
user's own program-of-studies schedule) is in the real `courses` table via
`tools/seed_courses_2026_2027_sem1.py`. Syllabus topics, exam dates and
business descriptions are still to come, as the user sends them.

That schedule also carries day/time/room/professor detail the `courses`
table has no columns for — a weekly recurring timetable is a different
shape of data from the single-dated `exams`/`reminders` the agenda already
handles. Flagged to the user rather than dropped or silently bolted on when
the course list was first seeded; the decision came back yes, and the
feature — a new `class_schedule` table, agenda wiring, and now
`tools/seed_class_schedule_2026_2027_sem1.py` carrying the real 13-row
weekly timetable off the user's own schedule image — is built and closed,
seed script and live hand-test both done. See "The weekly class schedule"
under "Memory". «Τι έχω σήμερα» (a Wednesday) and «Τι έχω αύριο» each spoke
back the right classes, in the right time order, room and professor
included, against the real database — matching Thursday's Μαθηματικά
Ι/Υδατική Χημεία/Φυσική Ατμόσφαιρας the user had described live before this
table existed.

Untested by design, not by oversight: this is real network traffic with a real
bill attached (`$10`/1000 searches plus tokens), so it is the first live
`Permission.CONFIRM` skill rather than `SAFE` — see "Policy" — and the roadmap's
own hand-test rule applies before it counts as closed, the same as Phase 3 and
Phase 4 before it.

**All three live tests have now run.** «Κάνε έρευνα για γυμναστήρια στην
Ξάνθη», confirmed with a bare «Ναι», came back through `brain.research()`
with a real, well-formed Greek summary (five short sentences, no list
formatting) about actual gyms in Ξάνθη — the CONFIRM gate, the server-side
`web_search` tool and `_final_text()`'s preamble-stripping are all confirmed
live. The refresh trigger also fired, but exposed the "same words, not same
subject" upsert boundary written up under "Research" — a genuine limit, not
a bug. The hand test also found one real gap, now fixed: «Κάνε ξανά έρευνα
για …» (verb, "again", "research", in that order) didn't match either
pattern and fell through to the brain, because only «ξανακάνε έρευνα» (the
two glued into one recognized word) was recognized; `RE_RESEARCH_REFRESH`
now accepts both. The third test — asking a later, unrelated-sounding
question to see whether `memory.recall()` actually surfaces the researched
topic — has now run too: «Τι θυμάσαι για γυμναστήρια;», with no mention of
Ξάνθη, matched the local `memory_recall` skill (not even the brain) and
spoke the full stored `expertise` summary back verbatim. Confirms the row
written by step 2 and the keyword search wired in step 3 both work end to
end on a real question, not just against a temp database in the test suite.

**Phase 2's brain half — the Claude API as a real `BRAIN_PROVIDER` — is built,
out of order, because Phase 5 needs it.** Three of the roadmap's five steps for
it are not code (create a key with a monthly spend cap, put it in `.env`, keep
qwen3:8b as an offline fallback), and the shape of the change follows from the
third: the local model does not go away, it becomes the fallback, exactly the
way Piper sits behind Edge TTS. `claude-haiku-4-5` is the default, the roadmap's
own pick, and the model with the fewest surprises for a ≤120-token spoken reply.
See "Providers" for the rules; `tests/test_brain_claude.py` pins them against a
fake client.

**Phase 2's brain half closed on 2026-09-30.** All four live-audio debts the
roadmap's hand-test rule demanded are now settled. A plain general-knowledge
question («Πες μου μερικά πράγματα για την Αθήνα») and a follow-up both came
back through `[timing] Claude stream: ...` (not Ollama), streamed normally,
with a correct answer and a sensible first-token time (0.6–2.7s) — the
wiring itself, end to end. A recall («Πώς με λένε;» answered «Σε λένε
Γιάννη.» from the recalled memory block, not invented). A grounding question
of the five that used to draw invented answers («Πες μου για τη ζωή μου πριν
τρία χρόνια» answered «Δεν ξέρω τι συνέβη στη ζωή σου πριν τρία χρόνια, δεν
μου το έχεις πει.» — flat refusal, nothing made up). And a barge-in
mid-reply on the streamed Claude path specifically (barge-in itself closed
already from Phase 3, but never before tried against this provider): asked
about the Β' Παγκόσμιος Πόλεμος, talked over Jarvis mid-sentence, and
`[barge] fired at 38.72s: 22.7 dB over a -46.3 dB floor (margin 9.0 dB)` cut
the reply off at 7.89s of the 9.60s it would have played, kept 23 pre-roll
frames, and the next turn recorded normally — same behaviour as the Ollama
path, now confirmed on this one too. Phase 2's *voice* half (ElevenLabs) is
still untouched and out of scope for this closure; `TTS_ENGINE`'s
`"elevenlabs"` is still a placeholder.

**Phase 3 — a reply you can interrupt — closed on 2026-09-27**, built out of
order on `feature/streaming-interrupt` and merged to `master` when it closed.
Four steps: a stoppable player (see "Playback"), a chunker, streaming replies
(see "Streaming replies") and barge-in (see "Barge-in"). Hand-tested on live
audio in wake-word mode, which is what closed it: streaming works, barge-in
fired several times cleanly with no false fires, the words that interrupted
him became the next command, and the recording-clock watermark (the last
commit on that branch) was re-verified live — no negative durations and no
spurious re-asks. The operating point it needed — `BARGE_IN_MARGIN_DB=9` —
came from `tools/barge_probe.py` rather than from a guess, and the probe took
three revisions before it was measuring the right thing; it is a property of
this room and re-measuring beats copying the number.

**Phase 4 — structure and safety — closed on 2026-09-28.** Four steps: the
skills registry, the policy layer and kill switch (see "Policy"), a durable
scheduler for reminders and timers that survive a restart (see "Scheduler"),
and a tag/category column across the memory tables so "τι έχω σήμερα" can
pull from every area at once (see "Tags"). The pointer had moved past it
before any of the three live tests the roadmap's own rule demands, leaving it
as debt rather than a finished step; all three are now settled:

- **Reminder restart-survival.** A timer was set, the window was closed
  before it fired, and well past a minute later the next run announced
  `[sched] Όσο ήμουν κλειστός έληξε ένα χρονόμετρο.` at startup, before Enter
  was pressed — `catch_up()` claiming a still-`pending` row as `missed` and
  speaking it before the main loop starts, exactly as "Scheduler" describes.
- **The agenda across tables.** «Θυμήσου ότι έχω εξέταση σήμερα στα
  μαθηματικά» saved to `exams` («Το σημείωσα στις εξετάσεις σου.»), «Θυμήσου
  ότι σε δύο ώρες πρέπει να πιω νερό» saved to `reminders` via the
  delay-inside-the-body rung («Εντάξει, θα σου το θυμίσω.»), and «Τι έχω
  σήμερα» answered with both rows pulled together from their two different
  tables in one sentence, alongside three older reminders in their
  fired/missed states with the right status suffixes.
- **The kill switch's fuzzy and bare rungs.** «Στα μάτα τα πάντα» (Whisper's
  mangling of «σταμάτα τα πάντα», normalizing to the exact string the fuzzy
  rung was built to catch) froze it, after two near-misses with a wrong
  object correctly stayed unfrozen; a bare «Πάγω σε» (mangled «Πάγωσε»)
  froze it again on the other rung. While frozen, an unrelated phrase and a
  wrong shutdown verb form were both refused; the real shutdown trigger
  («Κλήσε», which folds to the same normalized string as «κλείσε» through
  the iotacism fold) still passed through and shut it down cleanly, and the
  freeze — which survived that shutdown in the database, as documented —
  was cleared afterward with `python -m jarvis.policy unlock` from a fresh
  terminal.

Phase 1 closed on 2026-09-23 (`docs/PHASE1_STATUS.md`): the beep-reset bug and
the too-high silence floor are fixed and confirmed live, and the remaining
20-30s trigger latency is explained as model/accent fit rather than a bug —
the real fix is a custom Greek "Τζάρβις" model, deferred as not urgent.

Update that line when a phase is finished and the next one starts.

## Run

```
.\.venv\Scripts\python.exe main.py
```

Requires Ollama running locally with the configured model pulled, and the Piper
voice model downloaded into `models/`. Enter at the prompt to record, `exit`/`quit` to leave.

## Pipeline (as implemented)

1. **Record** — `jarvis/listener.py` shells out to FFmpeg over a hardcoded
   DirectShow mic (`MICROPHONE_NAME`). Two paths, both silence-detected
   rather than fixed-duration: `listen()` is the Enter-press one-shot path,
   stopping on ffmpeg's own `silencedetect` filter, capped by
   `MAX_RECORD_SECONDS` (15s) with `NO_SPEECH_TIMEOUT` (8s) for leading
   silence. Wake-word mode instead keeps one persistent ffmpeg stream open
   for the whole run and stops each turn via `record_command()`'s own
   frame-level `_StopDecider` — see "Wake word" and "The recording clock"
   for how that path works.
2. **Speech-to-text** — same file, transcribes with `faster_whisper`
   (`WHISPER_MODEL` from `.env`, default `"small"`; CPU, int8,
   `language="el"`).
3. **LLM reply** — `jarvis/brain.py` sends the running chat history to
   whichever brain `BRAIN_PROVIDER` names — a local Ollama model by default,
   or the Claude API — with a system prompt asking for short,
   natural-sounding replies (no markdown, no calling itself an AI unless
   asked) and refusing to invent the user's own facts (see "Grounding").
   The prompt doesn't say anything about which language to reply
   in — see "Language" for how that's actually handled. Two entry points:
   `ask()` returns a finished reply (`ollama.chat`), and `start_turn()`
   streams one (`stream=True`) for the path below.
4. **Text-to-speech** — `jarvis/speaker.py` synthesizes the reply with
   Microsoft Edge TTS by default (`TTS_ENGINE=edge`), falling back to
   offline Piper if Edge synthesis fails (e.g. no internet) or if
   `TTS_ENGINE=piper`. Both hand raw PCM to `jarvis/player.py`, which can be
   stopped mid-word — see "Playback". With `STREAM_REPLIES` on (the default)
   the reply is spoken *as it is generated*, in pieces cut by
   `jarvis/chunker.py` — see "Streaming replies".

`main.py` wires these together in a loop, trying `jarvis/skills.py` first and
only falling back to the brain when a skill doesn't match (see "Skills").

## Files

- `main.py` — CLI loop tying listener → skills → brain → speaker together.
- `jarvis/listener.py` — FFmpeg recording + Whisper transcription.
- `jarvis/skills.py` — local Greek voice commands, handled without the LLM.
- `jarvis/policy.py` — the `Skill`/`Permission` types, the permission gate,
  the kill switch, the audit log, and `python -m jarvis.policy` (also backing
  `:policy` at the prompt). See "Policy".
- `jarvis/brain.py` — the brain call (Ollama or the Claude API, see
  "Providers") + conversation history + system prompt. `ask()` for a finished
  reply, `start_turn()` → `Turn` for a streamed one (see "Streaming replies").
- `jarvis/chunker.py` — pure: cuts a token stream into pieces worth speaking.
- `jarvis/speaker.py` — Edge/Piper synthesis, the speech lock, the
  audible-interval bookkeeping the capture gate reads, and `speak_stream()`,
  which turns a stream of text pieces into one continuous, stoppable sound.
- `jarvis/player.py` — the output device: one PortAudio stream per utterance,
  fed from a queue, abortable mid-word. See "Playback".
- `jarvis/config.py` — loads `.env` (via `python-dotenv`) into `OLLAMA_MODEL` and `PIPER_MODEL_PATH`.
- `jarvis/text.py` — Greek normalization (see "Normalization"), the span map
  that makes captures verbatim, the fuzzy word matcher (`edit_distance`,
  `match_core`, `fuzzy_word`) shared by `memory.py`'s save triggers and
  `policy.py`'s kill switch, and the phrase data shared by `skills.py` and
  `memory.py` (see "Memory"). `skills.py`
  re-exports it under the old names, so `_normalize` and `SHUTDOWN_PHRASES`
  still work there.
- `jarvis/db.py` — SQLite schema, connections, backups.
- `jarvis/diag.py` — the diagnostic log: every `[timing]`/`[rec]`/`[wake]`
  line the terminal prints, timestamped into `data/jarvis.log`. See
  "Diagnostics".
- `jarvis/scheduler.py` — fires the `reminders` rows: claim, announce, and the
  startup catch-up for what came due while Jarvis was off. See "Scheduler".
- `jarvis/memory.py` — parsing speech into rows, and recalling rows as context.
- `jarvis/mem.py` — `python -m jarvis.mem`, also backing `:mem` at the prompt.
- `tools/barge_probe.py` — standalone barge-in diagnostic, not part of the
  app: measures how loud Jarvis's own voice arrives at the microphone against
  how loud you are talking over it, and replays candidate thresholds over the
  recorded frames. Where `BARGE_IN_MARGIN_DB` came from — see "Barge-in". Every phase is watched
  (`STREAM_SILENT_TIMEOUT`, 3s — far tighter than the app's 15s, because here
  audio is meant to be flowing continuously) and the run is abandoned with a
  device diagnosis the moment the microphone stops feeding it, rather than
  summarized as statistics; see the stderr note under "The recording clock"
  for the run that earned that. The speech bucket **follows a detected onset,
  not the prompt** (`_find_onset`): it opens on the first audio to hold
  `ONSET_DETECT_OVER_ECHO_P95_DB` (6 dB) over the echo's p95 for
  `ONSET_SECONDS`, and runs `BARGE_WINDOW_SECONDS` (4.0s) from there. It was
  pinned to the prompt — a 0.6s lead plus a 2.5s window — until a run answered
  after 5.8s: the bucket came back holding 2.6s of empty room, reported its
  +1.9 dB margin as though it had measured a voice, and filed the voice itself
  (peaking -14.1 dB) as `post`. Recomputed against a real echo floor the same
  burst reached +25.8 dB over a +11.9 dB false-fire bound — so the run read as
  evidence against a feature it was actually evidence for. The replay is also
  primed on the audible frames immediately before the onset rather than the
  pre-prompt echo, which was worth another 9.2 dB on that run. Finding no
  onset falls back to the fixed lead and says so loudly: the original defect
  was reporting an empty bucket in silence.
  `tests/test_barge_probe.py` pins the phase
  bucketing on synthetic frames — unusual for a diagnostic, and there because
  the buckets were once accused of dropping every frame when the real fault
  was the microphone. It asserts through the CSV, which is written by the same
  `label` closure the summary counts through, so it tests that the file and
  the numbers agree as well as that the labels are right.
- `tools/wake_score_probe.py` — standalone wake-word diagnostic, not part of
  the app: scores live or replayed audio with no threshold or debug floor in
  the way (free-running, prompted-attempts, and replay modes). See "Roadmap".
- `tools/gen_wakeword_clips.py`, `tools/record_wakeword_clips.py` — one-off
  data-prep tools for training a custom Greek "Τζάρβις" openWakeWord model:
  synthesizing clips from TTS voices, and recording real clips through the
  mic. Not part of the app.
- `native_mic_test.py`, `wavein_capture_test.py`, `windows_capture_test.py` — throwaway
  experiments trying different Windows mic-capture APIs; not part of the app.
- `gui_main.py` — entry point for the Phase 6 GUI shell, parallel to
  `main.py`, not a replacement for it. See "The GUI shell".
- `jarvis/gui/main_window.py` — `MainWindow`: the microphone/brain/voice
  wired in on background `QThread`s, the state machine, system monitoring
  (now including network throughput), the tasks list, quick actions and
  settings/debug mode (all of Phase 6), presented since as a dark-themed
  dashboard — a header with a live clock, a sidebar of five pages, and an
  original glowing `_Orb` avatar in place of a plain status label. See "The
  GUI shell" and "The dashboard redesign".

## Config

All settings live in `.env` (copy from `.env.example`) and are read in one
place, `jarvis/config.py`, each with a comment saying why its value is what it
is. `ANTHROPIC_API_KEY` is the only secret among them: read from the
environment, never printed, never written to `data/jarvis.log`, and only read at
all when `BRAIN_PROVIDER=claude`.

The mic device name and the FFmpeg path are hardcoded constants at the top of
`jarvis/listener.py`.

## Providers

`brain.py` and `speaker.py` dispatch to a provider function based on
`BRAIN_PROVIDER` and `TTS_ENGINE` (both in `.env`, see `.env.example`).

- Brain: `BRAIN_PROVIDER` selects from `_PROVIDERS` in `jarvis/brain.py`.
  `"ollama"` (local) and `"claude"` (Anthropic's Messages API) are both
  implemented; `"openai"` is a placeholder for later — adding it means a new
  function plus a new dict entry, no other changes. An unknown value raises
  `ValueError` at import time (startup), before the app's request-level error
  handling can swallow it. **So does `BRAIN_PROVIDER=claude` with no
  `ANTHROPIC_API_KEY`** (`_check_key()`), and for the same reason: a missing key
  is one clear message at startup, where the alternative is a spoken «δεν μπορώ
  να απαντήσω» once per question for the rest of the run.
  The `anthropic` SDK is imported *lazily*, inside `_get_client()` — with
  `BRAIN_PROVIDER=ollama` it is never needed, importing `brain` must stay cheap
  (`skills.py` pulls it in transitively), and the test suite therefore runs on a
  machine that has never installed it. Same lazy-singleton idiom as
  `speaker._get_voice()`.

### The Claude brain

Four things about it are deliberate, and three of them are about *not* sending
something.

- **The message format is one pure function** (`_to_messages_api`). Ollama takes
  a flat list with `system` messages anywhere in it; the Messages API takes a
  top-level `system` and a `messages` list that may not carry the role at all —
  a mid-conversation system message is an Opus 5 / 4.8 feature and a 400 on
  Haiku 4.5. That is not an edge case here: it is exactly where
  `_build_messages()` splices the recalled memory, on every turn that recalls
  anything. So every system message is joined into `system` in order, the frozen
  prompt first and the memory block after it — which is also where it belongs,
  since `MEMORY_PREAMBLE` exists precisely to make recalled facts read as
  background rather than as something the user just said. `_history` is never
  touched, so `ask()` and `Turn`'s contract is unchanged and its tests pass
  untouched.
- **No `temperature`, no `thinking`, no `output_config`.** `CLAUDE_MODEL` is a
  knob, and a request only valid for today's value of it is a trap:
  `temperature` is rejected outright on Sonnet 5 and Opus 5, `effort` on Haiku
  4.5, and an *absent* `thinking` means "none" on Haiku 4.5 but "adaptive" on
  the 5-series — which a 120-token spoken reply has no latency budget for. The
  prompt already asks for one or two short sentences, so the Ollama path's
  `TEMPERATURE` has no twin here.
- **The stop-reason vocabulary stays Ollama's, translated at this provider's
  edge.** `Turn.truncated` reads `"length"`; the Messages API says
  `"max_tokens"`. `_normalize_stop()` is the one line that keeps everything
  downstream from knowing which brain answered, and `MAX_REPLY_TOKENS` (120)
  carries over unchanged — same cap, same `_trim_to_last_sentence()` when it
  bites.
- **A transient failure falls back to Ollama; anything else raises.** This is
  the roadmap's "keep qwen3:8b as an offline fallback"
  (`CLAUDE_FALLBACK_OLLAMA`), and it is the same shape as Edge → Piper: the
  fallback is literally the other provider function, called with the same
  message list, and it is **announced on the terminal** the way «Edge TTS
  απέτυχε …, χρήση Piper.» is. A fallback nobody is told about is a different
  assistant answering under Claude's name.

  Which failures, and why the line is drawn there (`_is_transient`): 408/409/429
  and 5xx fall back, as do the SDK's connection and timeout errors. A 401, 403
  or 400 **raises** — it will never fix itself, and answering from qwen3 anyway
  would mean every reply quietly comes from the local model while you believe
  you are talking to Claude. That is a wrong answer with nothing on the terminal
  to show for it; a spoken error is the cheap direction. **Anything
  unclassifiable raises too**, a bug of our own included. Classification is by
  HTTP status plus the exception's module and class *name*, never by catching
  `anthropic`'s classes — that is what lets this module classify an error
  without importing the SDK, and the suite run without it installed.

  **Streaming falls back only before the first delta.** Once a word has reached
  the speakers there is no way to start over on the local model without saying
  it twice, so a failure mid-reply stays a failure: `Turn` drops the turn and
  `main._stream_reply()` speaks its error line, exactly as it already does when
  Ollama dies mid-stream.

`tests/test_brain_claude.py` pins all of it against a fake client. Nothing in
the suite reaches the API, needs a key, or needs the SDK installed.
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

## Playback

`jarvis/player.py` owns the output device; `speaker.py` owns synthesis, the
lock that serializes two callers, and the audible intervals the capture gate
reads. They were one module until Phase 3, and the split is what makes a
reply stoppable.

**The reason it had to change.** `speaker.speak()` played with
`winsound.PlaySound(wav_bytes, SND_MEMORY)`, which blocks until the sound
ends and cannot be cancelled — Python refuses the one flag combination that
would help:

```
>>> winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_ASYNC)
RuntimeError: Cannot play asynchronously from memory
```

So barge-in was never a feature that could be bolted onto the old speaker: a
reply already handed to winsound was going to finish. It also had no way to
answer "how much of that did we actually say", which is what the
conversation history has to be committed from once a reply can be cut in
half.

A `Player` is one PortAudio stream per utterance, fed from a deque by the
callback thread. Four things about it are deliberate:

- **One stream across chunks, not one per sentence.** A streamed reply
  arrives as several separately synthesized pieces; giving each its own
  device open/close would put a seam between every sentence. Appending to a
  running stream makes the seam a buffer hand-off instead.
- **Underruns are counted, never silent.** If synthesis falls behind
  playback the callback has nothing to hand the device and plays silence —
  audible as a hole mid-sentence and otherwise invisible. Same discipline as
  `listener._frames_evicted`, which exists for the same reason.
- **`played_seconds` is what reached the device, not what was heard.**
  PortAudio runs up to a blocksize plus the device's own latency ahead of
  the speaker cone, so it overstates by ~20–50ms. That is the right
  direction to be wrong in: it decides how much of a reply to commit to
  history, and crediting Jarvis with a few milliseconds he almost said beats
  dropping a word he did.
- **`CallbackStop` is raised on the block that empties the buffer, not the
  one after.** sounddevice's own contract is that pending buffers still
  play, so that block is heard in full; raising a block later would append
  silence to every reply, and not raising it would count an underrun for a
  sentence that simply ended.

**`speaker.stop()` is the output half of barge-in** — deciding *whether* to
interrupt is somebody else's job. It is not a no-op when nothing is playing
yet, and that is the non-obvious part. Measured live: an Edge synthesis took
1.53s, a stop arrived at 1.20s, and the reply then played out in full,
because `_current` was still `None` and the stop was dropped on the floor. A
stop that does nothing is worse than no stop at all — the user hears the
assistant ignore them. So it sets `_stop_pending`, which the next player to
start within that same `speak()` honours; `speak()` clears the flag on
entry, so a stop aimed at the previous reply cannot silence the next one.

**The device is configurable because PortAudio's default is not Windows's.**
Measured here, PortAudio picks device 3 (MME, the monitor's HDMI output)
while winsound followed the system setting. `PLAYBACK_DEVICE` takes an index
or part of a device name; empty means PortAudio's default. If replies ever
go silent, this is the first thing to check. A device that will not open
raises `PlaybackUnavailable` and `speaker.py` falls back to winsound —
uninterruptible, but losing barge-in is cheaper than losing the reply.

**The beeps stay on `winsound.Beep`.** They drive the system beep rather
than an output stream, they are far too short for cancelling to mean
anything, and keeping them off the PortAudio path means a device that will
not open costs the reply's quality and never the cue that tells the user to
speak.

`tests/test_player.py` drives the callback by hand, one block at a time,
with `sounddevice` faked in `sys.modules` — the same way
`tests/test_record_timing.py` drives `_StopDecider` from a list. Nothing in
the suite opens an audio device. It also patches `PLAYBACK_DEVICE` to empty:
`device=None` deliberately exercises `_resolve_device`, and without the patch
whether the suite passes depends on what is in `.env` and which speakers are
plugged in.

## Streaming replies

`STREAM_REPLIES` (on by default) speaks a reply as it is generated instead of
waiting for all of it. Three parts, each owning one question, wired together
in `main._stream_reply()`:

- **`brain.start_turn()` → `Turn`** streams the deltas and settles the
  history. `ask()` is unchanged and is still the path when `STREAM_REPLIES` is
  off or a provider has no streaming entry (`_STREAM_PROVIDERS`, same idiom as
  `_PROVIDERS`).
- **`chunker.Chunker`** decides where a piece is worth saying. Pure, no I/O,
  driven from a list in tests. See its module docstring for why the *first*
  chunk is allowed to be a clause rather than a sentence — measured, it moved
  the first spoken words from +6.3s to +3.3s, and sentence-granular streaming
  alone would have bought ~0.5s.
- **`speaker.speak_stream()`** turns pieces into one sound. One `Player` for
  the whole reply, so a seam between two chunks is a buffer hand-off.

**The history is committed from what was *heard*, not from what was
generated.** That is the whole reason `Turn` exists rather than a callback on
`ask()`. A reply cut off halfway was, to the user, only its first half, and a
model told it said the rest answers the next question as though the user had
heard it. `Turn` is settled exactly once — `commit(spoken)` or `abandon()` —
and an empty `spoken` pops the user's message too, because a question answered
by silence is a question never asked (the same reasoning `ask()`'s `except`
clause already followed).

Which chunks count is `speaker._heard()`: **a chunk is spoken once half of it
has played.** The halves are not symmetric — `played_seconds` is what reached
the device rather than the speaker cone, so it already leans towards crediting
a few milliseconds Jarvis nearly said. Dropping a sentence the user heard in
full makes the next reply repeat it; keeping one they heard the first word of
makes it refer back to something they never got.

Three smaller things are deliberate:

- **The terminal prints from `on_chunk`, not from the generator.** Generation
  runs a chunk or two ahead of playback, so printing as pieces are *produced*
  shows sentences that an interruption a moment later means nobody ever hears.
  `speak_stream` calls `on_chunk` as each piece is handed to the device.
- **The abort is checked before the next piece is pulled.** Pulling is what
  waits on the model, so checking afterwards cost one more chunk of generation
  and one more synthesis round trip after the reply was already over.
- **A samplerate change mid-reply costs exactly one seam.** A running
  PortAudio stream cannot change rate, so Edge failing halfway and Piper
  taking over at a different rate opens a second player. Logged, because it is
  the only case in which a reply legitimately has a join in it.

`tests/test_brain_stream.py` and `tests/test_speak_stream.py` pin both halves;
neither opens a device or reaches a model.

## Barge-in

Talking over Jarvis stops him mid-word. Off at the Enter prompt and on in
wake-word mode (`BARGE_IN_ENABLED`), because it judges audio that only exists
on the persistent stream.

**The frames it judges are the ones the capture gate throws away.**
`_should_capture` refuses everything recorded while Jarvis is audible — and
that is exactly the audio that can answer "is this him or is this you". So
`_stream_audio_reader` now shows each refused frame to `_BargeDecider` on its
way to being counted in `_frames_gated`. Nothing else about the gate changes:
what reaches the recording pipeline is identical, which is what keeps this
from being able to break the ordinary path.

**The rule is the one `tools/barge_probe.py` measured**, so
`BARGE_IN_MARGIN_DB` means here exactly what the probe's would-have-fired
table reported: each frame's level minus the **running median** of recent
audio, fired when it holds above the margin for `BARGE_ONSET_SECONDS` (0.32s,
twice the recorder's own onset, because this one competes with a signal that
is already loud). Measured on the first run whose buckets were both real:

```
echo alone reaches  ~+4 dB   -> the false-fire bound
speech over him    ~+15 dB   -> the miss bound
9 and 12 dB fired on the barge with no false fire; 15 dB and up missed it
```

9 sits 5 dB clear of the first and 6 under the second. It is a property of
this room, these speakers, this mic gain and where they sit — re-measure with
the probe rather than guessing.

Four things are deliberate:

- **A median, not a mean.** A barge-in is a handful of loud frames against a
  two-second window; a mean would be dragged up toward them until they stopped
  clearing it.
- **The floor must exist before the rule can fire** (`BARGE_MIN_PRIME_SECONDS`),
  or the first frames of a reply are judged against a median of themselves.
  The probe learned this the hard way in the opposite direction: building the
  median out of the frames under test made a clean +11.4 dB margin report
  MISSED at every threshold. The cost is a blind window at the start of a
  reply — **but only the session's first**, because the window is kept between
  replies. It estimates his voice through these speakers in this room, which
  does not change between one sentence and the next. A stream restart clears
  it, since `stream_time` and the device both restart there.
- **`speaker.stop()` is the callback**, called on the reader thread. It is the
  output half of barge-in already: idempotent, cheap, and it honours a stop
  that arrives before there is anything to stop. Anything it raises is logged
  and swallowed — that thread is the only one draining ffmpeg's stdout, and a
  dead reader starves every consumer.
- **The reaction is bounded below by ffmpeg, not by the rule.** dshow hands
  over ~1s of audio per burst, so the abort lands ~1.0–1.3s of audio-time
  after the user starts talking, and `player.abort()` silences him within a
  blocksize (~23ms) of that. The probe's figures are recorded-time and say
  nothing about this.

**The words that interrupted him become the next command** (`BARGE_PREROLL`).
They have to be kept deliberately: the capture gate refused them, so the
decider's ring buffer is the only copy. `disarm_barge(collect=True)` returns
them as `(stream_time, frame)` pairs from the onset (minus one frame of lead),
and `record_command(preroll_frames=...)` feeds them **through `_StopDecider`**
rather than prepending them as opaque audio the way the wake-word pre-roll is
prepended — that one ends with the wake word, while these are the start of the
utterance, and a decider left waiting for an onset that already happened
returns `no_speech` on a turn that had words in it.

Three consequences worth carrying forward:

- **An interrupted reply is never followed by `listener.flush()`.** The flush
  is what stops Jarvis recording his own voice as the next command, but after
  a barge-in it would raise the floor past the second the user is still
  speaking in and throw away the rest of the sentence that stopped him. The
  audible part is already out of the pipeline; the pre-roll is how it comes
  back. `ConversationWiringTests` pins this, both ways.
- **Before the buffer is taken, the reader is given time to catch up.** It runs
  up to ~1s of audio behind real time, so at the moment an abort returns, the
  frames around it have been recorded but not read. Taking the buffer without
  them puts a hole in the middle of the interrupting sentence — the weld
  Whisper transcribes confidently and wrongly.
- **Not flushing leaves a backlog in the queue, so the turn carries its own
  watermark.** Nothing drains the audio queue while Jarvis is talking, and the
  capture gate is only shut while he is *audible* — so a reply's transcription,
  its Edge round trip, its Ollama generation and every gap between chunks put
  live frames in the queue. Ordinarily `flush()` drops them; after a barge-in
  it deliberately cannot. Those frames are recorded *before* the pre-roll, so
  feeding them after it ran `_StopDecider`'s clock backwards: measured live,
  `0.00s wall / -12.80s audio` — 14 pre-roll frames from 35.20s, then a
  backlog frame from 22.32s. `_StopDecider` cannot catch this itself, because
  a backwards step makes `gap` negative and negative is never a hole. The cost
  was never the number: whichever way the backlog is shaped, this turn is
  charged for it — its stale quiet reaches `SILENCE_DURATION` and ends the
  turn before the user has finished the sentence, or the hole the reply's own
  playback left in it reads as a `"gap"` and the turn is re-asked. The live run
  did the second, which is why no transcription line follows it in the log.
  So `record_command()` keeps a local `floor`, starting at `_capture_floor` and
  rising past each pre-roll frame it feeds: **nothing recorded before what the
  decider has already seen may be fed after it.** It is a watermark for one
  turn rather than a flush, which is the whole point — everything recorded
  *after* the pre-roll is the rest of the interrupting sentence and still
  arrives. Only this path can raise it, so every other path is unchanged.
  `BargePrerollTests` pins both shapes of the bug and the deaf-spot direction.

Known limits, accepted rather than solved: a false fire (someone else talking,
a TV) costs the rest of one reply, which is the cheap direction; and the first
second of audio in the pre-roll is you mixed with him, which is what
`BARGE_PREROLL=false` exists for.

`tests/test_barge_decider.py` drives the rule from a list of dB levels, the
way `_StopDecider` is driven, and pins the arming, the kept audio and the
conversation wiring.

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
a heartbeat every 5s while waiting for the wake word, and a stopped-because
line carrying the invariant: audio seconds, wall seconds, and seconds lost to
holes. `WAKE_SCORE_FLOOR` (0.001) is the lowest score worth printing; it was
0.1, which hid the only interesting failure, since a wake word spoken into a
freshly reset model scores ~0.000 rather than ~0.05. Set it to 0 to print
every scored frame. The `[timing] Recording` line always
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

**openWakeWord gets a `reset()` on every hole — but a reset is expensive,
so the holes worth resetting for are the ones that are left.** It scores
each frame from the ~1.5s of audio before it, so audio welded across a hole
leaves that window holding two different moments spliced together.

The cost is not a degraded score, it is no score at all. `reset()` reseeds
the model's 16-frame window with embeddings of *four seconds of random
noise* (`openwakeword/utils.py`), and a wake word spoken into that window
scores ~0.000 rather than merely low. Measured by replaying
`data/wake_probe.wav`: utterances that score 0.999 clean score **0.000**
when a reset lands under ~0.5s before them, and recover only at ~0.96s.
That is a full second of deafness, and the old debug floor of 0.1 printed
nothing for it — a swallowed wake word and a wake word never spoken were
the same empty log.

So the reset is now reserved for holes that mean what it assumes. Two
changes:

- **`listen_for_wake_word()` calls `flush()` on entry, not `_drain()`.**
  This was the only transition that emptied the queues without raising
  `_capture_floor`, and that asymmetry manufactured a hole every single
  cycle. Draining alone leaves ffmpeg holding ~1s of already-recorded
  audio; it arrives immediately afterwards and is accepted, because the
  floor still sits where the previous turn left it. Those stale frames set
  `last_ts`, and then the frames the capture gate dropped while
  `beep_done()` sounded — 0.16s of beep plus `speaker.ECHO_PAD` (0.4s), a
  stable **0.56s**, seven frames — read as a hole. The model was reset at
  the exact moment the beep told the user to speak. Raising the floor drops
  those leftovers instead, so the first accepted frame is live audio with
  `last_ts` still `None` and no hole to find. It costs nothing: the
  leftovers are the last second of an expired conversation timeout, silent
  by definition, and the reset threw them away seven frames later anyway.
- **The retrigger cooldown no longer restarts on a hole.** It exists to stop
  the wake word that just fired from firing again out of the model's own
  buffer. After a hole nothing was just spoken, so there is nothing to
  suppress — and a freshly reset window scores ~0.000, not spuriously high,
  so it cannot re-fire on its own either. Restarting it bought no protection
  and cost another ~1s on top of the reset's.

**A stream can also stop feeding the wait entirely, and that is not a
hole.** ffmpeg alive, its stdout pipe open, and the DirectShow device
delivering nothing: `_stream_audio_reader` blocks in `stdout.read()`, so
the EOF sentinel that means "the stream ended" is never sent, and
`listen_for_wake_word()` waits for a frame that is never coming. Measured
live — frames stopped at 20.6s into a wait and the loop sat there for 200+
seconds, printing a heartbeat with frozen counters.

`record_command()` got a wall-clock backstop when the 43-second bug was
fixed; this wait never did, because waiting indefinitely is what it is
*for*. The distinction it was missing is between waiting for someone to
speak and waiting for audio that will never arrive.

`STREAM_STALL_TIMEOUT` (15s) is that backstop, and it watches
**`_stream_frames_read`** — the counter the reader thread advances for
every frame it pulls off ffmpeg, gated or not — rather than the queue. An
empty queue is the ordinary state while nobody is speaking, and it is also
what the capture gate produces while Jarvis is audible; a frozen
`_stream_frames_read` is the only one of the three that no amount of
waiting fixes. It was also the discriminator in the live log: `gated` stayed
constant too, which is what ruled out a stuck capture gate. The heartbeat
now carries that counter for the same reason.

Checked every pass and before the `get()`, for the reason the recording
backstop is: the `queue.Empty` branch is exactly where a starved stream
lives. Not gated on `WAKE_DEBUG` — the hang happens either way. It raises
`StreamStalled`, distinct from the plain `RuntimeError` on the EOF
sentinel, so `main()` can tell "the device went quiet under a live process"
(which reopening the capture genuinely fixes) from "the process ended"
(which it does not): it restarts the stream up to `MAX_STREAM_RESTARTS` (2)
before falling back to the Enter prompt. 15s is ~15x the worst inter-frame
gap normal operation produces, so it cannot fire on jitter; it only turns
an unbounded hang into a bounded one.

**ffmpeg's stderr is no longer discarded, and `start_stream()` no longer
reports success for a microphone that isn't there.** Those were one loose
end: `_stream_stderr_reader` enqueued the lines onto `_stream_stderr_queue`
and nothing ever read it, so whatever ffmpeg said as a device failed was
thrown away. It cost two diagnoses — a wake-word wait that sat for 200+
seconds, and then a `tools/barge_probe.py` run whose microphone had dropped
off the USB bus mid-run: it captured 13 frames (1.04s, exactly one
`-audio_buffer_size 1000` buffer, ending on a -18 dB teardown transient) and
then reported a *statistics* problem, "0 frames of clean echo", beside a
plausible-looking -47 dB "room" level measured from that one buffer. A
`--barge` run 40s later captured nothing at all. Neither said the word
microphone, and ffmpeg's `Could not find audio only device with name [...]`
was sitting unread in the queue throughout.

- `_stream_stderr_reader` routes every non-`silencedetect` line through
  **`diag.write()`**, not `diag.log()`: most of it is ffmpeg's startup
  banner, which belongs in the record and not on the terminal at every
  launch. Deliberately not gated on `WAKE_DEBUG` — the device failure
  happens whether or not anyone asked for debug output, and this is its only
  trace.
- `_stderr_lines()` reads the queue back for an error message, dropping the
  `silencedetect` traffic and the startup banner (`_BANNER_MARKERS`) — both
  of them ffmpeg talking about itself rather than about the device. A
  *structural* filter, never a keyword one for "real" errors: an
  unanticipated failure must fall through it intact, since the outcome this
  exists to prevent is a bare exit code. **Which end to keep is the
  caller's**, and the two differ: a device that never opened is explained by
  the first lines (ffmpeg names the cause, then unwinds into generic
  wrappers), while one that died mid-run is explained by the last (the first
  fifteen are the banner of a stream that was working fine).
- `start_stream()` waits `DEVICE_OPEN_TIMEOUT` (2.5s) for evidence the device
  really opened, and raises with ffmpeg's own words if the process **exited**.
  Popen succeeding says nothing about the microphone: with the mic unplugged,
  ffmpeg starts, fails, prints why and exits -5 (`AVERROR(EIO)`) in ~200ms,
  and `start_stream()` used to return normally. Only an *exited* process
  counts as failure — a merely slow device must never cost wake-word mode, so
  a timeout with ffmpeg still alive returns normally and leaves that case to
  `STREAM_STALL_TIMEOUT`. The bound therefore only has to clear the ~1.4s
  DirectShow takes to hand over its first frame, and the wait returns the
  moment that frame arrives. `main()` already caught anything raised here
  into the Enter-press fallback, so the message reaches the user unchanged.
  `DeviceOpenTests` and `StderrLineTests` in `tests/test_record_timing.py`
  pin all of it, with a faked `Popen`.

If a structural hole ever comes back, the fallback is to skip the reset when
`_frames_gated` fully accounts for the missing frames — keyed to that
counter, never to a duration, which drifts the moment `ECHO_PAD` or a beep
length changes. `_gap_blame()` already computes it.

**Which is why a hole now says who took it.** A frame can go missing three
ways and they are identical by the time a consumer notices: the capture gate
refused it (`_frames_gated`), the queue overran and `_enqueue_bounded`
dropped the oldest to make room (`_frames_evicted`, otherwise completely
silent), or ffmpeg never produced it. (A gated frame is no longer only
counted: it is shown to the barge-in decider first, since audio recorded while
Jarvis was audible is the only audio that can say whether someone is talking
over him. See "Barge-in".) `_gap_blame()` turns the two counters
into the answer, and the gap line carries where in the wait the hole opened,
which separates a structural one at the start of a cycle from one that opens
mid-wait. A 5s heartbeat reports that the wait is still being fed; it is
checked *before* the `get()`, so it still prints when the queue has starved
entirely — which is one of the answers it exists to give.

## Conversation mode

`CONVERSATION_MODE` (on by default, wake-word mode only). After a reply,
`main.py`'s `_converse()` loops `record_command()` → skills/brain → `speak()`
without needing the wake word again, calling `listener.flush()` after every
reply so Jarvis never records its own voice as the next command. (The capture
gate already drops frames *recorded* while he was audible; the flush raises
the floor past everything older than the end of the reply. See "The recording
clock" — both are answering the same question on the same clock now.)

After every reply *he finished*, that is. An **interrupted** one is
deliberately not flushed: the floor would be raised past the second the user is
still speaking in, taking the rest of the sentence that stopped him with it.
See "Barge-in".

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

Each skill is a `Skill` entry in the `SKILLS` list — name, trigger phrases,
description, handler, permission — and `handle()` walks that list in order,
dispatching each through `policy.dispatch()` rather than calling the handler
itself. The `Skill` and `Permission` types live in `policy.py`, not here:
`dispatch()` has to call a handler, so policy can't import skills, and the
registry's *contents* stay here while its *types* live there. `skills.py`
re-exports both under their own names. See "Policy".

`phrases` on a registry entry is documentation, not the dispatch mechanism —
matching style genuinely varies (substring-in-list, single-keyword-then-parse,
`memory.py`'s regex ladder), and the registry doesn't unify it. `memory_save`
carries an empty tuple for that reason: its real triggers are
`memory.parse()`'s ladder, and listing a few here would be a fiction.

Implemented, in the order `handle()` tries them: shutting Jarvis down; saving
to memory and reading it back (both in "Memory" below); running a web
research and storing a structured summary (see "Research" — `research`
carries an empty tuple too, for the same reason `memory_save` does:
`memory.parse_research()` owns the real triggers); a spoken timer that
announces itself when it fires (a `reminders` row now, see "Scheduler");
current time / date;
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

A timer is a `reminders` row with `kind='timer'`, not a `threading.Timer`:
a Timer object dies with the process, so «βάλε χρονόμετρο για 20 λεπτά» used
to evaporate on a restart, silently, since nothing recorded it had ever been
set. `jarvis/scheduler.py` announces it. `speaker.speak()` is still guarded by
a lock, so an announcement firing while Jarvis is already talking waits its
turn instead of cutting off or overlapping the current audio.

## Policy

`jarvis/policy.py` sits between `skills.handle()` and every handler: nothing
runs without a permission, nothing that isn't `SAFE` runs without an explicit
yes, and every decision leaves an audit row. Nothing here imports `speaker`
or `listener` — asking the user a question is an injected callable
(`set_confirm_asker`), the same way `mem.run()` takes its confirm reader, so
the whole layer is testable without a microphone.

**Deny by default.** `Skill.permission` defaults to `Permission.BLOCKED`, so
a skill added later without naming a permission cannot run. An unrecognised
permission value is blocked too. Seven of the eight registered skills are
`SAFE` — none of them sends, deletes, spends or reaches outside the machine.
`research` (see "Research") is the first live `CONFIRM` skill: real network
traffic with a real bill attached, so it is recognised and asked about before
it runs rather than run on match the way a `SAFE` skill is. `BLOCKED` still
carries no live skill; it is what Phase 7's file move/rename/delete plugs
into.

**A `CONFIRM` skill needs a `matches()` and a `confirm_prompt`.** The
existing handlers answer "did you match?" by *doing the thing*, which is fine
for a safe action — running it is the decision — but a `CONFIRM` skill has to
be recognised before it acts or there is nothing left to confirm. So anything
not `SAFE` must be able to say whether it matched without acting, and must
carry the Greek question to ask out loud. A `CONFIRM` skill missing either
raises at import: that would be a registration bug wearing a safety feature's
clothes, silently never running. `BLOCKED` needs neither — with no matcher it
simply never runs, which is the right outcome anyway.

Silence, a mangled answer, an unrecognised word, a raising asker, or no asker
installed at all are all a **no** (`text.is_yes`, shared with `:mem del`).
The burden is on the yes.

### The kill switch

«σταμάτα τα πάντα» (also «σταμάτησε τα πάντα», «πάγωσε τα πάντα») freezes
everything except shutdown. While frozen, every utterance gets one fixed line;
only `SHUTDOWN_PHRASES` passes through, because being unable to shut down a
frozen assistant would be a worse trap than the one the switch exists to
escape. `main._reply_to()` re-checks `is_frozen()` before the brain as a
backstop.

Four things about it are deliberate:

- **It is checked in `policy.intercept()`, before the registry loop and
  before the permission gate** — not registered as a skill. It governs the
  policy layer, so it must not be gated *by* the policy layer.
- **It is matched as a substring, via a regex with `memory.py`'s `_GAP`
  separator between words.** A false positive freezes Jarvis, which is
  recoverable and roughly what the user meant anyway; a false negative means
  the kill switch didn't work when it was needed. Those are not symmetric.
  The regex rather than a plain substring is because `normalize()` doesn't
  strip punctuation and Whisper inserts it mid-phrase — «σταμάτα, τα πάντα»
  would otherwise miss. See "Punctuation is *not* normalized away".
- **The verb is matched fuzzily, the object is not** (`policy.fuzzy_kill()`,
  tried only when the exact regex misses). Two live attempts at «σταμάτα τα
  πάντα» came back as «Στα μάτα τα πάντα» and «Λέω στα μάτα τα πάντα» — both
  normalizing to `στα ματα τα παντα`, a word break *inside* the verb, which
  `_GAP` (a separator *between* words) cannot catch. So the verb goes through
  `text.fuzzy_word()` like memory's save triggers: stem exact (`σταματ`,
  `παγωσ` — that σ is what excludes «παγωτό»), ending by edit distance.

  **The asymmetry is the opposite of memory's, and that sets the budgets.**
  There a wrong match is a wrong row nobody sees until a recall reads it
  back; here it is a freeze, announced out loud and undone by `unlock` or a
  restart, while a miss is the switch failing when it was needed. So the
  budget is generous (2) and **what keeps it honest is the object, not the
  budget**: `τα πάντα` must follow the verb immediately, matched exactly
  (separators free, so a split «τα πά ντα» survives). Same role as memory's
  `notes_ok` — a guessed trigger must be confirmed by a second, independent
  piece. It does real work: «κοίτα με στα μάτια» normalizes to `κιτα με στα
  ματια`, whose `στα ματια` *does* match the verb one edit out, and is saved
  only by the missing object.

  Measured over the corpus in `FuzzyKillSwitchTests`: 14/14 positives fire,
  19/21 negatives stay silent. The two that fire are «θέλω να σταματήσω τα
  πάντα και να φύγω» and «σταμάτησα τα πάντα χθες» — first-person forms of
  the phrase itself, and accepted, since «σταμάτησε τα πάντα» already takes
  the third-person narration of the same thing. The audit log separates
  `voice` from `voice_fuzzy`, which is the evidence for retuning later.
  Known boundary, pinned by a test: adjacency means «σταμάτα *τώρα* τα
  πάντα» misses — as it did before this change too.
- **A third rung catches the verb spoken alone** (`policy.bare_kill()`,
  tried only when both of the above miss). Two live «σταμάτα τα πάντα»
  reached Whisper as «Πάγωσα» — the object gone from the transcript
  entirely rather than mangled inside it, so `fuzzy_kill()` had nothing to
  confirm the verb against and correctly stayed silent. Not a truncated
  recording either: those turns held 2.56s and 4.40s of audio against the
  ~1.4s a cut-short one stops at, so the words were said and the
  transcription lost them.

  With no object to confirm against, **the guard is the shape of the
  utterance: the verb and nothing else**, anchored at the start and
  required to reach the end. Same idiom as bare `"τέλος"` in
  `is_conversation_end()`. A leading «Τζάρβις» is skipped first (matched
  fuzzily, and in Latin too — Whisper mangles the name like any other
  ending), so «Τζάρβις, πάγωσε» still counts as bare.

  **Only `πάγωσ` is on this rung, and that is the design.** A bare
  «σταμάτα» is how you interrupt Jarvis mid-reply and is pinned as a
  negative; freezing everything for the commonest barge-in there is would
  be a worse failure than the one this fixes. «πάγωσε» alone has no
  competing reading — nothing else in the skill set freezes anything.
  Logged as `voice_bare`, so all three rungs stay separable in the audit
  log. Known boundary: a *trailing* «πάγωσε Τζάρβις» misses, since only a
  leading address is skipped.
- **There is no voice unlock.** Only `python -m jarvis.policy unlock` (or
  `:policy unlock` at the Enter prompt), or a restart. A spoken unlock would
  defeat the point.

The flag lives in `db.policy_state`, not in memory, so `unlock` from a second
terminal reaches a *running* Jarvis — the same WAL-mode trick that makes
`:mem` work mid-session. That means it also outlives the process that set it,
which is why `main()` calls `clear_on_startup()`: restart is the other
documented way out, and without that clear, a freeze would survive every
restart with no way back short of editing the database.

`is_frozen()` treats the database as authoritative whenever it can be read,
so a second terminal's unlock lands on the next utterance; the in-process
flag is a cache and the fallback when the read fails. It therefore fails
*frozen* if this process is the one that froze — forgetting a freeze is the
expensive direction.

### Audit log

`db.audit`, four short columns: `ts`, `action`, `decision`, `reason`. One row
per utterance — the skill that actually replied (not each one tried), or
`brain -> allowed (no_skill_matched)` when none did.

**It never holds what was said.** `action` is a skill name or a fixed word;
an utterance refused while frozen is logged as `request`, never as its text.
`decision` and `reason` come from the fixed vocabularies at the top of
`policy.py`, so the log can be grepped and reads consistently. A test asserts
every row stays inside them.

Read it with `:mem list audit`. It is evidence, so `mem.py` has it in
`_READABLE` but not `_WRITABLE` — `edit` and `del` refuse it. A failed audit
write costs a log row, never a turn, same discipline as `memory.recall_safe()`.

`policy_state` and `audit` were both added without touching `SCHEMA_VERSION`:
every statement in `_SCHEMA` is `CREATE ... IF NOT EXISTS` and `_init()` runs
the whole script on every connect, so an existing database picks up a new
table on its next start. That version tracks *data* migrations (the norm
refold, and later the tags column), and bumping it for a new table would
re-run those for nothing. A new *column* is the case that cannot ride in this
way — see "Tags".

## Diagnostics

`jarvis/diag.py` mirrors the terminal's diagnostic lines into
`data/jarvis.log`, timestamped. Every hard bug in this project has been a
timing one, and every one was diagnosed from a `[timing]` or `[rec]` line —
twice the line that would have settled a question was in a scrollback
nobody still had, so the question could only be reopened by reproducing the
fault live.

`diag.log()` is a drop-in for the `print()` calls that carried those lines,
and `_debug()` in `listener.py` now routes through it too. Four things
about it are deliberate:

- **It mirrors stdout rather than replacing it.** Callers keep their own
  gating, so a line printed only under `WAKE_DEBUG` is logged only under
  `WAKE_DEBUG`. That is what keeps the file comparable to a pasted
  scrollback — it is a record of the session *as it was shown*.
- **Nothing is written until `diag.start_session()` is called**, which only
  `main()` does. The log records runs of Jarvis, not imports of the package;
  without that, the test suite — which imports `speaker`, `skills` and
  `listener` freely — wrote fake `[timing] Piper load` lines into the real
  log. A `tests/__init__.py` guard was tried first and rejected: `unittest
  discover -s tests` doesn't import the package `__init__`, so it worked
  under one invocation and not the other.
- **It never costs a turn.** Every filesystem call is wrapped and failures
  are swallowed, same discipline as `db.backup()` at startup and
  `memory.recall_safe()`. The first failure sets a flag so a broken path
  costs one failed syscall per run rather than one per frame.
- **It holds no transcribed text** — timings, mic levels, stop reasons and
  wake scores only, the same rule the audit log follows. It still lands
  under `data/` (gitignored), because levels and timings describe someone's
  room even when the words are absent.

Rotated at `LOG_MAX_BYTES` (2MB), keeping `LOG_KEEP` (3) old files, and
each run opens with a `--- session start ---` banner so one session's lines
can be told from the last one's. `LOG_ENABLED=false` turns it off entirely.

## Scheduler

`jarvis/scheduler.py` is the half of the `reminders` table that was
missing. The columns — `due_at`, `kind`, `status`, `fired_at`, and the
`(status, due_at)` index — existed from the day the table was written, and
nothing ever fired them: `status` was only ever written as `"pending"` and
`fired_at` never written at all. A reminder set for 5pm was stored and then
surfaced only if the brain happened to be asked something that recalled it.

`main()` calls `scheduler.start(speaker.speak)` after
`policy.clear_on_startup()` — so a freeze left over from the last run can't
swallow the catch-up — and `scheduler.stop()` in the same `finally` as
`listener.stop_stream()`. Nothing here imports `speaker` or `listener`: the
announcement is an injected callable, the same idiom as
`policy.set_confirm_asker`, so the whole module is testable without a
microphone.

Four things about it are deliberate:

- **It polls; it does not arm a timer per row.** A `threading.Timer` object
  cannot survive a restart, which is the entire requirement. Polling also
  means a reminder added from a second terminal, or edited with `:mem edit`,
  is picked up on the next tick with no further wiring — the same WAL trick
  `:mem` and `:policy unlock` already rely on. The thread holds **one**
  connection for its lifetime, because `db.connect()` re-runs the whole
  schema script on every call; that is what makes `SCHEDULER_TICK` (2s)
  cheap enough to double as a timer's worst-case lateness.
- **A row is claimed before it is announced**, by an `UPDATE` guarded on
  `status = 'pending'`, and announced only if it changed exactly one row.
  Two Jarvis processes against one database is a shape WAL mode deliberately
  allows, and the loser of that race says nothing rather than repeating it.
  Claims are taken while the connection is held and the announcements made
  after it is released: speaking takes seconds, and holding a write
  transaction across it would block `:mem` in the other terminal for just as
  long.
- **Claim first, speak second.** A crash in between loses that one
  announcement. The other order risks the realistic failure instead: if
  `speak()` is itself what is broken (no audio device), speaking before
  marking would re-announce the same reminder every tick forever.
- **A freeze stops the clock without eating it.** While the kill switch is
  on, a tick claims nothing and announces nothing, so the rows are still
  `pending` when it is lifted. Announcing would break the freeze; claiming
  silently would make the freeze destroy data.

### Catch-up

`catch_up()` runs once at startup, on the caller's thread and before the
loop, so what was missed is heard right after «Jarvis έτοιμος» rather than a
tick later. Everything `pending` with `due_at <= now` is claimed as a third
status, **`missed`** — distinct from `fired`, so `:mem list reminders` can
tell "you were told this" from "this went by while Jarvis was off".

It is announced as one sentence rather than fired one after another: five
reminders replayed back to back on startup is a wall of speech nobody
listens to. Only the newest `SCHEDULER_CATCHUP_LIMIT` (3) are read out, and
**the bound is on what is spoken aloud, never on what is reported** — the
rest are counted in the same sentence («και άλλες 2 υπενθυμίσεις») and every
row stays in the table. An expired timer is counted but not replayed: a
countdown from yesterday has no content worth hearing again, and it is still
reported rather than dropped.

**A future-dated row is never touched.** `due_at <= now` is the whole
filter, so tomorrow's reminder is still `pending` after a catch-up and still
there to fire tomorrow. This is the roadmap's "never silently drops a
future-dated exam or deadline", and it is read as a constraint on the sweep
rather than a request to fire exams: `exams` has a date-only `due_date` and
no status column, and an exam is a fact `_due_today()` recalls, not an alarm.
`tests/test_scheduler.py` pins both halves.

No migration was involved. Every column already existed; `'fired'` and
`'missed'` are new *data*, not new schema, so `SCHEMA_VERSION` stays at 2 —
the same reasoning that let `audit` and `policy_state` arrive without a bump.

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
7. **relative delay in the body** (`θυμήσου να με ρωτήσεις σε 2 λεπτά…`) —
   see "A delay inside the body" below
8. **fallback to a plain note — always taken, unless the trigger itself was
   fuzzy** (see "Endings are tolerated, stems are not")

`RE_TRIGGER` is anchored at the start of the utterance, so which phrases may
open one is the whole of what it accepts: `θυμήσου`, `να θυμάσαι`, **`θέλω να
θυμάσαι`**, `κράτα`, `σημείωσε`, `μην ξεχάσεις`, each optionally behind a
leading `Τζάρβις`. `θέλω να θυμάσαι` was a live miss — nothing mangled, the
phrase transcribed correctly and refused anyway, because the anchor left no
room in front of `να θυμάσαι`. It fell past the save into `memory_recall`,
which answered a recall question nobody had asked. It is a form of its own on
both paths now, exact and fuzzy, at the same rung as `να θυμάσαι`, and
`WantToRememberTests` pins it — including the ordinary wishes (`θέλω να μάθω…`,
`θέλω να πάω…`) that the three-word core keeps out.

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

### A delay inside the body

Steps 1-2 are gated on the **reminder verb** — `θύμισέ μου`,
`υπενθύμισέ μου` — and step 0 only ever sees the whole utterance. So
«Θυμήσου να με ρωτήσεις **σε 2 λεπτά** αν έφαγα» matched neither: `θυμήσου`
is `RE_TRIGGER`'s *save* verb, the delay sat in the body, and the sentence
was filed as a plain note. It was stored correctly and then never
announced, because a note has no `due_at` for the scheduler to find — a
live test lost four minutes waiting for it. The audible tell was there and
easy to miss: a reminder answers «Εντάξει, θα σου το θυμίσω.», and that
turn answered «Το θυμάμαι.»

`RE_DELAY` + `_parse_delay()` close it, reading the *body* — the string
left after the trigger is stripped, which step 0 never sees. Three things
are deliberate:

- **It runs last among the structured rungs, and that placement is the
  whole safety argument.** Every rung above returns before reaching it, so
  nothing it diverts was ever anything but a plain note. No currently
  structured save can change table, which is an invariant the existing
  suite checks by passing unchanged.
- **The preposition is the gate.** A bare duration is a fact («το μάθημα
  διαρκεί 2 ώρες»); `σε` in front of it is what makes it a delay. What
  this deliberately accepts in exchange: «θυμήσου ότι θα γυρίσω σε 2 ώρες»
  now announces itself. The row is still stored and still searchable, so
  the cost is one spoken line; the cost of the miss was a reminder that
  never fired at all.
- **It sits above the `notes_ok` check, so a fuzzy trigger may reach it.**
  `σε` + number + unit is a second, independent pattern, which is exactly
  what guard 3 asks a guessed verb to be confirmed by (see "Endings are
  tolerated, stems are not"). «Θυμήσω να πάρω ψωμί» still goes to the
  brain; «Θυμήσω να πάρω ψωμί σε 10 λεπτά» saves.

The delay phrase is **cut out of the stored text** — the same thing
`RE_REMIND_REL` does by keeping only what follows the unit — so the
announcement two minutes later is «Υπενθύμιση: να με ρωτήσεις αν έφαγα»
rather than one carrying a stale countdown. Both halves are read back out
of `raw`, so the text stays verbatim; `Norm.slice()` only yields a
contiguous piece, hence the join. A delay with nothing else in the body
(«Θυμήσου σε 2 λεπτά») says when but never what, and falls through to the
note fallback.

Two known boundaries. **Absolute times mid-body** («…να με ρωτήσεις στις 5
αν έφαγα») are *not* on this rung: `στις 5` is more often a fact about a
schedule than an alarm, so widening there needs its own argument. And the
three original `RE_REMIND_*` patterns still separate their words with
`\s+` rather than `_GAP`, predating that rule — so «Υπενθύμισέ μου σε 2,
λεπτά…» misses where the new pattern would not.

### A reminder's time after «ότι», and an unfolded PM check

Two more live-audio-shaped bugs in the reminder parsers, found while fixing
a reported failure and both now closed with regression tests.

**The time after «ότι»/«να» instead of before it.** `RE_REMIND_ABS` reads
`_REMIND_VERB + "(?P<when>.*?)\s*(?:οτι|να)\s+(?P<body>.+)"` — `when` is
non-greedy, so it happily matches empty. The natural phrasing «Υπενθύμισέ
μου ότι έχω ραντεβού στις 8 το βράδυ» puts the clock phrase *after* «ότι»,
so nothing sits between the verb and the marker, `when` matches empty,
both `_parse_clock`/`_parse_date` come back `None`, and the whole sentence
fell straight through to the plain-note fallback — stored, never
scheduled, with nothing on the terminal to say a reminder had been
attempted. `_parse_reminder` now tries the same two parses a second time
against `body` when `when` comes up empty. Nothing needs cutting out of
the body for this, unlike `RE_REMIND_REL`'s unit or `_parse_delay`'s
countdown: a clock time is still true when it's read back later, so the
announcement keeps the full sentence, time phrase included. Same shape of
fix for «να» as the other marker. A date works the same way, at the cost
of inheriting the existing ambiguity between a bare number as an hour and
as a day (see below) — a separate question from the one this fix targets,
pinned rather than resolved.

**`_PM_PARTS` was spelled unfolded.** `RE_CLOCK`'s own source goes through
`_re()`, which folds it, so it correctly matches a captured «το βράδυ»/«το
μεσημέρι» as the folded «το βραδι»/«το μεσιμερι» — but the Python list
`_parse_clock` checked that capture against was written `("το μεσημερι",
"το απογευμα", "το βραδυ")`, by hand, with the fold undone on two of the
three (η and υ each fold to ι; «απόγευμα»'s «ευ» doesn't, which is exactly
why the one existing test for this, the afternoon case, never caught it).
The captured string never equalled either literal, so `hour += 12` never
ran: «στις 8 το βράδυ» and «στις 1 το μεσημέρι» both silently resolved as
the *am* hour. `_PM_PARTS` is now built with `normalize()`, the same
`_FRAC_SECONDS` idiom, rather than hand-folded a second time — exactly the
trap "Normalization" already warns about for phrase lists and patterns,
just missed here because this one is neither.

**Spelled-out hours.** `RE_CLOCK`'s hour was digits-only, so «στις οκτώ»
never matched *at all* — a silent drop into the note fallback, same as the
ordering bug above, just from a different cause. The hour group now reads
`_HOUR_RE` (digit or `NUMBER_WORDS`, same table `_NUM_RE` already draws
from), and `_parse_clock` reads it with `_number()` instead of a bare
`int()`.

**Known boundary, inherited rather than introduced:** a bare day-of-month
number in a date phrase is read as an hour before the date overwrites the
day/month/year onto it, so «στις 12 Ιουνίου» (with no am/pm part) resolves
to noon rather than the 9am default a dateless reminder gets. This already
happened for the forward (time-before-«ότι») phrasing; the fix here keeps
it symmetric rather than fixing it, since that's a distinct ambiguity
(hour-vs-day-of-month) from the one this fix was asked to close.

Pinned in `tests/test_memory_parse.py`'s `ReminderTests`: evening and noon
rolling to pm, a spelled-out hour, the time after both «ότι» and «να», the
date variant and its inherited noon quirk, and a genuinely unparseable
reminder still coming back `None` rather than a guessed time.

**Hand-tested on live audio on 2026-10-03, and a third bug fell out of the
test itself.** «Υπενθύμισέ μου ότι φοράω τα γυαλιά μου στις 8 το βράδυ»
came back through Whisper as «Υπερθήμησε μου ότι φοράω τα γυαλιά μου στις
οκτώτο βράδυ.» — two separate kinds of noise in one utterance. The verb
mangled ν→ρ («υπερθήμησε» for «υπενθύμισε»), caught only because
`RE_REMIND_ABS.search()` is unanchored and happens to find «θιμισε μου» as
a substring inside the garbled word — incidental, pre-existing, and out of
this fix's scope. But «οκτώ» and «το» came back **glued into one word**
with no space at all («οκτώτο»), and `RE_CLOCK`'s part group required a
literal `\s+` before «το βράδυ» — so the hour matched («οκτώ» is still a
prefix of «οκτώτο») but the evening marker silently didn't, and the
reminder would have fired at 8am tomorrow instead of 8pm that same night.
Exactly the kind of silent-wrong-time failure this whole area exists to
prevent, found by the hand test it was built to pass.

Fixed by reusing `_JOIN` (already built for "zero or more separators",
which covers a vanished one as well as an inserted one) as the gap before
`part` and inside it, in place of a bare `\s+` — `_GAP`/`_JOIN`/`_OTI`
moved up the file ahead of `RE_CLOCK`, since it now needs them before
`RE_TRIGGER` does. Re-ran the exact live transcription after the fix:
correctly resolves to 8pm the same day. Pinned as
`test_glued_hour_and_part_still_reads_as_pm`.

### Tags

Every content table carries a `tags` column: the row's life area, so
«τι έχω σήμερα» can pull from every table at once instead of needing to
know which table holds what. `MEMORY_TAGS` in `config.py` is the
vocabulary — a closed set, because these are *your* areas and a fixed list
means a typo cannot invent a tag nothing will ever search for.

A tag comes from two sources, in order: **the table**, which is exact and
free (a course or exam is `university` by construction, a business row is
`business`), then **keywords** matched against the row's already-folded
`norm`. `memory.tags_for()` is called from `save()` — the one place every
write passes through, and the point at which `norm` is finished.

This is `_TABLE_KEYWORDS`' idea done properly. That one keeps a row
findable by the generic word that filed it, by stuffing the word into the
search key; a tag is the same classification in a column of its own, where
it can be *selected on* rather than only matched.

Three things are deliberate:

- **Tagging is additive and never gates a save.** A row nothing matches
  keeps no tag (`NULL`, not `""`), lands in the same table as before, and
  is still found by keyword search. Matching is exact-substring, not fuzzy:
  the asymmetry that justified edit distance for the kill switch — a miss
  means the safety feature didn't work — does not hold here, where a missed
  tag only means the row is found the way it was found before tags existed.
- **The stored form is comma-delimited *and* comma-terminated**
  (`,cafe,university,`). The sentinels are what let `LIKE '%,cafe,%'` match
  a whole tag rather than a prefix of another one — without them
  `marketing` matches `ai_marketing`. `memory.tag_like()` builds the
  pattern; a test pins that exact pair.
- **A query naming two areas filters by neither.** `tag_of_query()` returns
  `None` on ambiguity, because filtering by one of two named areas answers
  a question nobody asked, and showing everything is the recoverable
  direction.

**The migration is the first that needed a real `ALTER`.** `_SCHEMA` is all
`CREATE TABLE IF NOT EXISTS`, which is how `audit` and `policy_state`
arrived without a version bump — but `IF NOT EXISTS` skips the *whole*
statement for a table that already exists, so nothing in `_SCHEMA` can ever
reach an existing one. A new column needs `ALTER TABLE ... ADD COLUMN`,
and needing it is exactly what earns `SCHEMA_VERSION = 3`. The `ALTER` is
guarded by `db._has_column()`, because `_migrate()` is not transactional
across its steps: a crash between the `ALTER` and the version stamp would
otherwise make every later start fail on a duplicate column, with no way
back short of editing the database by hand. Existing rows are backfilled
from their stored `norm`, so tags work on the first run rather than only
for rows saved afterwards.

### The agenda

`memory.agenda(conn, day, tag=None)` returns `(kind, text)` for everything
dated that day, from every table that carries a date. `_due_today()` is now
a rendering of it for the brain's memory block, and the `agenda` skill
speaks it for «τι έχω σήμερα» / «αύριο» / «μεθαύριο», optionally narrowed
to one area («τι έχω σήμερα για το μαγαζί»). Day words come from
`memory.RELDAY`, so σήμερα/αύριο/μεθαύριο are spelled in one place.

It is registered **before** `memory_recall`: «τι έχω σήμερα» is the
narrower question, and the generic recall would answer it with a keyword
search that knows nothing about dates. The phrases are whole («τι έχω
σήμερα»), never a bare «τι έχω», which would swallow ordinary questions.

**A reminder appears whatever its status**, with a fired one marked
«(έγινε)» and a missed one «(χάθηκε)». The question is what the day holds,
not what is still queued — and since the scheduler began claiming rows,
filtering on `pending` would have made a 9am reminder invisible by 10am.
That is the loose end the scheduler left, closed here.

Known boundary: the agenda is *dated* items only. A café note with no date
is tagged `cafe` and found by keyword search, but «τι έχω σήμερα για τον
καφέ» will not list it, because it is not on for today.

### The weekly class schedule

`class_schedule` is a different shape of table from every other one here:
every other table's agenda entry is tied to a `due_date` (an exam) or a
`due_at` (a reminder) — one specific calendar day. A class recurs every
week on the same weekday, so the row has no date at all, only a `weekday`
column (0-6, Python's `date.weekday()` convention — Δευτέρα=0 .. Κυριακή=6,
the same convention `memory.WEEKDAYS`/`RE_DATE_WDAY` already use for spoken
weekday names). `agenda()` matches it on `day.weekday()` instead of `day`
itself, which is the whole reason this needed its own table rather than a
column on `courses`: `courses` already exists and is dateless by design
(a semester-long enrolment fact), and bolting a single weekday/time onto it
would still only describe one weekly slot per course, when the real
schedule needs day *and* time *and* room *and* professor together, and (a
smaller point, but real) a course meeting three times a week needs three
rows, not one.

Columns: `course`, `weekday`, `start_time`, `end_time`, `room`,
`professor`, `semester` — the last four nullable, since not every source
lists all of them. `end_time`/`room`/`professor` are folded into one
rendering, `memory.render_class()`, shared by `agenda()`, the
`class_schedule` entry in `_SEARCHABLE`, and `mem._summarize()` (via
`memory.WEEKDAY_NAMES`), so the three don't drift into three different
phrasings of the same row.

Wired into `agenda()` like any other table: tagged `university` by
`_TABLE_TAGS` (the same implicit tag `courses`/`exams` get, whatever the
row's own text says), searchable through the generic keyword path via
`_TABLE_KEYWORDS["class_schedule"]` (`"μάθημα ωρολόγιο πρόγραμμα"`, the
same idea as `courses`' own keyword entry — findable by the generic word
even though the row's own text may never say it), and listed/shown/edited/
deleted through `:mem ... class_schedule` like any other content table —
`db.CONTENT_TABLES["class_schedule"] = "course"` is what makes that work.

**Classes are listed first in an agenda, and time-ordered.** They anchor
the shape of the day the way exams and reminders don't, so `agenda()`
returns them before exams and reminders, each in `start_time` order —
sorted in SQL (`ORDER BY start_time`), a plain string sort that works
because every stored time is `HH:MM`, 24-hour, zero-padded. The spoken form
(`skills._handle_agenda`) prefixes each with «μάθημα», the same way an exam
is prefixed with «εξέταση»; the brain's memory block
(`memory._due_today()`) does the same with «Σήμερα μάθημα: …».

**No voice save trigger yet.** Unlike every other table here, there is no
spoken «θυμήσου ότι έχω μάθημα Τρίτη στις 5» pattern that writes into
`class_schedule` — the source of truth is the user's actual
program-of-studies document, not something worth parsing out of speech with
all the ambiguity `RE_COURSE_OF` already has to fight for `courses`. Rows
are seeded, the way `tools/seed_courses_2026_2027_sem1.py` seeded `courses`:
`tools/seed_class_schedule_2026_2027_sem1.py` builds a `Parsed` per row
(`table="class_schedule"`) and calls `memory.save()`, so a seeded row gets
the same tags/timestamps a real save would. `ROWS` there is 13 entries, from
the user's real χειμερινό 2026-2027 (1ο εξάμηνο) schedule image — every one
of the 7 seeded courses, but 13 rows because several meet more than once a
week: Προγραμματισμός Η/Υ has one lecture plus three separate lab groups
(Ομάδα Α/Α2/Α3, each its own time and mostly its own professor pairing),
Υδατική Χημεία has a separate lecture and lab session, and Μαθηματικά
Ι/Βιολογία - Οικολογία each meet twice a week as the same plain lecture.
Keyed to idempotency on `(course, weekday, start_time)` rather than `id`,
same idea as `seed_courses`' `(name, semester)`, so running the script twice
adds nothing the second time.

Two courses' schedule cells name no professor at all — Εισαγωγή στην
Επιστήμη/Εισαγωγή στις Εργαστηριακές Πρακτικές, both nullable `professor`
by design, not a transcription gap. Where a course's multiple weekly
sessions would otherwise be indistinguishable in `class_schedule` (no
column for lecture-vs-lab or lab group), that distinction is folded into
`course` itself as a parenthetical (`"Προγραμματισμός Η/Υ (Ε, Ομάδα Α)"`)
rather than left implicit in the room/time alone — the seed script's own
docstring has the full reasoning and the room-code legend (Β1-Β7 = ΠΡΟΚΑΤ,
ΥΚ = the department's Υπολογιστικό Κέντρο in Κιμμέρια).

Run once, locally, to populate the real database:
```
.\.venv\Scripts\python.exe tools\seed_class_schedule_2026_2027_sem1.py
```
Run and hand-tested live on 2026-09-30: 13 rows seeded, then «Τι έχω
σήμερα» (a Wednesday) and «Τι έχω αύριο» each answered correctly from the
real database — right classes, right time order within the day, room and
professor spoken along with each.

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

**Schema.** `SCHEMA_VERSION` is 3. `db.connect()` migrates an older database
in place on the next start (`db._migrate`); version 2 refolds every stored
`norm` (see "Normalization"), version 3 adds and backfills `tags` (see
"Tags").

**Backups.** `db.backup()` uses SQLite's backup API (safe against a
concurrent writer) into `BACKUP_DIR` (`data/backups/`), keeping the newest
`BACKUP_KEEP` (5). Runs once at startup, wrapped so a failure never stops
Jarvis from starting, and on `:mem backup`. Filenames are microsecond-stamped
so they are unique and sort chronologically — pruning deletes the oldest by
name, and a coarser stamp let a pruned name be reused and overwritten.

## Research

Phase 5 steps 1-4 (see "Roadmap"): «κάνε έρευνα για …» runs several web
searches on a topic through the Claude API and speaks back a structured
summary, which is also kept as a dated `expertise` row so later questions can
draw on it. Not yet hand-tested on live audio — built and unit-tested only.

**Always the Claude API, whatever `BRAIN_PROVIDER` is set to.** Ollama has no
way to reach the internet at all, so there is no local fallback the way
`_ask_claude`/`_stream_claude` have one — `brain.research()` either reaches
Anthropic or raises `ResearchUnavailable`, which the skill speaks verbatim. A
missing `ANTHROPIC_API_KEY` is therefore a per-*call* refusal here, not the
import-time failure `BRAIN_PROVIDER=claude` gets from `brain._check_key()`:
research is opt-in on its own, independently of which brain answers ordinary
questions, and `CLAUDE_RESEARCH_MODEL`/`RESEARCH_MAX_SEARCHES`/
`RESEARCH_MAX_TOKENS` (`jarvis/config.py`) are its own knobs, defaulting to
`CLAUDE_MODEL` rather than inventing a second model setting before there is a
reason to tell them apart.

**web_search is a server tool.** Anthropic runs the searches itself and feeds
the results back inside the one `messages.create()` call
(`tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses":
RESEARCH_MAX_SEARCHES}]`) — there is no client-side tool-use loop for this
process to drive, unlike a tool it would have to execute itself. `RESEARCH_
SYSTEM_PROMPT` asks for the reply to be *only* the structured summary, but the
model still writes "I'll search for..." text between search rounds, so
`brain._final_text()` keeps only the text blocks *after* the last
`web_search_tool_result` block — everything before that is thinking out loud
about what to search next, not the finding. Falls back to every text block
when there was no search at all, so a reply that never searched is still
returned rather than silently dropped.

**The trigger is parsed, not matched by substring**, the same shape as
`memory.parse()`'s ladder but deliberately not part of it: research triggers
an action (a real API call, a real charge) rather than filing a fact, so
`memory.parse_research()` is its own pure function returning `(topic,
is_refresh)` — `RE_RESEARCH` for «κάνε έρευνα για …» / «ερεύνησε …» / «ψάξε
στο ίντερνετ για …», `RE_RESEARCH_REFRESH` for «ξανακάνε έρευνα για …» *and*
«κάνε ξανά έρευνα για …», tried first. A live hand test asked for the second
spelling and it fell through to the brain, because only the glued-word form
was recognized; `RE_RESEARCH_REFRESH` now carries both as alternatives.
Neither can be shadowed by `RE_RESEARCH` by accident, and not for the same
reason: "ξανακάνε" is one word to the recognizer, so it never starts with
"κανε" the way `RE_RESEARCH`'s anchor requires — the same non-overlap
`RE_TRIGGER`'s rungs rely on — while "κάνε ξανά έρευνα" *does* start with
"κανε", but `RE_RESEARCH`'s own alternative demands "ερευνα" immediately
after it and "ξανά" sits in between, so it would refuse the string even if
tried first. Checking refresh first is still what makes relying on that
second argument unnecessary. The topic is read back through `Norm.group()`,
verbatim (accents, capitals), for the same reason every other capture in
`memory.py` is: `"το ΕΚΠΑ"` searches better than `"το εκπα"`.

**The `expertise` table is keyed by topic, upserted, not appended.**
`memory.save_expertise()` is the same shape as `save()`'s `profile` branch:
`topic_key` (the topic alone, normalized) is `UNIQUE`, so researching a topic
again — the refresh trigger — updates the row in place rather than piling up
duplicate summaries, which is what "a stale topic can be redone on request"
(the roadmap's wording for step 4) actually means. `norm` is built from
*topic and summary together*, unlike `topic_key`, so a keyword search over a
word that only appears inside the summary can still surface the row — wired
into `memory._SEARCHABLE` (step 3), the same list `recall()`'s keyword hits
and the spoken `memory_recall` skill already read. `db.CONTENT_TABLES` and
`mem.py`'s `_summarize()`/`_refresh_norm()` all know about it too, so
`:mem list/show/edit/del expertise` work like any other table — except
`topic_key` is protected from direct edits (`mem._PROTECTED_COLUMNS`), since
editing it by hand would desync it from `topic` and silently break the next
upsert.

**The upsert used to be keyed on the exact spoken topic, not "the same
subject" — now softened for the refresh path only.** A refresh whose topic
Whisper transcribes differently from the original request (different
words, not just a different fold) gets a different `topic_key` under a
plain upsert and lands as a *second* row rather than updating the first,
even though a person would call it the same topic. Measured live:
«Ξανακάνε έρευνα για γυμναστήρια στην Ξάνθη» came back mangled as «...για γη
μου να στείρει, ας την ξάνθει» — the refresh verb still matched (the comma
survived via `_JOIN`), but the captured topic no longer normalized to the
same key as the original research. Nothing on the trigger side can fix
this — the topic *is* whatever was said — so `memory.find_similar_expertise()`
fixes it on the storage side instead, consulted only when `_handle_research()`
already knows it's a refresh (never on a first-time «κάνε έρευνα», where a
wrong merge would silently overwrite an unrelated topic's summary — worse
than the duplicate row this was living with). Same two-part shape as the
kill switch's fuzzy rungs: a generous half — `stems_of()`, the same stemmer
`search()`'s keyword hits already use, so mangled words around the real
content are tolerated — and a guard that must independently confirm it:
the shared stems must cover at least half of the *existing* row's own
stems, so one incidental shared word against a longer, unrelated topic
can't pass, and two candidates tying for best score refuse rather than
guess which one was meant. Re-run against the exact live transcription
above (now `FindSimilarExpertiseTests.test_the_live_boundary_case_now_matches`):
matches, on the strength of «ξάνθει» folding to the same stem as «Ξάνθη»
surviving as half of the original row's two content stems.

**`research` is the first live `Permission.CONFIRM` skill** (see "Policy" —
every other registered skill is `SAFE`). This is real network traffic with a
real bill attached, unlike anything else in `SKILLS`, so `policy.dispatch()`
only reaches `_handle_research()` once `RESEARCH_CONFIRM_PROMPT` ("Αυτό θα
ψάξει στο διαδίκτυο και έχει κόστος. Να προχωρήσω;") has been answered yes —
`_research_matches()` recognizes the trigger without acting, exactly what
`Skill.matches` exists for. If the research itself succeeds but the database
write fails, the finding is still spoken: losing the row is worse hidden than
said out loud, since it was already paid for either way.

`tests/test_research.py` pins all of it: the parser (including the refresh/
plain non-overlap and the verbatim topic), the `expertise` upsert against a
real temp database, `brain._final_text()`/`brain.research()` against a fake
client (same idiom as `tests/test_brain_claude.py` — no key, no SDK, no
network needed to run it), and the skill wired through `policy.dispatch()`'s
CONFIRM gate end to end (confirmed, declined, no asker installed, the
`ResearchUnavailable` and generic-error paths, and the audit row).

## The GUI shell

Phase 6 step 3 — "build a functional shell first (empty states, no logic)" —
is built: `jarvis/gui/` is its own package, and `gui_main.py` at the repo root
is its entry point, parallel to `main.py` rather than replacing it. Nothing
in `jarvis/gui/` is imported by `main.py` or vice versa, so the Enter-press/
wake-word CLI stays the always-available path while the GUI is built out one
step at a time.

```
.\.venv\Scripts\python.exe gui_main.py
```

**The framework is PySide6** (Qt for Python), decided — per the roadmap's own
step 2, "the UI framework decided by an actual comparison, not assumed" —
against a lightweight web-shell (pywebview + a local server). A single-
machine, local-first assistant has no reason to run an HTTP server for
itself, and Qt's signal/slot model is built for exactly the pattern this
needs: a background thread finishes listening/thinking/speaking, the UI
thread is told. PySide6 over PyQt6 for the license alone — LGPL, genuinely
free, rather than PyQt6's GPL/commercial split.

**The audit that preceded it found nothing to change outside `main.py`.**
`listener.py`, `brain.py`, `speaker.py`/`player.py`, `skills.py`, `policy.py`,
`memory.py`, `scheduler.py`, `db.py` and `diag.py` were already decoupled from
the CLI — no `print()`s baked into the logic, no assumption about who's
calling — and need zero changes to work under a GUI. Only `main.py` is
GUI-incompatible, and only because of its shape: a single blocking loop with
no state machine, no threading, and terminal `print()` as its only output.
That is why the GUI is its own parallel entry point rather than a rewrite of
`main.py` in place — the old loop still works exactly as documented above.

**`jarvis/gui/main_window.py`'s `MainWindow` is the shell**, and it is
deliberately inert: a menu bar (Αρχείο/Έξοδος; a Ρυθμίσεις placeholder dialog;
a checkable-but-unwired "Λειτουργία αποσφαλμάτωσης" debug toggle), a status
label fixed at "Κατάσταση: Αδρανές", a splitter with an empty transcript list
on the left and a side panel on the right — quick-action buttons sourced from
`config.SKILL_SITES`/`SKILL_APPS` (disabled, not wired to a no-op: a button
that looks clickable but does nothing is worse than one that's honestly not
ready yet), an empty tasks list, and CPU/RAM labels reading "—" rather than
"0%", since a real reading of zero and "nothing has measured this yet" must
not look the same on screen. Every widget something will later need to find
has a stable `objectName()` — centralized as module-level constants
(`STATUS_LABEL`, `TRANSCRIPT_LIST`, `TASKS_LIST`, `CPU_LABEL`, `RAM_LABEL`,
`DEBUG_ACTION`) — and a `widget(window, object_name)` helper looks one up,
raising `LookupError` rather than returning `None` on a miss.

Steps 4 and 5 are what give this content: wiring the microphone/brain/TTS in
one at a time (each tested before the next), then the state machine, real
system monitoring (`psutil`, most likely), the tasks list (rendering
`memory.agenda()`, already built and voice-driven, as a list instead of
speaking it), quick actions (wiring a click to `skills._open_site()`/
`_open_app()`), and settings/debug mode (a real surface over what
`jarvis/config.py` reads from `.env`) — in that order, never all at once.

**Step 4, part 1 of 3: the microphone.** A "Εγγραφή" button calls
`listener.listen()` — the same Enter-press one-shot path `main.py` uses —
and appends `"Εσύ: {text}"` to the transcript list exactly as the CLI prints
it, once transcription comes back; `None`/empty is a silent no-op, the same
as `main.py`'s own `if not text: continue`. `brain.ask()`/`speaker.speak()`
are deliberately not wired in yet — that is the rest of step 4, each tested
before the next, not this one — so the button records and transcribes but
Jarvis doesn't yet answer.

**`listener.listen()` runs on a `QThread`, never on the UI thread.** It
blocks for as long as the user is speaking plus however long Whisper takes,
and a blocked UI thread in Qt is a frozen, unresponsive window. `_ListenWorker`
(`jarvis/gui/main_window.py`) runs `listen()` on its own thread and reports
back over a `Signal(object)` — `object` rather than a typed signal because
`listen()`'s own return type is `str | None`, and Qt's typed signals need one
concrete type; the slot re-narrows it. Qt marshals a signal emitted on a
worker thread onto the UI thread automatically, which is the one safe way to
touch a widget from work that started elsewhere — `MainWindow` itself never
calls `listener.listen()` directly, only through the worker. The button
disables itself and the status label reads "Κατάσταση: Ακούω..." for the
duration, both restored when the signal arrives; a second click while one
recording is already in flight is a no-op (`_listen_thread is not None`),
since `listener.listen()` isn't reentrant — one ffmpeg process, one temp wav
path. `closeEvent()` waits for an in-flight thread before closing, so the
window is never torn down out from under a running recording.

`tests/test_gui_shell.py`'s `MicrophoneWiringTests` pins all of it with
`listener.listen` mocked (never a real microphone in the suite): a spoken
turn appends the right transcript line and restores the button/status; a
`None` turn appends nothing; and a second click while listening is ignored.

**Hand-tested live on 2026-10-03.** «Γεια σου Τζάρβις», spoken into the real
microphone after clicking "Εγγραφή", came back through the real pipeline —
`[timing] Recording: 8.77s`, `[timing] Whisper load: 4.05s`,
`[timing] Transcription: 2.10s`, the same lines `main.py` prints — and
appeared in the transcript list as `"Εσύ: Γεια σου Τζάρβις"`. No reply
followed, correctly: `brain.ask()`/`speaker.speak()` were parts 2 and 3 of
this step, not yet wired at that point. Step 4 part 1 is closed.

**Step 4, part 2 of 3: the brain's reply.** `_get_reply()` re-derives
`main._answer()`'s text-generation half rather than importing `main.py`
(the two entry points stay uncoupled, per "The audit that preceded it",
above): a skill first (`skills.handle()`, which already runs the kill switch
through `policy.intercept()`), the `policy.is_frozen()` backstop
`main._answer()` also keeps, then the brain
(`brain.ask(text, memory.recall_safe(text))`) when nothing matched, with
`policy.record("brain", "allowed", "no_skill_matched")` logging the same
audit row the CLI does. A failed brain call returns the same fixed line
`main.py`'s own `BRAIN_ERROR_REPLY` is (duplicated as a local constant, for
the same uncoupling reason, not imported). It returns the reply text rather
than speaking it — `speaker.speak()`/TTS is part 3, not this one — so a
reply now appears as `"Jarvis: …"` in the transcript, silently, with nothing
spoken aloud yet.

**`_BrainWorker` runs `_get_reply()` on its own `QThread`, same reasoning as
`_ListenWorker`.** `brain.ask()` blocks on a local model or a network round
trip, and either one on the UI thread freezes the window. The status label
reads "Κατάσταση: Σκέφτεται..." for the duration, and the record button
stays disabled across *both* halves of a turn — a click landing after
transcription but before the reply comes back must not start a second
recording, so `_start_listening()`'s guard checks `_brain_thread` as well as
`_listen_thread`. `closeEvent()` waits on both threads before closing, for
the same reason it already waited on one.

`tests/test_gui_shell.py`'s `BrainWiringTests` pins `_get_reply()`'s four
branches with `skills.handle`/`policy`/`brain.ask` all mocked (never a real
skill, database or model in the suite): a matching skill answers without
calling `brain.ask()` at all; no match falls through to the brain; the
frozen backstop produces no reply line; and a raised exception falls back
to the fixed error line. `MicrophoneWiringTests` is updated alongside it —
a spoken turn now also waits out the `_brain_thread` it triggers, and a new
test pins the "thinking" half of the double-click guard.

**Hand-tested live on 2026-10-03, with the Claude brain behind it.**
`BRAIN_PROVIDER=claude` in this run's `.env`, so the real path exercised was
skill-miss → `policy.is_frozen()` backstop → `brain.ask()` → the Anthropic
API, not the Ollama fallback — a different branch from the one the
microphone hand test above exercised (which never reached the brain at
all). Spoken turn, real transcription, then
`[timing] Claude response: 20.38s (in 1562 tok, out 18 tok)` on the
terminal and a real reply appended to the transcript as `"Jarvis: …"` —
confirming `_get_reply()`'s brain branch, `_BrainWorker`'s threading, and
the audit row it logs all work end to end, not just against the mocks in
`BrainWiringTests`. Step 4 part 2 is closed.

**Step 4, part 3 of 3: the voice.** `_SpeakWorker` runs `speaker.speak(reply)`
on its own `QThread`, the same reasoning as the other two workers: `speak()`
blocks for the full round trip (Edge's network call, or Piper's fallback)
plus however long the audio takes to play, and that on the UI thread freezes
the window for the same span. `_on_reply_finished()` now appends the
`"Jarvis: …"` transcript line and switches the status to
"Κατάσταση: Μιλάει..." *before* starting it — the text is already decided the
moment the brain answers, and there is no reason to make the user wait to
read it until the audio has also finished. The record button and the thread
guard in `_start_listening()` now cover all three legs (listening, thinking,
speaking): a click landing at any point in that sequence is a no-op, not
just during the first two. `closeEvent()` waits on `_speak_thread` too, for
the same reason it already waited on the other two. An empty reply (the
frozen backstop's `""`) starts no `_SpeakWorker` at all — there is nothing
to say and nothing to wait on.

`tests/test_gui_shell.py`'s new `SpeechWiringTests` pins `_on_reply_finished`
directly: a non-empty reply shows the transcript line and the "speaking"
status before `speak()` even returns, calls `speaker.speak()` with exactly
that text, and the button/status recover once the (mocked) worker finishes;
an empty reply starts no thread and touches neither the transcript nor
`speaker.speak`. `MicrophoneWiringTests` and `BrainWiringTests` are updated
to mock `speaker.speak()` throughout (a real TTS call during either of those
would otherwise now fire from the reply they produce) and to wait out
`_speak_thread` where one gets started, plus a new
`test_a_click_while_speaking_is_ignored` pinning the third leg of the
double-click guard.

**Hand-tested live on 2026-10-03, voice and grounding both confirmed in one
conversation.** Three turns through the real GUI, heard out loud and read in
the transcript: a wake-style greeting answered naturally; asked what stage of
its own construction it was in, it said it doesn't know much about its own
technical details or how it's built — a flat refusal rather than an invented
answer, exactly the subject-not-confidence line "Grounding" draws, now
confirmed through the GUI's `_SpeakWorker` path and not just the CLI's; asked
how old the user is, it said it doesn't know that either — nothing in the
recalled memory block to answer from, and no guess offered in its place.
Confirms `_SpeakWorker` actually plays Edge/Piper audio end to end (not just
calling `speaker.speak()` without error, which the mocks already covered),
and that a GUI turn behaves the same as a CLI one on the one thing that
matters most for a voice assistant: refusing to make things up. Step 4 part 3
is closed, and Phase 6 step 4 as a whole — microphone, brain, voice, all
three wired in and hand-tested one at a time — is done.

**Step 5, piece 1: the state machine.** Before this, "what is Jarvis doing
right now" had no single answer — it was three separate questions
(`_listen_thread is not None`? `_brain_thread`? `_speak_thread`?), asked
anew at every guard and every status-label update, with the status text and
the record button's enabled state set by hand at each of the four
transition points. `State` (module-level `Enum`: `IDLE`, `LISTENING`,
`THINKING`, `SPEAKING`) and `MainWindow._set_state()` collapse that into one
value and one call site: every transition now calls `_set_state(state)`,
which looks the status text up in `_STATUS_TEXT` and enables the record
button iff the new state is `IDLE` — so the label and the button can no
longer drift apart the way three separate call sites eventually would have.
`_start_listening()`'s guard is `self._state is not State.IDLE`, replacing
the three-attribute check. The three thread attributes
(`_listen_thread`/`_brain_thread`/`_speak_thread`) still exist — `closeEvent()`
still needs something to `wait()` on — but nothing outside `closeEvent()`
reads them as a None-check anymore; `_state` is the one source of truth for
that question now, which is what a later step (debug mode's own status
surface, most likely) will read rather than reaching into three private
attributes.

`State` is exported at module level, not nested in `MainWindow`, for the
same reason the object-name constants are: a later step and this file's own
test both need to name it without an instance in hand.
`tests/test_gui_shell.py`'s new `StateMachineTests` pins `_set_state()`
directly — every state's status text, and that only `IDLE` leaves the
record button enabled — and `MicrophoneWiringTests`/`BrainWiringTests`/
`SpeechWiringTests` all gained a `window._state` assertion at the point in
each turn where a specific state is expected, alongside the status-text and
button-enabled assertions that were already there. No behavior changes: the
status strings and button states a turn produces are identical to before
this refactor, which is the point — this is a structural change, not a new
feature, so nothing it's not meant to touch should move.

Not yet hand-tested live — this piece has no new observable behavior to
hand-test (the status text and button timing are unchanged), so the mocked
suite is the whole bar for it, unlike the three pieces of step 4 before it.

**Step 5, piece 2: real system monitoring.** The side panel's CPU/RAM labels
have read a fixed "—" since step 3, deliberately — a real reading of zero
and "nothing has measured this yet" must not look the same on screen, and
step 3's own brief was an empty shell, no logic. `_poll_system()` is the
logic: it reads `psutil.cpu_percent(interval=None)` and
`psutil.virtual_memory().percent` and writes both into `CPU_LABEL`/
`RAM_LABEL` as `"CPU: 17%"`/`"RAM: 42%"`. It runs on a `QTimer`
(`_monitor_timer`, `MONITOR_POLL_MS` = 2000ms), not a `QThread` — unlike
`listener.listen()`/`brain.ask()`/`speaker.speak()`, a `psutil` read here is
a near-instant syscall-level number with no disk or network I/O behind it,
so it never blocks the UI thread and doesn't need the "not until asked"
discipline the three worker threads do. The timer starts at construction
(not on any user action) and is stopped in `closeEvent()` alongside the
`.wait()` calls on the three worker threads.

**`cpu_percent(interval=None)`'s first call in a process is a known trap,
documented by `psutil` itself**: with no prior call to measure *from*, it
reports usage since the process started rather than since "now" — a
meaningless, usually-near-zero number on a GUI app that just launched.
`MainWindow.__init__` makes one throwaway priming call before the timer
ever starts, so the first real tick (one `MONITOR_POLL_MS` later) measures
since construction instead of since process start. `MonitoringTests` pins
this indirectly by mocking `psutil` outright rather than asserting on real
numbers, which would make the suite flaky by definition — one test confirms
the timer is active at the right interval, one confirms `_poll_system()`
reads both `psutil` calls and writes both labels, one confirms closing the
window stops the timer.

This piece does introduce new, real, user-visible behavior — the "—"
placeholders become actual moving percentages — unlike the state machine
above, so it is not being called closed on the mocked suite alone the way
that piece was. Flagged for a quick visual hand test: open the GUI, confirm
the CPU/RAM labels show real numbers that change over a few seconds rather
than sitting at "—" or a frozen value.

**Hand-tested live on 2026-10-03.** CPU/RAM read 12% and 82% respectively on
the real machine, confirming `_poll_system()`'s psutil path end to end.

**VRAM, added the same day on request.** `psutil` has no concept of GPU
memory at all — that needs a GPU-specific library, and which one depends on
the vendor. The user's machine has an NVIDIA GPU, so `_read_vram_percent()`
uses `pynvml` (the `nvidia-ml-py` package) — `nvmlInit()` once, then
`nvmlDeviceGetMemoryInfo()` on every poll. Imported lazily, inside the
function, same idiom as `speaker._get_voice()`'s Piper import: a machine
with no NVIDIA GPU — every environment this project's test suite runs in,
included — never needs the module loaded at all.

**The whole function is one `try`/`except`, and the first failure latches.**
No card, no driver, or the package not installed are all the same outcome
here: `None`, which `_poll_system()` renders as "VRAM: μη διαθέσιμο" rather
than leaving the label on its construction-time "—" forever (a frozen "—"
would read as "not measured yet", when the true state is "measured, and
there's nothing there"). `_nvml_unavailable` is a module-level flag that,
once set, skips straight to `None` on every later call rather than retrying
a failing `nvmlInit()` on every `MONITOR_POLL_MS` tick — same discipline as
`diag.py`'s write-failure flag: a missing GPU costs one failed call per
process, not one per poll. The device handle itself is also cached
module-level once fetched, for the same reason.

`GpuMonitoringTests` fakes `pynvml` in `sys.modules` (same idiom
`tests/test_player.py` uses for `sounddevice`) to pin `_read_vram_percent()`
directly: a present GPU reads a real percentage from the faked memory info;
a missing package (no fake installed, so the lazy `import pynvml` raises
`ImportError` exactly as it does on a machine without `nvidia-ml-py`)
reports unavailable; a present package with no usable device (`nvmlInit()`
raising) reports unavailable the same way; a failure is never retried
within the process; and the device handle is fetched once across repeated
calls, not once per call. `MonitoringTests`' own `_poll_system()` test is
split in two and a third added, mocking `_read_vram_percent()` wholesale
rather than `pynvml` — the right level for pinning what `_poll_system()`
does with a reading, as distinct from how that reading is produced.

**Hand-tested live on 2026-10-03.** Confirmed on the real machine: the VRAM
label read a real percentage, not "μη διαθέσιμο", against the user's NVIDIA
GPU. Step 5 piece 2 (CPU, RAM and VRAM, all three live) is closed.

**Step 5, piece 3: the tasks list.** The side panel's tasks list has been
empty by design since step 3. `_load_tasks()` fills it with today's
`memory.agenda()` — the same call the `agenda` skill already makes for
«τι έχω σήμερα» (see "Memory" → "The agenda") — rendered as a list instead
of spoken as a sentence. `_render_agenda_item()` mirrors that skill's own
phrasing so an item reads the same whether it's heard or shown: an exam
prefixed "εξέταση", a class "μάθημα", a reminder shown as its own text
(its fired/missed suffix, if any, is already baked in by `memory.agenda()`
itself). It's a second, hand-kept copy of that phrasing rather than an
import from `skills.py` — small enough that duplicating it costs far less
than coupling the GUI to a private module's inline rendering would.

**Narrowed to today only**, not the αύριο/μεθαύριο a spoken «τι έχω αύριο»
can ask for — the step only asks to render `memory.agenda()` as a list,
not to rebuild the day-picking logic `_agenda_day()` already owns in
`skills.py`. A tab or filter for other days is further than this piece
goes.

**Refreshed at two points, not on a timer.** Once at construction (so the
panel isn't empty for longer than it has to be), and again at the end of
every completed turn (`_on_speak_finished()`) — a turn can itself have just
saved the exam/reminder/class that would change what today holds (e.g.
«θυμήσου ότι έχω εξέταση σήμερα στα μαθηματικά», spoken through the GUI's
own microphone). Polling a database that only changes when a turn changes
it would be `_poll_system()`'s reasoning applied somewhere it doesn't fit —
CPU/RAM/VRAM genuinely drift on their own between ticks; today's agenda
does not.

**Every error is swallowed, same discipline as `memory.recall_safe()`.**
A locked or broken database means the tasks list doesn't refresh this
time, never a crashed GUI or a popup nobody asked for — `_load_tasks()`
clears the list first, then lets any exception from `db.connect()`/
`memory.agenda()` simply end the refresh with nothing added.

`TasksWiringTests` pins `_load_tasks()` with `memory.agenda()` mocked
directly: construction loads a handful of mixed-kind items in the order
given and renders each correctly; a database error leaves the list empty
without raising; and a completed turn (`_on_speak_finished()`, called
directly) picks up a changed agenda. `RenderAgendaItemTests` pins
`_render_agenda_item()`'s three branches on their own, no window needed.
Every other existing test in this file that completes a full turn now
also exercises `_load_tasks()` incidentally (since `_on_speak_finished()`
calls it) — `db.connect()` is patched once, for the whole test module
(`setUpModule()`/`tearDownModule()`), the same reason
`listener.listen()`/`brain.ask()`/`speaker.speak()` are mocked in every
individual test: so that no test here ever opens the real
`data/jarvis.db`. A bare `MagicMock`'s default `__iter__` (an empty
iterator) is what lets `memory.agenda()` come back `[]` against it with no
further mocking needed — which is exactly what the pre-existing
"tasks list starts empty" assertion in `ShellConstructionTests` still
relies on.

**Hand-tested live on 2026-10-03.** Confirmed on the real machine: the
tasks list shows today's agenda in the side panel. Step 5 piece 3 (the
tasks list) is closed.

**Built in a sandbox that cannot run it, hand-tested on the real machine
instead.** This sandbox cannot install PySide6 at all — `pypi.org`/
`files.pythonhosted.org` sit in the proxy's `noProxy` list, so requests go
direct and are refused — so nothing in `jarvis/gui/` was run here, only
syntax-checked with `python3 -m py_compile`, before being handed to the user's
Windows machine. `tests/test_gui_shell.py` exercises real widget construction
(`QT_QPA_PLATFORM=offscreen` lets it build widgets with no display attached)
wherever PySide6 is installed — skipped, not failed, in this sandbox, the
same shape as the rest of the suite being skippable-by-environment rather
than broken by it (see "Config"/"Providers" for the lazy-import idiom this
mirrors).

**Step 3 closed on 2026-10-03.** `pip install -r requirements.txt` then
`gui_main.py` on the real machine opened a window matching the shell exactly:
the menu bar, the "Κατάσταση: Αδρανές" status line, the empty transcript list
on the left, and the side panel's disabled quick-action buttons, empty tasks
list, and "—" CPU/RAM placeholders all rendered as built. The roadmap's own
hand-test rule is satisfied; step 4 (wiring in the microphone/brain/TTS, one
at a time) is next.

**Step 5, piece 4: quick actions.** The side panel's quick-action buttons
have been disabled placeholders since step 3 — one per entry in
`config.SKILL_SITES`/`SKILL_APPS`, so the panel always reflects what's
really configured rather than a separately-maintained list that could drift
from it, but with nothing behind the click.

**`skills._open_site(url)`/`_open_app(argv)` didn't exist before this piece**
— the voice skill's `_handle_open()` called `webbrowser.open(url)`/
`subprocess.Popen(argv)` inline. Extracted as their own functions first, as a
behavior-preserving refactor, so the GUI's buttons can call the *exact* code
the voice command does rather than a second copy of "how to open a site/app"
— unlike `_render_agenda_item()` in `main_window.py`, which is a deliberate
second copy because `skills.py`'s agenda rendering has no clean function
boundary to share, this one does, so it's shared rather than duplicated.
`_handle_open()` itself now calls these two functions instead of
`webbrowser`/`subprocess` directly; its own existing tests
(`tests/test_skills.py`'s `OpenSkillTests`, which patch `skills.webbrowser`/
`skills.subprocess` directly) pass unchanged, since the extraction doesn't
change which module-level names get called.

**Each button is now enabled and wired to a click handler**
(`_quick_action_site()`/`_quick_action_app()`) that calls the matching
function and reports the result in the transcript list, the same way a
spoken «άνοιξε το …» command's reply would: `"Jarvis: Άνοιξα το {label}."`
on success, `"Jarvis: Δεν μπόρεσα να ανοίξω το {label}."` if the call raises
— caught and shown rather than left to crash the GUI, same discipline as
`_load_tasks()` swallowing a database error. A click doesn't touch
`_state`/the state machine at all: it's a fire-and-forget action outside the
listen/think/speak turn cycle, not a conversation turn, so it never disables
the record button or any other control.

`QuickActionWiringTests` (`tests/test_gui_shell.py`) pins all of it, with
`skills._open_site`/`_open_app` mocked in every test so nothing here ever
opens a real browser or launches a real process: a site button is enabled
and calls `_open_site` with the configured URL, appending the right
transcript line; an app button is enabled and calls `_open_app` with the
configured argv; and a raised exception from either is caught and reported
rather than propagating.

**Hand-tested live on 2026-10-03.** Confirmed on the real machine: every
quick-action button (YouTube, Gmail, Google, υπολογιστή, Notepad) opens the
right site or launches the right app, with the matching "Jarvis: Άνοιξα
το …" line appearing in the transcript. Step 5 piece 4 (quick actions) is
closed.

**Step 5, piece 5 (the last of step 5): settings/debug mode.** The menu's
"Ρυθμίσεις…" item has opened a placeholder `QMessageBox` since step 3
("the real settings surface ... is settings/debug mode, the last of Phase
6's five steps. For now this just proves the menu structure is right.").
`_show_settings_dialog()` is that real surface: a `QDialog` with one
read-only row per setting (`QFormLayout`, label → `QLabel`), grouped the
same way CLAUDE.md's own "Config"/"Providers" sections are — brain
provider and model, TTS engine and voice, the Whisper model, then
wake word/barge-in/conversation-mode/scheduler/log on/off — read straight
from `jarvis/config.py`'s already-loaded values.

**Read-only is not a shortcut, it's the only shape consistent with the
project's own working rule "never edit `.env`".** A settings dialog that
*wrote* a changed value back to `.env` would be exactly that, done through
a dialog instead of a text editor. Seeing a wrong value here still tells
you to go fix `.env` by hand and restart — which is also the only way any
of these values ever take effect, since `config.py` reads `.env` once at
import time, not on a timer or a signal. A future `settings/debug mode`
that actually changes behaviour live (hot-swapping `BRAIN_PROVIDER`, say)
would be a different, larger feature than what this piece asks for.

**The debug toggle was checkable but inert since step 3; it now does
something real.** `DEBUG_ACTION`'s checked state
(`_debug_mode_enabled()`) gates one thing: after a turn's reply is shown
in the transcript, `_append_debug_line()` queries the single most recent
`audit` row (`SELECT action, decision, reason FROM audit ORDER BY id DESC
LIMIT 1`) and appends it as `"[debug] {action} → {decision} ({reason})"`.
This isn't a new logging path — `_get_reply()` already writes exactly one
audit row per turn (either from a matched skill's `policy.dispatch()`, or
the explicit `policy.record("brain", "allowed", "no_skill_matched")` line
already in `_get_reply()` itself, see "Step 4, part 2") — so "debug mode"
means surfacing the same audit log `:mem list audit` already exposes in a
terminal, in the one place a GUI user actually is. Off by default, so an
ordinary conversation's transcript is unchanged from before this piece.

Swallows every error, same discipline as `_load_tasks()`: a locked or
broken database costs this one debug line, never a crashed GUI — and a
turn the frozen backstop silenced (`_get_reply()` returning `""`) already
returns out of `_on_reply_finished()` before reaching this at all, so
there's never a stray `"[debug] ..."` line with no reply above it.

`DebugModeWiringTests` (`tests/test_gui_shell.py`) pins all of it: debug
mode is off by default; a reply with it off appends only the one
`"Jarvis: ..."` line; a reply with it on (and `db.connect()` mocked to
return a fake audit row) appends a second `"[debug] ..."` line with the
exact row's values; and a database error during that query leaves the
debug line out without raising. `SettingsDialogTests` mocks
`QDialog.exec()` (modal, and would otherwise block the test process
waiting on a window that never appears) and pins that opening the dialog
never raises, including against a patched `config.BRAIN_PROVIDER` — there
are no object names to assert on inside a `QFormLayout` of plain labels,
so the guarantee this class pins is "building it from the live config
never crashes", not any one row's exact text.

**Hand-tested live on 2026-10-03.** The settings dialog showed the real
values from the running `.env` (`BRAIN_PROVIDER=claude`,
`CLAUDE_MODEL=claude-haiku-4-5`, `TTS_ENGINE=edge`,
`TTS_VOICE=el-GR-NestorasNeural`, `WHISPER_MODEL=small`, wake word off,
barge-in/conversation mode/scheduler/diagnostic log all on) — nothing
guessed or hardcoded, confirming `_show_settings_dialog()` reads the same
`config.py` values the rest of the app runs on. With debug mode checked, a
real turn («Γεια σου Τζάρβις, τι γίνεται, όλα καλά;») answered normally
and appended `"[debug] brain → allowed (no_skill_matched)"` to the
transcript — confirming `_append_debug_line()` reads the real audit row
`_get_reply()` just wrote, not a mocked one. Step 5 piece 5 is closed.

**Phase 6 step 5 — state machine, system monitoring, tasks, quick
actions, settings/debug mode — is done in full, and so is Phase 6 as
originally scoped.**

### The dashboard redesign

Done after Phase 6 closed, before Phase 7, at the user's request rather
than anything on the roadmap: the shell above was functional but, in the
user's own words, "πολύ γραφικό" — plain, not the "εντυπωσιακή... premium"
multi-view platform look of a reference photo they sent. The scope they
set, through two rounds of clarifying questions: whichever implementation
gives the best premium result (left to judgment), and from the photo, only
its *top* dashboard portion — header, sidebar, central avatar, system
status/quick actions/current task panels. The photo's bottom section, eight
small explainer cards (Ακούει/Σκέφτεται/Απαντάει/... each with its own mini
illustration and caption) is marketing material about the assistant, not UI
for it, and the user confirmed explicitly it is not wanted in the running
app.

**The reference photo's central avatar is Marvel's Iron Man helmet —
instead `_Orb` draws an original design.** A glowing avatar built from
concentric rings, a radial-gradient glow and a row of animated waveform
bars, in the same blue sci-fi register as the photo but nobody's
intellectual property. `_ORB_COLOR` keys its glow colour to `State` (dim
blue idle, bright blue listening, amber thinking, teal speaking) so the
avatar itself shows what Jarvis is doing, not just the status label next to
it. A `QTimer` (`ORB_TICK_MS`, 60ms) drives `_Orb.tick()`, since a widget
with no events of its own otherwise never repaints.

**The waveform is explicitly decorative, not a real audio level.** Wiring
it to the actual microphone/speaker signal would mean reaching into
`listener.py`/`speaker.py`'s internals for a number this redesign doesn't
otherwise need, so the bar heights are a deterministic `math.sin()` wobble
off an internal phase counter instead — flagged in both `ORB_TICK_MS`'s own
comment and `_Orb`'s docstring as a known simplification, in keeping with
this project's own rule against presenting invented data as real (see
"Grounding").

**Layout: one header, one sidebar, five pages in a `QStackedWidget`.** The
old single-screen shell (status label + transcript + a splitter's side
panel) is now `Home`/`Chat`/`Tasks`/`Files`/`Settings`, switched by sidebar
buttons (`NAV_HOME`/`NAV_CHAT`/`NAV_TASKS`/`NAV_FILES`/`NAV_SETTINGS`) via
`_go_to_page()`, which also keeps exactly one of them `checked()` — Qt
doesn't auto-exclude checkable buttons outside a `QButtonGroup`, so this is
done by hand. Where everything moved:

- **Home** — the orb, the record button, and (as a right-hand column) the
  System Status panel (CPU/RAM/VRAM, same `_poll_system()` as before, now
  joined by a **Network** reading — see below), the Quick Actions panel
  (unchanged wiring, same `skills._open_site()`/`_open_app()` calls), and a
  new **Current Task** panel showing the first item of today's agenda (or a
  flat "waiting for a command" line) — the same `memory.agenda()` call
  `_load_tasks()` already made for the Tasks page, read once and rendered
  into both places.
- **Chat** — the transcript list, unchanged in content and behaviour; it
  just no longer shares a screen with the record button, which lives on
  Home now.
- **Tasks** — the full agenda list, unchanged from step 5 piece 3.
- **Files** — a stub placeholder page naming Phase 7 as what will fill it;
  nothing of Phase 7 was pulled forward.
- **Settings** — replaces the old `QDialog`+`DEBUG_ACTION` menu `QAction`
  with a page holding the same read-only `config.py` rows plus the
  debug-mode toggle, now a `QCheckBox` (`findChild(QCheckBox, DEBUG_ACTION)`
  in `_debug_mode_enabled()`, was `QAction`) — a page fits the sidebar's own
  navigation model, where a modal dialog over a single-screen shell did not.
  Still read-only, for the same reason as before: the project's own working
  rule is "never edit `.env`", so a field here that wrote back to it would
  be exactly that, by another name.

**Network throughput is new, alongside CPU/RAM/VRAM.** `psutil` only gives
a running total of bytes moved since boot, not a rate, so
`_poll_system()` computes one as a delta between polls:
`(total_bytes_now - total_bytes_last) / (time_now - time_last)`, using
`time.monotonic()` so a system clock adjustment can't produce a negative or
nonsensical elapsed time. The first poll in a process has no "last" reading
to diff against and shows nothing yet — the same one-tick blind spot
`psutil.cpu_percent()`'s own priming call already has, not a bug needing a
separate fix. `_format_rate()` renders it as `"1.2 MB/s"`-style text, kept
to two units (KB/s, MB/s) since this is a glance-at number.

**The header carries a live clock and date**, via `_update_clock()` on its
own one-second `QTimer` (`_clock_timer`) plus one immediate call at
construction so the header isn't blank for the first second. `CLOCK_LABEL`/
`DATE_LABEL` are named constants, consistent with every other widget this
file can find again — they started as inline string literals in the first
draft of this redesign and were promoted before committing, the same
reason every other object name here is centralized rather than scattered.

**The styling is a single dark QSS stylesheet** (`_STYLESHEET`, applied via
`self.setStyleSheet(...)` in `__init__`) rather than per-widget styling
calls scattered through the `_build_*()` methods — a deep navy palette, one
blue accent, rounded panel/button/list styling matching the photo's
register without any of its imagery. `QFrame#header`/`#sidebar`/`#panel`
and `QPushButton[navButton="true"]:checked`/`:hover` are the selectors that
carry most of the look; everything else (buttons, lists, the status bar)
gets a general rule so new widgets added later pick up the theme without
needing their own stylesheet entry.

**Nothing about the actual behaviour changed.** The worker threads
(`_ListenWorker`/`_BrainWorker`/`_SpeakWorker`), the `State` machine and
`_set_state()`, `_get_reply()`, `_load_tasks()`'s agenda query, the quick
actions' calls into `skills.py`, and the debug line's audit-row read are
all exactly what step 4 and step 5 built — this redesign only changed where
things are drawn and what they look like while drawing it. `_STATUS_TEXT`'s
Greek strings, `_BRAIN_ERROR_REPLY`, and every status transition's timing
are unchanged, which is why none of the existing worker/state/tasks tests
needed anything beyond the object-lookup fixes below.

**Tests.** `tests/test_gui_shell.py`'s `DebugModeWiringTests`/
`ShellConstructionTests` were updated for `DEBUG_ACTION` now being a
`QCheckBox` found via `widget()` rather than a `QAction` found via
`findChild(QAction, ...)`. `SettingsDialogTests` (which mocked
`QDialog.exec()`) is replaced by `SettingsPageTests`, pinning that
`NAV_SETTINGS` is reachable from the sidebar and switches the stack to page
4, and that the page's `QLabel`s show the real `config.BRAIN_PROVIDER`
value rather than anything hardcoded. New coverage for what's actually new:
`SidebarNavigationTests` (Home is shown at construction; each of the other
four nav buttons switches to its own page and is the only one left
checked), `OrbTests` (`_set_state()` updates the orb's own state; `tick()`
advances its phase), `FormatRateTests` (`_format_rate()`'s KB/s and MB/s
rendering), `NetworkMonitoringTests` (the first poll shows nothing, the
second computes a real rate from a faked `psutil.net_io_counters()` and
`time.monotonic()`), and `ClockTests` (`_update_clock()` against a patched
`datetime.now()`, and that closing the window stops `_clock_timer`). All 53
tests in `test_gui_shell.py` (42 before this redesign, 11 new) skip cleanly
in this sandbox (no PySide6, the same constraint noted below); the full
suite is 318 tests, 26 errors (the same pre-existing Windows-only-module
baseline), 53 skipped.

**Not yet hand-tested live.** This whole redesign was built and verified
only against `py_compile` and the mocked suite in a sandbox that cannot
install PySide6 (same constraint as "Built in a sandbox that cannot run
it," above) — pending a `git pull` and a real run on the user's Windows
machine: the dark theme, the orb's animation and colour changes across a
real conversation turn, sidebar navigation across all five pages, the
Network reading alongside CPU/RAM/VRAM, the Current Task panel, and the
Settings page's checkbox and values all need a visual hand test before this
is closed out the way every other piece in this file has been.

### A second pass: the panels and page headers weren't actually styled

The first hand test on the user's machine came back with direct feedback:
the dashboard still didn't look premium — specifically "the boxes." Reading
the redesign's own first-pass stylesheet explains why: `_build_monitoring_box()`/
`_build_quick_actions_box()`/`_build_current_task_box()` each put a plain,
unstyled `QLabel("System Status")`-style heading inside a `QFrame#panel`, and
`_STYLESHEET`'s `QFrame#panel` rule was a single flat background colour, one
thin border and a fixed radius — competent, but exactly the "γραφικό" (plain)
look the whole redesign was meant to replace, just in a darker palette. A
panel with no visual weight on its own title and no accent of any kind reads
as a grey box with text in it, premium or not.

**Two helper functions, `_panel_title()` and `_page_title()`, replace the
bare `QLabel(...)` calls** every panel and every page header used —
`object_name="panelTitle"`/`"pageTitle"` respectively, so the stylesheet can
target them without every call site repeating font/colour rules by hand.
`_panel_title()` is small-caps-style (uppercase text supplied by the caller),
letter-spaced, dim, and sits above a 1px divider — the same "section label"
treatment a real dashboard widget library gives a card's own heading, instead
of a heading that is visually identical to the data below it. `_page_title()`
is the opposite register: large, white, bold, underlined in the accent blue —
making Συνομιλία/Εργασίες/Αρχεία/Ρυθμίσεις read as actual page headers
instead of the first line of body text.

**`QFrame#panel` itself gained three things a flat box didn't have**: a
top-to-bottom gradient background (`#141c30` → `#101726`) rather than one
flat colour, a 3px accent-blue left border standing in for a card's "this is
a distinct, important block" cue, and consistent internal margins
(`setContentsMargins(16, 14, 16, 14)`, `setSpacing(8)` — previously these
three `_build_*_box()` methods built a `QVBoxLayout()` with Qt's default
margins and no explicit spacing at all, which is part of why they looked
cramped rather than composed).

**The CPU/RAM/VRAM/Network labels got a `metric` dynamic property**
(`label.setProperty("metric", True)`) so `QLabel[metric="true"]` can give
them real weight (bold, a brighter text colour) distinct from a plain label
— those four numbers are the one thing a glance at the System Status panel
is actually for, and they looked exactly as important as everything else
around them before this.

**Quick-action buttons got a `quickAction` property** too
(`button.setProperty("quickAction", True)`), left-aligned and tighter-padded
via `QPushButton[quickAction="true"]` — a row of buttons stacked in a narrow
side panel reads better left-aligned than centred, which the generic
`QPushButton` rule (centre-aligned by Qt's own default) didn't give them.

**The record button is no longer just "a QPushButton that happens to say
Εγγραφή."** `QPushButton#record_button` is its own rule: a blue gradient
fill, a fully rounded pill shape (`border-radius: 22px` against its own
padding), bigger text, letter-spacing — the one button on the whole app that
actually starts a turn is now the one button that looks like the primary
action, instead of matching every secondary button's flat grey.

**The Settings page's rows gained their own identity too.** Previously each
row was `form.addRow("Brain provider:", QLabel("claude"))` — a plain string
label and a plain value label, both defaulting to the same colour and
weight as literally every other piece of text in the window. `settingKey`/
`settingValue` object names (and the whole form now sits inside its own
`QFrame#panel`, not loose in the page layout) separate "what this setting is
called" (dim, in the panel-title colour family) from "what it's currently
set to" (bright, bold) — the same key/value visual hierarchy an actual
settings screen has.

**Lists, the status pill, the nav buttons and the scrollbar all got a second
pass too**, each for the same reason: `QListWidget::item` now has its own
padding and rounded selection highlight instead of Qt's flat default row
style; `QLabel#status_label` (the header's "Κατάσταση: ..." text) is now a
pill — a translucent blue background and border around the text, not bare
text sitting in the header; the checked nav button is a blue gradient
instead of a flat single-colour fill, matching the record button's register;
and `QScrollBar` got a thin, rounded, dark-on-dark style, since Qt's
platform-default scrollbar (a grey Windows-95-style slab) is the fastest way
to make an otherwise-dark window look unfinished the moment a list grows
past its visible height. None of this touched layout structure, object
names anything else already depended on, or behaviour — `_set_state()`,
the worker threads, `_poll_system()`, `_load_tasks()`, the quick-action
click handlers and the debug line are all byte-for-byte what they were
before this pass; only `_STYLESHEET` and the handful of `_build_*()` methods
that build the panels/pages changed, which is why no test needed updating —
`tests/test_gui_shell.py`'s 53 tests still assert on object names and
behaviour, none of which moved, and all still skip cleanly in this sandbox
(no PySide6).

**Still not hand-tested live.** Built and verified the same way as the
first pass — `py_compile` plus the mocked suite — in the same sandbox that
cannot install PySide6. Needs the same kind of visual hand test as before,
this time specifically on the panels: do the System Status/Quick Actions/
Current Task boxes actually read as distinct cards now, does the left
accent border show up against the dark background, does the record button
read as the obvious primary action, and does the Settings page's key/value
layout look like a settings screen rather than a column of identical labels.

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
ending budget**, matched by `text.fuzzy_word()` — which the kill switch
shares, with a looser budget and a different confirming guard (see "The kill
switch"). Three separate guards keep the looseness honest here, and only one
of them is a number:

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
lives next to it: `θυμήσου`, `να θυμάσαι`, `θέλω να θυμάσαι` and `μην
ξεχάσεις` get 2; `σημείωσε` gets 1, because `σημειώσεις` is an ordinary noun
sitting 2 away;
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

## Grounding

The brain is a small local model with no tools and no network, and its
failure mode is not silence — it is a fluent invention. Asked what the shop
sold in March it will name a figure; asked what is on today it will invent a
deadline. Both read exactly like an answer, and out loud there is nothing to
reread.

So `SYSTEM_PROMPT` has a second paragraph, and it draws the line by
**subject, not by confidence**. Anything about the speaker's own life — their
schedule, school, courses, coursework, obligations, business — may only be
said if it is in the recalled memory block or was said earlier in this same
conversation. Names, dates, times, deadlines, numbers and turnover are never
to be produced from the model's own head, *not even as an example or a
hypothetical* — that hedge is spelled out because it is the loophole a model
takes when told not to guess: it guesses and labels the guess «ας πούμε».

General world knowledge is explicitly still allowed. The instruction is
about the user's facts, not about the model's; forbidding invention wholesale
would also have cost the capital-of-Australia answer the few-shot examples
ask for.

**"No internet" is stated separately** because it is a different kind of
ignorance. Weather, news, prices, email and messages are not things the model
lacks memory of — they are things nothing in this process can ever reach, so
the honest answer is fixed rather than dependent on what was saved.

The prompt also supplies the words for saying no — «δεν το ξέρω αυτό», «δεν
μου το έχεις πει», «δεν έχω πρόσβαση σε αυτό» — and says to stop there. A
model told only *not* to answer tends to fill the turn with an apology and
then answer anyway.

Two few-shot examples carry the same thing by demonstration, which this model
follows more reliably than prose: a weather question and a question about the
user's own shop, each answered with a flat refusal and nothing after it. They
sit in `FEW_SHOT_EXAMPLES`, inside the frozen prefix, so the history trim can
never drop them.

None of this is enforceable in code — it is a prompt, and a prompt is
persuasion. It was hand-tested on live audio against the five questions that
had previously drawn invented answers, and the one that mattered was the
business-figures question, since that is the failure a user would act on.

## Language

The user speaks Greek. Jarvis should answer in Greek by default.

## Working rules

- Explain changes briefly before/after making them.
- Ask before deleting any file.
- Never edit `.env`.
- Remind me to commit once something works.
