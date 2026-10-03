"""Entry point for Jarvis's GUI shell (Phase 6). Parallel to main.py, not a
replacement for it: the Enter-press/wake-word CLI stays the always-available
path while the GUI is being built out one step at a time, and nothing in
jarvis/gui/ is imported by main.py or vice versa.

Run:
    .\\.venv\\Scripts\\python.exe gui_main.py
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from jarvis.gui.main_window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
