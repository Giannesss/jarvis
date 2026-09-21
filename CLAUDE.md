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

Phrase lists are spelled with a plain `σ`, never a final `ς`: `_normalize()`
folds `ς` to `σ`, so `"Τέλος Τζάρβις"` arrives as `"τελοσ τζαρβισ"`.

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
