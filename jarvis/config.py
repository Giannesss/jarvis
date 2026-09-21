import os

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

# Recording silence detection (see jarvis/listener.py).
SILENCE_THRESHOLD_DB = -35  # ffmpeg silencedetect noise floor, in dB
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
