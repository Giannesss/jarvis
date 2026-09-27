import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
PIPER_MODEL_PATH = os.environ.get("PIPER_MODEL_PATH", "models/el_GR-joy-medium.onnx")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")

# "ollama" (local, the default) or "claude" (Anthropic's Messages API, which
# is online and needs a key). See CLAUDE.md "Providers". An unrecognized value
# raises at import time, i.e. at startup.
BRAIN_PROVIDER = os.environ.get("BRAIN_PROVIDER", "ollama")

# --- The Claude brain (BRAIN_PROVIDER=claude, see jarvis/brain.py).
#
# Everything in this block is inert while BRAIN_PROVIDER is "ollama": no key is
# needed, no SDK is imported, and nothing said to Jarvis leaves the machine.
#
# The key is read from the environment (.env, loaded above) and nowhere else.
# It is never printed, never logged, and never written to data/jarvis.log. A
# missing key while BRAIN_PROVIDER=claude is a *startup* failure rather than a
# per-question one -- see brain._check_key().
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Haiku 4.5 is the roadmap's own pick for everyday use -- cheap and fast, which
# is what a one-or-two-sentence spoken reply wants. It is also the model with
# the fewest surprises for this shape of request: omitting the `thinking`
# parameter means no thinking at all, whereas the 5-series models think
# adaptively unless explicitly told not to, and a 120-token spoken answer has
# no latency budget for it. Change this and re-read brain._ask_claude()'s note
# on what is deliberately *not* sent.
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5")

# Whether a *transient* Claude failure -- no internet, a rate limit, a 5xx --
# is answered from the local Ollama model for that one turn, the same way Edge
# TTS falls back to Piper. This is the roadmap's Phase 2 step 5, "keep qwen3:8b
# as an offline fallback".
#
# Transient only. A bad key or a malformed request raises instead: falling back
# there would mean every reply quietly comes from qwen3 while you believe you
# are talking to Claude. See brain._is_transient().
CLAUDE_FALLBACK_OLLAMA = (
    os.environ.get("CLAUDE_FALLBACK_OLLAMA", "true").lower() == "true"
)

# "edge" (online, Microsoft Edge TTS, male voice by default) or "piper"
# (offline, falls back to this automatically if edge synthesis fails).
TTS_ENGINE = os.environ.get("TTS_ENGINE", "edge")
TTS_VOICE = os.environ.get("TTS_VOICE", "el-GR-NestorasNeural")

# Playback (jarvis/player.py). Replies now play through one PortAudio stream
# instead of winsound, because winsound cannot play from memory
# asynchronously ("RuntimeError: Cannot play asynchronously from memory") and
# so cannot be stopped once started -- which is the whole of barge-in.
#
# PLAYBACK_DEVICE is empty for PortAudio's own default, an index, or a
# substring of a device name ("Realtek"). It is configurable because
# PortAudio's default is NOT necessarily the Windows default winsound used:
# measured here, PortAudio picks the monitor's HDMI output. If Jarvis goes
# quiet after this change, this is the first thing to check.
#
# PLAYBACK_BLOCKSIZE is how much audio the device is handed at a time, so it
# is also how long a barge-in takes to actually silence him: 512 frames is
# ~23ms at 22050 Hz. Smaller is more responsive and more likely to underrun.
PLAYBACK_DEVICE = os.environ.get("PLAYBACK_DEVICE", "")
PLAYBACK_BLOCKSIZE = int(os.environ.get("PLAYBACK_BLOCKSIZE", "512"))

# Streaming replies (jarvis/chunker.py): how a reply is cut into pieces worth
# speaking before the rest of it exists.
#
# STREAM_EARLY_FIRST_CHUNK lets the *first* piece be smaller than a sentence,
# broken at a comma. This is the knob that decides whether streaming is
# visible at all: measured warm on qwen3:8b, the first sentence completes at
# 5.4-6.1s and the whole reply at 6.2-8.0s -- and in one of the two trials the
# first sentence WAS the whole reply, 0.08s apart, because SYSTEM_PROMPT asks
# for one or two short sentences. Sentence-granular streaming therefore buys
# ~0.5s on a typical reply; breaking the first chunk at a clause gets the
# first words out at ~2-3s. The cost is prosody at one seam, once per reply.
# Turn it off to keep whole sentences everywhere.
STREAM_EARLY_FIRST_CHUNK = (
    os.environ.get("STREAM_EARLY_FIRST_CHUNK", "true").lower() == "true"
)
# 25, not 40, and the difference is the whole feature. A typical short Greek
# reply puts its first comma around character 36 ("Συνήθως είναι ζεστός και
# ηλιόλουστος,"), so a minimum of 40 skips it and finds nothing until the
# sentence ends -- the early chunk never fires at all. Measured live against
# qwen3:8b, time until the first words could start playing:
#
#     "Τι καιρό κάνει τον Οκτώβριο;"   off 6.33s   min=40 6.24s   min=25 3.26s
#     "Πες μου δυο πράγματα για την Αθήνα."
#                                      off 8.32s   min=40 5.47s   min=25 5.54s
#
# Below ~20 it starts cutting off openers ("Λοιπόν,") as chunks of their own,
# which wastes a synthesis round trip on one word and sounds clipped.
STREAM_FIRST_CHUNK_MIN_CHARS = int(os.environ.get("STREAM_FIRST_CHUNK_MIN_CHARS", "25"))
STREAM_FIRST_CHUNK_MAX_CHARS = int(os.environ.get("STREAM_FIRST_CHUNK_MAX_CHARS", "140"))

