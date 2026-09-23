# CLAUDE.md audit

Read-only review of `CLAUDE.md` against the current codebase (branch
`fix/wake-word`, 2026-09-23). No edits made to `CLAUDE.md` — this just lists
what's stale, missing, or internally inconsistent, ranked roughly by impact.
No TODO/FIXME/XXX comments were found anywhere in `jarvis/`, `tools/`, or
`main.py`, so there's nothing in that category to report.

## 1. "Pipeline (as implemented)" is out of date — the biggest issue

This section reads like it was written before wake-word mode, conversation
mode, and Edge TTS existed, and never revised even though most of the rest of
the doc clearly has been (the "Wake word" / "The recording clock" /
"Conversation mode" sections are detailed and current). Four separate claims
in it don't match the code:

- **"capture `RECORD_SECONDS` (6s)... into a temp WAV."** There is no
  `RECORD_SECONDS` constant anywhere in the codebase anymore. `listen()`
  (`jarvis/listener.py:141`) stops on ffmpeg's `silencedetect` output —
  dynamic, not a fixed duration — with `MAX_RECORD_SECONDS` (15s, from
  `config.py`) as the hard cap and `NO_SPEECH_TIMEOUT` (8s) for leading
  silence. This whole section also only describes `listen()`, the
  Enter-press path; it says nothing about `start_stream()` /
  `listen_for_wake_word()` / `record_command()`, which is most of
  `listener.py`'s 1119 lines and the subject of two of the doc's other major
  sections.
- **"`faster_whisper` (`base` model...)."** The model is `WHISPER_MODEL`
  from `.env` (`jarvis/config.py:10`), defaulting to `"small"`, not a
  hardcoded `"base"`.
