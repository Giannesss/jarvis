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

No new dependencies or API-key handling have been added for the
placeholders — that's future work when one is actually implemented.

## Skills

`jarvis/skills.py` handles a fixed set of Greek voice commands locally,
before the transcribed text ever reaches the brain: `main.py` calls
`skills.handle(text)` first and only calls `brain.ask(text)` when that
returns `None`. Matching is accent-insensitive, case-insensitive, and
tolerant of extra words (simple substring matching on normalized text),
since the input comes from speech recognition.

Implemented: current time / date; opening a website (`SKILL_SITES` in
`config.py`) or a local app (`SKILL_APPS` in `config.py`) by name; a spoken
timer that announces itself (print + `speaker.speak`) when it fires; and
shutting Jarvis down. Only the sites/apps listed in `config.py` can ever be
opened — `skills.py` never builds a command from spoken text, and app
launches always use a fixed argv list (never a shell).

Shutdown is signalled via a `skills.shutdown_requested` flag (set by the
skill, checked by `main.py` after speaking the reply) rather than by
`handle()`'s return value, since `handle()` is otherwise always `str | None`.

Timers run on a daemon `threading.Timer` so a pending one can't hang process
exit. `speaker.speak()` is guarded by a lock so a timer announcement firing
while Jarvis is already talking waits its turn instead of cutting off or
overlapping the current audio.

## Language

The user speaks Greek. Jarvis should answer in Greek by default.

## Working rules

- Explain changes briefly before/after making them.
- Ask before deleting any file.
- Never edit `.env`.
- Remind me to commit once something works.
