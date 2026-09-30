"""Session-wide test setup.

A test run must not depend on whichever machine or .env it happens to run
on -- same reasoning as test_player.py patching PLAYBACK_DEVICE rather than
trusting whatever is plugged in. This file pins BRAIN_PROVIDER to "ollama"
before anything imports jarvis.config, regardless of what a developer's own
.env sets it to.

That is not just tidiness. jarvis/brain.py fixes BRAIN_PROVIDER at import
time (module-level `BRAIN_PROVIDER = config.BRAIN_PROVIDER`), and several
tests that predate the Claude provider -- tests/test_brain_stream.py,
tests/test_memory_skills.py -- patch _PROVIDERS["ollama"] /
_STREAM_PROVIDERS["ollama"] directly, on the assumption that BRAIN_PROVIDER
is "ollama". That patch only intercepts anything if brain.ask()/start_turn()
actually dispatch through the "ollama" key. A developer running the suite
with BRAIN_PROVIDER=claude set locally (to hand-test the Claude brain, say)
would otherwise have those tests silently fall through to the real
_ask_claude/_stream_claude -- hitting the real network and spending real
money on every test run, or erroring confusingly if the anthropic package
weren't installed in whatever environment pytest runs under.

tests/test_brain_claude.py does not have this problem: every test in it
patches brain._get_client and, where it dispatches through _PROVIDERS at
all, brain.BRAIN_PROVIDER explicitly (see its own module docstring) -- so it
runs the same whatever this file pins the ambient default to.

os.environ is set here before jarvis.config's load_dotenv() ever runs, and
load_dotenv() never overrides an already-set variable (its default is
override=False) -- so this wins regardless of .env's own value.
"""

import os

os.environ["BRAIN_PROVIDER"] = "ollama"
