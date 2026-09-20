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

`main.py` just wires these three together in a loop.

## Files

- `main.py` — CLI loop tying listener → brain → speaker together.
- `jarvis/listener.py` — FFmpeg recording + Whisper transcription.
- `jarvis/brain.py` — Ollama chat call + conversation history + system prompt.
- `jarvis/speaker.py` — Piper TTS + playback.
- `jarvis/config.py` — loads `.env` (via `python-dotenv`) into `OLLAMA_MODEL` and `PIPER_MODEL_PATH`.
- `native_mic_test.py`, `wavein_capture_test.py`, `windows_capture_test.py` — throwaway
  experiments trying different Windows mic-capture APIs; not part of the app.

## Config

All settings live in `.env` (copy from `.env.example`): `OLLAMA_MODEL`, `PIPER_MODEL_PATH`.
The mic device name, record duration, and FFmpeg path are hardcoded constants at the
top of `jarvis/listener.py`.

## Language

The user speaks Greek. Jarvis should answer in Greek by default.

## Working rules

- Explain changes briefly before/after making them.
- Ask before deleting any file.
- Never edit `.env`.
- Remind me to commit once something works.