- **"a system prompt telling it to be concise and reply in whatever language
  the user used."** `brain.SYSTEM_PROMPT` (`jarvis/brain.py:7-15`) says
  nothing about language at all — it's about tone (short, natural, no
  markdown, don't call itself an AI unless asked). Language handling isn't
  in the system prompt; the only place it's stated is the "Language" section
  at the bottom of the doc ("The user speaks Greek. Jarvis should answer in
  Greek by default"), which is a different and narrower claim than "whatever
  language the user used."
- **"synthesizes the reply with Piper and plays it via `winsound`."** This
  describes only the fallback path. `TTS_ENGINE` defaults to `"edge"`
  (`jarvis/config.py:17`), and `speaker.speak()` tries Edge TTS first,
  falling back to Piper only if it fails — which is exactly what the doc's
  own opening paragraph says ("Voice output can optionally use Microsoft's
  online Edge TTS... falls back to offline Piper"). The Pipeline section
  contradicts the doc's own header.

Given how much has changed underneath it, this section probably wants a
rewrite rather than a patch — it's the one place a new reader would go for
"what actually happens end to end," and right now it describes a version of
the app from before wake word existed.

## 2. "Config" section is stale and misleadingly narrow

> All settings live in `.env` (copy from `.env.example`): `OLLAMA_MODEL`,
> `PIPER_MODEL_PATH`. The mic device name, record duration, and FFmpeg path
> are hardcoded constants at the top of `jarvis/listener.py`.

- `.env.example` currently has 20 settings (`OLLAMA_MODEL`,
  `PIPER_MODEL_PATH`, `TTS_ENGINE`, `TTS_VOICE`, all the `WAKE_*` vars,
  `CONVERSATION_MODE`/`CONVERSATION_TIMEOUT`, `JARVIS_DATA_DIR`,
  `JARVIS_DB_PATH`, `BACKUP_DIR`, `BACKUP_KEEP`, `MEMORY_TOKEN_BUDGET`), plus
  `WHISPER_MODEL` which has an env-backed default in `config.py` but isn't
  even in `.env.example`. Most of these are individually documented in
  their own sections (Providers, Wake word, Memory), so this isn't
  information that's missing from the doc as a whole — but this section
  reads as *the* config reference and is off by about 18 settings.
- **"record duration... hardcoded"** is no longer true — see #1.
  `MICROPHONE_NAME` and `FFMPEG_PATH` are still hardcoded constants in
  `listener.py` (lines 31-37), so that half of the sentence is still
  correct.

## 3. "Files" section omits the entire `tools/` directory

The Files list covers `main.py` and every `jarvis/*.py` module, plus the
three root-level throwaway mic-capture experiments — but not
`tools/wake_score_probe.py`, `tools/gen_wakeword_clips.py`, or
`tools/record_wakeword_clips.py`. All three are committed, documented
scripts (not throwaway experiments like the root-level ones):
`wake_score_probe.py` is the diagnostic tool the doc's own current-phase
roadmap note tells the reader to run next, and the other two are the
custom-wake-word data-prep pipeline referenced under "Wake word"
(`WAKE_MODEL_PATH` / `models/tzarvis.onnx`). Worth a line each, at minimum
for `wake_score_probe.py` since the roadmap note already points at it by
path.

## Sections checked and found accurate

Spot-checked against the source for specific claims (function names,
constants, ordering, regex behavior) rather than just skimmed — no
discrepancies found:

- **Providers** — `_PROVIDERS`/`_VOICE_PROVIDERS`/`_ENGINES` dispatch dicts,
  `ValueError` at import time, Piper/`"elevenlabs"` fallback framing: all
  match `brain.py`, `speaker.py`, `wakeword.py`.
- **Wake word** — `flush()`-not-`drain()` on entry, cooldown not restarting
  on a hole, `WAKE_SCORE_FLOOR`/`WAKE_DEBUG` behavior, `WAKE_MODEL_PATH`'s
  two forms and `_score_key` derivation, `.onnx`-only custom models: all
  match `wakeword.py` and the current `listener.py`. This section was
  clearly the most recently rewritten (commits `32f40a4`, `2cab8c8`) and it
  shows.
- **The recording clock** — `_stream_origin`/`_note_origin`,
  `_should_capture`, `_StopDecider`, `_gap_blame`, `_enqueue_bounded`
  eviction counting, `flush()` as a watermark: all match.
- **Conversation mode** — `CONVERSATION_MODE`/`CONVERSATION_TIMEOUT`
  defaults, `MAX_GAP_RETRIES` (2), end-phrase handling, wake-word-only scope:
  matches `main.py`'s `_converse()`.
- **Skills** — `handle()`'s exact dispatch order (shutdown → memory save →
  memory recall → timer → time → date → open), fixed-argv `subprocess.Popen`
  / `webbrowser.open` only from `config.py`: matches `skills.py` line for
  line.
- **Memory** — parse ladder order, `SCHEMA_VERSION = 2`, `MIN_STEM = 5`,
  `_FUZZY_TRIGGERS` budgets (θυμ/ήσου=2, να θυμ/άσαι=2, σημειώσ/ε=1, μην
  ξεχ/άσεις=2, "κράτα" exact-only), `businesses.name` stored as `None` not
  `""`, `MEMORY_RECALL_PHRASES`/`MEMORY_RECALL_LIMIT` (3), WAL mode + 5s busy
  timeout, `:mem` command set (list/show/edit/del/export/backup): all match
  `memory.py`, `db.py`, `mem.py`.
- **Normalization** — `fold_iotacism` digraph handling, `normalize_spans`,
  `edit_distance`, punctuation left un-normalized: matches `text.py`.
- **Language / Working rules** — policy statements, nothing to verify
  against code.

## Not in scope, flagged only in passing

`README.md` (not `CLAUDE.md`, so left alone) says the default Ollama model
is `qwen3:14b`; `jarvis/config.py`'s actual default is `qwen3:8b`. Not part
of this audit's mandate, but adjacent enough to mention in case it's worth a
follow-up.
