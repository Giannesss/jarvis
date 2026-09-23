import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
PIPER_MODEL_PATH = os.environ.get("PIPER_MODEL_PATH", "models/el_GR-joy-medium.onnx")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")

# "ollama" (local, only one implemented). See CLAUDE.md "Providers".
BRAIN_PROVIDER = os.environ.get("BRAIN_PROVIDER", "ollama")

# "edge" (online, Microsoft Edge TTS, male voice by default) or "piper"
# (offline, falls back to this automatically if edge synthesis fails).
TTS_ENGINE = os.environ.get("TTS_ENGINE", "edge")
TTS_VOICE = os.environ.get("TTS_VOICE", "el-GR-NestorasNeural")

# Recording silence detection (see jarvis/listener.py). The floor separating
# speech from room noise, in dBFS: _StopDecider compares each 80ms frame's RMS
# against it, and ffmpeg's silencedetect takes the same number.
#
# -35 was too high for this microphone. Measured over data/wake_probe.csv (890
# frames, one session): speech runs at a median of -38.0 dB and peaks at -20.6,
# against a noise floor whose median is -72.0. So the floor sat *above* median
# speech and was cleared only on peaks — onset detection was a coin flip on a
# quiet utterance, and ffmpeg called ordinary speech silence.
#
# -45 sits 27 dB clear of the noise floor and 7 dB under median speech, which
# is the right side of both. Note this is not the wake word's problem: that
# score is level-invariant (a 15 dB replay sweep moved it by <0.001), so this
# threshold governs the *recorder* only.
SILENCE_THRESHOLD_DB = -45
SILENCE_DURATION = 1.0  # seconds of silence before ffmpeg reports it
MAX_RECORD_SECONDS = 15  # hard safety cap on recording length
NO_SPEECH_TIMEOUT = 8  # stop early if nothing is said within this long

# Wake-word listening (see jarvis/listener.py, jarvis/wakeword.py). Off by
# default; opt in per session, e.g. `$env:WAKE_WORD_ENABLED="true"`.
WAKE_WORD_ENABLED = os.environ.get("WAKE_WORD_ENABLED", "false").lower() == "true"

# "openwakeword" (only one implemented). See CLAUDE.md "Providers" pattern.
WAKE_WORD_ENGINE = os.environ.get("WAKE_WORD_ENGINE", "openwakeword")

# Either the name of a pretrained openWakeWord model (downloaded on first use)
# or a path to a custom .onnx model, e.g. "models/tzarvis.onnx" for the Greek
# "Τζάρβις". Default is the pretrained English "Hey Jarvis".
WAKE_MODEL_PATH = os.environ.get("WAKE_MODEL_PATH", "hey_jarvis")

# Detection score cutoff, 0-1. Higher = stricter (fewer false triggers, must
# say the wake phrase more clearly); lower = easier to trigger but chattier.
WAKE_THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", "0.5"))

# Verbose wake-word/recording diagnostics: per-frame dB while waiting for
# speech, detection scores, and why each recording stopped.
WAKE_DEBUG = os.environ.get("WAKE_DEBUG", "false").lower() == "true"

# Under WAKE_DEBUG, the lowest score worth printing. This was 0.1, which made
# a suppressed detection indistinguishable from silence in the log: a
# wakeword.reset() reseeds the model's 16-frame window with embeddings of
# random noise, and a wake word spoken into that window scores ~0.000 rather
# than merely less — measured on data/wake_probe.wav, utterances that score
# 0.999 clean score 0.000 when reset lands under ~0.5s before them. Those
# frames printed nothing at all, so the log could not tell "it scored zero"
# from "you never spoke". Set to 0 to print every scored frame.
WAKE_SCORE_FLOOR = float(os.environ.get("WAKE_SCORE_FLOOR", "0.001"))

# Play a short beep after the wake word fires, as a "go ahead" cue. When on,
# the recording starts from scratch after the beep (the queues are flushed),
# so the wake word itself is never part of what Whisper transcribes. Turn it
# off to fall back to the pre-roll path: no beep, and the ~1s captured just
# before the trigger is prepended so words said in the same breath as the
# wake word survive — at the cost of the wake word being in the audio too.
WAKE_BEEP = os.environ.get("WAKE_BEEP", "true").lower() == "true"

# After a detection the model's internal buffer still holds the wake word, so
# it keeps scoring above threshold for several frames. Detections are ignored
# for this long (while frames keep being fed, refilling the buffer with fresh
# audio) after a reset. See jarvis/listener.py listen_for_wake_word().
WAKE_RETRIGGER_COOLDOWN = 1.0