# Never synthesize a piece shorter than this on its own: a two-word chunk
# spends the same ~0.4s Edge round trip as a whole sentence and lands clipped.
# A short sentence is merged into the next one instead.
STREAM_MIN_CHUNK_CHARS = int(os.environ.get("STREAM_MIN_CHUNK_CHARS", "16"))

# Whether a reply is spoken as it is generated (brain.start_turn -> Chunker ->
# speaker.speak_stream) or synthesized whole and then played (brain.ask ->
# speaker.speak). Off is the pre-streaming path, kept working rather than kept
# around: it is the only way to hear a reply when the streaming pipeline is
# itself the suspect, and it is what the Enter-press prompt is tested against.
STREAM_REPLIES = os.environ.get("STREAM_REPLIES", "true").lower() == "true"

# --- Barge-in (jarvis/listener.py _BargeDecider, armed from main.py). Talking
# over Jarvis stops him mid-word instead of waiting him out.
#
# Every frame recorded while he is audible is refused by the capture gate
# (listener._should_capture) -- and those are exactly the frames this has to
# judge, so they are routed to the decider on their way to being dropped.
#
# BARGE_IN_MARGIN_DB is the whole feature: how far over the *running median* of
# recent audio a level must sustain itself to count as someone talking over
# him. Measured here with tools/barge_probe.py, on a clean run:
#
#     echo alone reaches  ~+4 dB   -> anything at or below this cuts a reply
#                                     off for nothing (his own vowels)
#     speech over him     ~+15 dB  -> anything above this misses you
#
# and the would-have-fired table agreed: 9 and 12 dB fired on the barge with
# no false fire on echo-only audio, 15 dB and up missed the barge entirely. 9
# sits 5 dB clear of the false-fire bound and 6 dB under the miss bound, which
# is as close to the middle of a 4-15 dB band as a round number gets. Raise it
# if a reply is ever cut off by nothing; lower it if talking over him is
# ignored. Re-measure with the probe rather than guessing -- it is a property
# of this room, these speakers, this mic gain and where they sit.
BARGE_IN_ENABLED = os.environ.get("BARGE_IN_ENABLED", "true").lower() == "true"
BARGE_IN_MARGIN_DB = float(os.environ.get("BARGE_IN_MARGIN_DB", "9"))

# How long a level has to hold above the margin. 0.32s (4 frames), twice the
# recorder's own ONSET_SECONDS, because this one is competing with a signal
# that is already loud: a door, a desk knock or a chair must never cut off a
# reply.
BARGE_ONSET_SECONDS = float(os.environ.get("BARGE_ONSET_SECONDS", "0.32"))

# The running level is a median over this much recent audio. A median, not a
# mean: a barge-in is short and loud, and would drag a mean up toward itself
# until it stopped being detectable.
BARGE_ECHO_WINDOW_SECONDS = float(os.environ.get("BARGE_ECHO_WINDOW_SECONDS", "2.0"))

# How much audio the window needs before the rule may fire at all. Without it
# the first frames of a reply are judged against a median of one or two
# samples of themselves, which is not a floor.
#
# The cost is a blind window at the very start of a reply -- but only of the
# session's *first* reply: the window is kept between replies, because it
# estimates Jarvis's own voice through the same speakers in the same room and
# that does not change between one sentence and the next.
BARGE_MIN_PRIME_SECONDS = float(os.environ.get("BARGE_MIN_PRIME_SECONDS", "1.0"))

# Whether the words that interrupted him become the next command.
#
# They have to be kept deliberately: the capture gate refused them, so they
# are nowhere else. On they are handed to record_command() as a pre-roll, and
# the first second of that audio is you mixed with him -- which is the reason
# this is a switch. Off, the interruption only stops the reply and the command
# is whatever is said after it.
BARGE_PREROLL = os.environ.get("BARGE_PREROLL", "true").lower() == "true"

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
