"""Entry point for Jarvis's GUI shell (Phase 6). Parallel to main.py, not a
replacement for it: the Enter-press/wake-word CLI stays the always-available
path while the GUI is being built out one step at a time, and nothing in
jarvis/gui/ is imported by main.py or vice versa.

Run:
    .\\.venv\\Scripts\\python.exe gui_main.py
"""

from __future__ import annotations

import pathlib
import sys

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from jarvis.gui.main_window import MainWindow

# Bundled Inter/JetBrains Mono .ttf files (eighth design pass -- see
# jarvis/gui/theme.py and tools/fetch_fonts.py). Loaded before the window is
# constructed so every stylesheet's font-family rule can simply name
# "Inter"/"JetBrains Mono" and let Qt resolve it -- addApplicationFont()
# registers the family with the OS font database for this process only,
# nothing is installed system-wide. A missing directory or a missing file is
# not an error: theme.py's font-family strings already list "Segoe UI"/
# "Cascadia Mono, Consolas, monospace" right after the bundled name, so Qt
# falls back exactly the way every other font declaration in this project
# already does when its first choice isn't available.
_FONTS_DIR = pathlib.Path(__file__).resolve().parent / "assets" / "fonts"


def _load_bundled_fonts() -> None:
    if not _FONTS_DIR.is_dir():
        return
    for font_file in sorted(_FONTS_DIR.glob("*.ttf")):
        QFontDatabase.addApplicationFont(str(font_file))


def main() -> None:
    app = QApplication(sys.argv)
    _load_bundled_fonts()
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