# Keep listening for follow-up commands after a reply instead of requiring the
# wake word again. Ends on an end phrase (jarvis/skills.py), on
# CONVERSATION_TIMEOUT seconds with no speech, or on shutdown.
CONVERSATION_MODE = os.environ.get("CONVERSATION_MODE", "true").lower() == "true"
CONVERSATION_TIMEOUT = float(os.environ.get("CONVERSATION_TIMEOUT", "6"))

# --- Persistent memory (jarvis/db.py, jarvis/memory.py). Everything lives
# under data/, which is gitignored: it holds what you've told Jarvis about
# yourself, so it never belongs in the repo.
DATA_DIR = Path(os.environ.get("JARVIS_DATA_DIR", "data"))
DB_PATH = Path(os.environ.get("JARVIS_DB_PATH", DATA_DIR / "jarvis.db"))

# Startup and ":mem backup" copy the database here via SQLite's backup API,
# keeping only the newest BACKUP_KEEP files.
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", DATA_DIR / "backups"))
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "5"))

# Diagnostics (jarvis/diag.py). Every [timing]/[rec]/[wake] line printed to
# the terminal is mirrored here with a timestamp, because the answer to
# "why was that turn wrong" has repeatedly been a scrollback nobody still
# had. Rotated at LOG_MAX_BYTES, keeping LOG_KEEP old files; under data/,
# so it is gitignored like the rest -- transcribed text never reaches it,
# but mic levels and timings still describe someone's room.
LOG_PATH = Path(os.environ.get("JARVIS_LOG_PATH", DATA_DIR / "jarvis.log"))
LOG_ENABLED = os.environ.get("LOG_ENABLED", "true").lower() == "true"
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_KEEP = int(os.environ.get("LOG_KEEP", "3"))

# The scheduler (jarvis/scheduler.py): reminders and timers that survive a
# restart. SCHEDULER_TICK is how often the pending rows are checked, so it is
# also the worst-case lateness of a timer -- the thread holds one connection
# for its lifetime, which is what makes a tick this short cheap. Only the
# newest SCHEDULER_CATCHUP_LIMIT missed reminders are read out at startup;
# the rest are counted aloud and stay in the table as `missed`.
SCHEDULER_ENABLED = os.environ.get("SCHEDULER_ENABLED", "true").lower() == "true"
SCHEDULER_TICK = float(os.environ.get("SCHEDULER_TICK", "2"))
SCHEDULER_CATCHUP_LIMIT = int(os.environ.get("SCHEDULER_CATCHUP_LIMIT", "3"))

# Hard cap on how much remembered context is injected into a brain call.
# Estimated at len(text)/3, which is conservative for Greek.
MEMORY_TOKEN_BUDGET = int(os.environ.get("MEMORY_TOKEN_BUDGET", "300"))

# The life areas a saved row can belong to, so "τι έχω σήμερα" can pull across
# every table at once instead of needing you to know which one holds what.
#
# A closed set on purpose: these are *your* areas, nobody else can guess them,
# and a fixed list means a typo cannot invent a tag that nothing will ever
# search for. Values are the spoken words implying the tag, matched as
# substrings of a row's already-normalized `norm` -- so write stems where the
# ending varies ("πελατ" covers πελάτης and πελάτες), and spell them naturally:
# they go through text.normalize() at import like every other phrase list.
#
# Tagging is additive and never gates a save: a row nothing matches simply has
# no tag and is still found by keyword search exactly as before.
MEMORY_TAGS = {
    "cafe": ("καφέ", "καφετέρια", "μπαρίστα", "εσπρέσο", "καφεκοπτ"),
    "restaurant": ("εστιατόριο", "ταβέρνα", "μενού", "σερβιτόρ", "κουζίνα"),
    "university": ("μάθημα", "εξάμηνο", "σχολή", "πανεπιστήμιο", "εξέταση", "πτυχίο"),
    "ai_marketing": ("μάρκετινγκ", "καμπάνια", "πελατ", "διαφήμισ", "μπράντα"),
    "business": ("επιχείρηση", "μαγαζί", "εταιρεία", "κατάστημα"),
}

# Skill: websites openable via "άνοιξε το ..." (jarvis/skills.py). Keys are
# matched as accent/case-insensitive substrings of the spoken text, so they
# can be spelled naturally here.
SKILL_SITES = {
    "YouTube": "https://www.youtube.com",
    "Gmail": "https://mail.google.com",
    "Google": "https://www.google.com",
}

# Skill: local apps openable via "άνοιξε το/τον ..." (jarvis/skills.py). Keys
# match the same way as SKILL_SITES. Values are argv lists passed directly to
# subprocess.Popen (never a shell), so only these exact programs can launch —
# add new ones here, never let skills.py build a command from spoken text.
SKILL_APPS = {
    "υπολογιστή": ["calc.exe"],
    "Notepad": ["notepad.exe"],
}
