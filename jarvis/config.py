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
