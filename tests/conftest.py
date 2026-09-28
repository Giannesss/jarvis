"""Session-wide test setup.

A test run must not depend on whichever machine or .env it happens to run
on -- same reasoning as test_player.py patching PLAYBACK_DEVICE rather than
trusting whatever is plugged in. This file pins BRAIN_PROVIDER to "ollama"
before anything imports jarvis.config, regardless of what a developer's own
.env sets it to.

That is not just tidiness: BRAIN_PROVIDER=claude reaches the real network and
costs real money per call, and several existing tests
(tests/test_brain_stream.py, tests/test_memory_skills.py) patch
_PROVIDERS["ollama"] / _STREAM_PROVIDERS["ollama"] directly -- that patch
only intercepts anything if jarvis.brain actually dispatches through the
"ollama" key. A developer running the suite with BRAIN_PROVIDER=claude set
locally (to hand-test the Claude brain, say) would otherwise have those
tests silently fall through to the real _ask_claude/_stream_claude and hit
the real API instead of the fake providers they think they installed.

os.environ is set here before jarvis.config's load_dotenv() ever runs, and
load_dotenv() never overrides an already-set variable (its default is
override=False) -- so this wins regardless of .env's own value.

tests/test_brain_claude.py's ImportGuardTests still exercises
BRAIN_PROVIDER="claude" explicitly, via mock.patch.object(config, ...) +
importlib.reload -- that patches the config module's attribute directly and
is unaffected by what this file does to os.environ.
"""

import os

os.environ["BRAIN_PROVIDER"] = "ollama"
