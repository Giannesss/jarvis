"""Phase 6's GUI shell (see CLAUDE.md "Phase 6 -- a visible Jarvis").

Deliberately its own package, separate from jarvis/*.py: everything under
jarvis/ outside this package is backend logic the GUI calls into, never the
other way around, and nothing here is imported by main.py or the CLI path.
`gui_main.py` at the repo root is the GUI's own entry point, parallel to
main.py rather than replacing it -- the CLI stays the always-available path
while the GUI is being built out one step at a time.
"""
