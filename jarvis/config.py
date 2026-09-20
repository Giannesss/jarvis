import os

from dotenv import load_dotenv

load_dotenv()

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:14b")
PIPER_MODEL_PATH = os.environ.get("PIPER_MODEL_PATH", "models/el_GR-joy-medium.onnx")
