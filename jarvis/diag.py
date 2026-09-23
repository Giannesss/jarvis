"""The diagnostic log: everything the terminal prints, kept on disk.

Jarvis's hardest bugs have all been timing ones -- a 43-second recording, a
0.05-second one, a wake word swallowed by a reset -- and every one of them
was diagnosed from a [timing] or [rec] line in the terminal. Twice now the
line that would have settled a question was in a scrollback buffer that no
longer existed, so the question could only be reopened by reproducing the
fault live. This module is that gap closed: log() prints exactly what
print() used to and appends the same line, timestamped, to LOG_PATH.

Three properties it has to keep:

  * **It never costs a turn.** Every filesystem call is wrapped and failures
    are swallowed -- a full disk or a locked file loses a log line, never a
    reply. Same discipline as db.backup() at startup and memory.recall_safe().
    After the first failure it stops trying, so a broken path cannot cost a
    syscall per frame for the rest of the run.
  * **It mirrors stdout rather than replacing it.** Callers keep their
    existing gating: a line printed only under WAKE_DEBUG is logged only
    under WAKE_DEBUG. The file is a record of the session as it was shown,
    which is what makes it comparable to a pasted scrollback.
  * **It holds no transcribed text.** Timings, mic levels, stop reasons and
    wake scores only -- the same rule audit rows follow (see policy.py).
    It still lands under data/, gitignored, because levels and timings
    describe someone's room even when the words are absent.

Opened and closed per line rather than held open: these lines arrive at most
a few times a second, and a handle buffered across a hard crash would lose
precisely the last few lines -- the ones describing the crash.
"""

from __future__ import annotations

import time

from jarvis.config import LOG_ENABLED, LOG_KEEP, LOG_MAX_BYTES, LOG_PATH

# Starts disabled and is switched on by start_session(), so the log records
# *runs of Jarvis* rather than anything that merely imports the package. That
# is what keeps the test suite -- which imports speaker, skills and listener
# freely but never calls main() -- from writing fake timings into the real
# log, whatever way the tests are invoked. Also set when a write fails, so a
# broken or unwritable path costs one failed syscall per run, not one per line.
_disabled = True


def _rotate() -> None:
    """jarvis.log -> jarvis.log.1 -> ... -> jarvis.log.<LOG_KEEP>, oldest
    dropped. Called only when the live file is already over the cap, so the
    cost is paid once per LOG_MAX_BYTES rather than per line."""
    oldest = LOG_PATH.with_suffix(LOG_PATH.suffix + f".{LOG_KEEP}")
    oldest.unlink(missing_ok=True)

    for index in range(LOG_KEEP - 1, 0, -1):
        older = LOG_PATH.with_suffix(LOG_PATH.suffix + f".{index}")
        if older.exists():
            older.replace(LOG_PATH.with_suffix(LOG_PATH.suffix + f".{index + 1}"))

    LOG_PATH.replace(LOG_PATH.with_suffix(LOG_PATH.suffix + ".1"))


def write(message: str) -> None:
    """Append one timestamped line, printing nothing. For the rare caller
    that wants the record without the terminal noise; log() is the usual one."""
    global _disabled
    if _disabled:
        return

    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

        if LOG_PATH.exists() and LOG_PATH.stat().st_size >= LOG_MAX_BYTES:
            _rotate()

        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as log_file:
            print(f"{stamp} {message}", file=log_file)
    except OSError:
        # Deliberately silent and one-way: printing a warning per frame would
        # be worse than the missing log, and the line itself still reached
        # the terminal via log().
        _disabled = True


def log(message: str) -> None:
    """Print the line and record it. A drop-in for the print() calls that
    carried Jarvis's diagnostics before the file existed."""
    print(message)
    write(message)


def start_session(text: str) -> None:
    """Open the log for this run and mark its start.

    Until this is called nothing is written, so importing jarvis.* is silent.
    The banner separates one session's lines from the last one's, since the
    file spans restarts.
    """
    global _disabled
    if not LOG_ENABLED:
        return

    _disabled = False
    write(f"--- {text} ---")
