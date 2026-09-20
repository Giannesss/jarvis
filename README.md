# Jarvis

Ένας απλός φωνητικός βοηθός σε Python, που τρέχει εξ ολοκλήρου τοπικά μέσω
[Ollama](https://ollama.com) — χωρίς API key, χωρίς κόστος ανά token, χωρίς
σύνδεση στο internet για τις απαντήσεις.

## Setup

### 1. Python venv

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Αν το `pyaudio` αποτύχει να εγκατασταθεί στα Windows, δοκίμασε:

```
pip install pipwin
pipwin install pyaudio
```

### 2. Ollama (τοπικό μοντέλο)

1. Κατέβασε τον επίσημο installer: https://ollama.com/download/windows
2. Τρέξε το `OllamaSetup.exe` — εγκαθιστά το Ollama και το ξεκινάει ως background service (δεν χρειάζεται admin δικαιώματα)
3. Άνοιξε **νέο** terminal (το παλιό δεν βλέπει το ενημερωμένο PATH)
4. Επιβεβαίωσε την έκδοση: `ollama --version` — θέλουμε **0.23.0 ή νεότερη** (υπάρχει γνωστό CVE σε versions 0.12.10–0.17.5, οπότε αν είναι παλιότερο, ξανατρέξε τον installer)
5. Κατέβασε το μοντέλο (λίγα GB, μία φορά):
   ```
   ollama pull qwen3:14b
   ```
6. Δοκίμασέ το απευθείας και επιβεβαίωσε ότι τρέχει στη GPU (όχι CPU):
   ```
   ollama run qwen3:14b
   ollama ps
   ```

Το `qwen3:14b` επιλέχθηκε γιατί χωράει άνετα στα 12GB VRAM της RTX 5070 Ti (laptop) και υποστηρίζει επίσημα ελληνικά. Αν θες πιο γρήγορες απαντήσεις (π.χ. ενώ παίζεις κάτι άλλο στη GPU), βάλε στο `.env`:
```
OLLAMA_MODEL=qwen3:8b
```

### 3. Ελληνική φωνή (Piper)

Το text-to-speech χρησιμοποιεί το [Piper](https://github.com/OHF-Voice/piper1-gpl) — τοπικό, offline
neural TTS με ελληνική φωνή (οι Windows SAPI5 φωνές δεν καλύπτουν καλά τα ελληνικά, οπότε δεν
χρησιμοποιούμε system voice). Κατέβασε το μοντέλο φωνής μία φορά:

```
python -m piper.download_voices el_GR-joy-medium --download-dir models
```

### 4. Config

```
copy .env.example .env
```

Το `.env` είναι προαιρετικό — ορίζει ποιο τοπικό μοντέλο LLM θα χρησιμοποιηθεί (default: `qwen3:14b`)
και το path του Piper voice model (default: `models/el_GR-joy-medium.onnx`).

## Εκτέλεση

```
python main.py
```

Πάτα Enter, μίλησε, και ο Jarvis θα σου απαντήσει γραπτά και φωνητικά (τοπικά, μέσω Ollama). Γράψε `exit` για έξοδο.
