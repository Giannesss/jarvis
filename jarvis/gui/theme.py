"""The eighth design pass's theme layer: one place to change colour, in
place of the hand-kept hex constants `_STYLESHEET` used to scatter through
`main_window.py`. This is the closest Qt equivalent to the "CSS variables /
theme file" the redesign brief asked for -- QSS itself has no variable
syntax, so `build_stylesheet()` below is a plain Python function that
string-formats a palette dict into the same QSS `main_window.py` always
built by hand, parametrized on two axes: `mode` ("dark"/"light") and
`accent` (one of ACCENT_CHOICES). Nothing about the selectors, object
names or property names below changed from the pre-theme `_STYLESHEET` --
only the hex values feeding them moved into `PALETTES`/`ACCENTS`, so no
existing object-name-keyed test needed to change for this file to exist.

Switching themes at runtime is just calling `build_stylesheet(mode, accent)`
again and handing the result to `QWidget.setStyleSheet()` -- Qt reapplies a
stylesheet immediately, no restart needed, unlike every other row on the
Settings page (which all come from `.env` and only take effect on the next
launch, per that page's own docstring). The theme toggle is the one setting
on that page that is *not* read-only for exactly that reason: it changes
nothing `config.py` reads from disk, only how this window paints itself,
so there is nothing to desync from `.env` by letting it take effect live.
"""

from __future__ import annotations

# Two font stacks, each falling back to a font that ships with Windows if
# the bundled file isn't present -- same discipline as the pre-existing
# "Cascadia Mono", Consolas, monospace chain elsewhere in this project.
# `tools/fetch_fonts.py` documents how to populate assets/fonts/ with the
# real Inter/JetBrains Mono .ttf files on a machine that actually has
# internet access (this sandbox's network proxy refuses raw.githubusercontent.com,
# so the files can't be fetched from here -- see that script's own docstring).
FONT_UI = '"Inter", "Segoe UI", sans-serif'
FONT_MONO = '"JetBrains Mono", "Cascadia Mono", Consolas, monospace'

# One accent per choice offered in Settings -- a single hue used sparingly
# (the record button, the active nav item, the status dot, a card's own top
# border), never the whole panel. _SECONDARY_ACCENT (violet) and the teal
# Current-Task tint stay fixed decoration regardless of accent choice --
# they pair *with* the chosen accent rather than being replaced by it, the
# same two-tone idea the sixth design pass introduced.
ACCENTS = {
    "blue": "#0a84ff",
    "violet": "#8b5cf6",
    "teal": "#22c3b6",
}
ACCENT_CHOICES = tuple(ACCENTS.keys())
SECONDARY_ACCENT = "#8b5cf6"
ACCENT_TEAL = "#22c3b6"

# Dark is the palette every design pass since the third has actually been
# built and hand-tested against; light is new with this pass and mirrors
# its same elevation structure (background < chrome < surface), just
# inverted, rather than a different design -- the point of a theme toggle
# is the same product in a different light, not two different apps.
PALETTES = {
    "dark": {
        "background": "#0a0a0c",
        "chrome": "#0d0d10",
        "surface": "#141417",
        "surface_raised": "#1c1c1f",
        "hairline": "rgba(255, 255, 255, 0.08)",
        "text_primary": "#f5f5f7",
        "text_secondary": "rgba(245, 245, 247, 0.55)",
        "text_tertiary": "rgba(245, 245, 247, 0.32)",
        "scrollbar": "rgba(255, 255, 255, 0.14)",
        "scrollbar_hover": "rgba(255, 255, 255, 0.22)",
        "glass_fill": "rgba(20, 20, 24, 0.55)",
        "bg_grid": "255, 255, 255",
    },
    "light": {
        "background": "#f4f4f6",
        "chrome": "#ffffff",
        "surface": "#ffffff",
        "surface_raised": "#ececf0",
        "hairline": "rgba(0, 0, 0, 0.08)",
        "text_primary": "#1c1c1f",
        "text_secondary": "rgba(28, 28, 31, 0.6)",
        "text_tertiary": "rgba(28, 28, 31, 0.35)",
        "scrollbar": "rgba(0, 0, 0, 0.14)",
        "scrollbar_hover": "rgba(0, 0, 0, 0.22)",
        "glass_fill": "rgba(255, 255, 255, 0.55)",
        "bg_grid": "0, 0, 0",
    },
}


def _rgba_from_hex(hex_color: str, alpha: float) -> str:
    """"#0a84ff", 0.14 -> "rgba(10, 132, 255, 0.14)" -- every accent-tinted
    fill in the sheet below needs the accent at some alpha, and the accent
    itself is now a runtime choice rather than a literal, so this replaces
    the hand-written rgba(...) literals the pre-theme sheet used for its one
    fixed blue."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha})"


def build_stylesheet(mode: str = "dark", accent: str = "blue") -> str:
    """Returns the whole window's QSS, built from the palette for `mode`
    and the accent hex for `accent`. Falls back to "dark"/"blue" for an
    unrecognised value rather than raising -- a theme toggle is cosmetic,
    and a typo in a stored preference should never be the reason the window
    fails to open."""
    palette = PALETTES.get(mode, PALETTES["dark"])
    accent_hex = ACCENTS.get(accent, ACCENTS["blue"])
    p = palette
    a = accent_hex
    a_soft = _rgba_from_hex(a, 0.14)
    a_border = _rgba_from_hex(a, 0.5)

    return f"""
QMainWindow {{
    background-color: {p['background']};
}}
QWidget {{
    color: {p['text_primary']};
    font-family: {FONT_UI};
    font-size: 13px;
}}

/* The window canvas itself is painted by _Background (a real QWidget
   subclass), not by a rule here -- QSS has no notion of a repeating grid or
   of motion. _Background reads PALETTE/ACCENT at construction and again on
   every theme change (see MainWindow._apply_theme()) so its aurora wash
   matches whichever mode/accent is active. */

QFrame#homeGlow {{
    background-color: qradialgradient(
        cx:0.5, cy:0.42, radius:0.85, fx:0.5, fy:0.42,
        stop:0 {_rgba_from_hex(a, 0.10)},
        stop:0.5 {_rgba_from_hex(a, 0.035)},
        stop:1 {_rgba_from_hex(a, 0.0)}
    );
}}

/* --- Custom title bar --------------------------------------------------- */
QFrame#titleBar {{
    background-color: {p['chrome']};
    border-bottom: 1px solid {p['hairline']};
    min-height: 34px;
    max-height: 34px;
}}
QLabel#titleBarLabel {{
    color: {p['text_secondary']};
    font-size: 12px;
    font-weight: 500;
}}
QPushButton[titleBarButton="true"] {{
    background-color: transparent;
    border: none;
    border-radius: 0;
    padding: 0;
    min-width: 44px;
    max-width: 44px;
    min-height: 34px;
    max-height: 34px;
    color: {p['text_secondary']};
    font-size: 13px;
}}
QPushButton[titleBarButton="true"]:hover {{
    background-color: {p['surface_raised']};
}}
QPushButton#titleBarClose:hover {{
    background-color: #e81123;
    color: #ffffff;
}}

QMenuBar {{
    background-color: {p['chrome']};
    color: {p['text_primary']};
    border-bottom: 1px solid {p['hairline']};
}}
QMenuBar::item {{
    background-color: transparent;
    padding: 4px 10px;
}}
QMenuBar::item:selected {{
    background-color: {p['surface_raised']};
    border-radius: 5px;
}}
QMenu {{
    background-color: {p['surface_raised']};
    color: {p['text_primary']};
    border: 1px solid {p['hairline']};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 24px 6px 12px;
    border-radius: 5px;
}}
QMenu::item:selected {{
    background-color: {a_soft};
    color: {p['text_primary']};
}}

/* --- Header -------------------------------------------------------------- */
QFrame#header {{
    background-color: {p['chrome']};
    border-bottom: 1px solid {p['hairline']};
    min-height: 56px;
    max-height: 56px;
}}
QLabel#brandTitle {{
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 0.4px;
    color: {p['text_primary']};
}}
QLabel#status_label {{
    font-size: 12px;
    font-weight: 500;
    color: {p['text_secondary']};
}}
QLabel#clock_label {{
    font-family: {FONT_MONO};
    font-size: 17px;
    font-weight: 600;
    letter-spacing: 0.5px;
    color: {p['text_primary']};
}}
QLabel#date_label {{
    color: {p['text_tertiary']};
    font-size: 11px;
}}

/* --- Sidebar --------------------------------------------------------------- */
QFrame#sidebar {{
    background-color: {p['chrome']};
    border-right: 1px solid {p['hairline']};
    min-width: 196px;
    max-width: 196px;
}}
QLabel#sidebarBrand {{
    color: {p['text_primary']};
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 2px;
    padding: 4px 10px 10px 10px;
}}
QLabel#sidebarSection {{
    color: {p['text_tertiary']};
    font-size: 10.5px;
    font-weight: 600;
    letter-spacing: 1.2px;
    padding: 10px 10px 2px 10px;
}}
QFrame#sidebarDivider {{
    background-color: {p['hairline']};
    max-height: 1px;
    min-height: 1px;
    border: none;
    margin: 6px 4px;
}}
QPushButton[navButton="true"] {{
    text-align: left;
    padding: 9px 14px;
    margin: 1px 12px;
    border: none;
    border-radius: 7px;
    background-color: transparent;
    color: {p['text_secondary']};
    font-weight: 500;
    font-size: 13px;
}}
QPushButton[navButton="true"]:checked {{
    background-color: {a_soft};
    color: {a};
    font-weight: 600;
}}
QPushButton[navButton="true"]:hover:!checked {{
    background-color: {p['surface_raised']};
    color: {p['text_primary']};
}}

/* --- Panels (System Status / Quick Actions / Current Task / Settings) -- */
QFrame#panel {{
    background-color: {p['glass_fill']};
    border: 1px solid {p['hairline']};
    border-radius: 14px;
}}
QFrame#panel[accentColor="blue"] {{
    border-top: 1px solid {_rgba_from_hex(ACCENTS['blue'], 0.55)};
}}
QFrame#panel[accentColor="violet"] {{
    border-top: 1px solid {_rgba_from_hex(ACCENTS['violet'], 0.5)};
}}
QFrame#panel[accentColor="teal"] {{
    border-top: 1px solid {_rgba_from_hex(ACCENTS['teal'], 0.5)};
}}
QLabel#panelTitle {{
    color: {p['text_secondary']};
    font-size: 12.5px;
    font-weight: 600;
}}
QLabel[metric="true"] {{
    font-family: {FONT_MONO};
    font-size: 13.5px;
    font-weight: 500;
    letter-spacing: 0.2px;
    color: {p['text_primary']};
    padding: 1px 0;
}}

QProgressBar {{
    background-color: {p['surface_raised']};
    border: none;
    border-radius: 2px;
}}
QProgressBar::chunk {{
    border-radius: 2px;
}}
QProgressBar#cpu_bar::chunk {{
    background-color: {ACCENTS['blue']};
}}
QProgressBar#ram_bar::chunk {{
    background-color: {ACCENTS['violet']};
}}
QProgressBar#vram_bar::chunk {{
    background-color: {ACCENTS['teal']};
}}
QLabel#pageTitle {{
    font-size: 20px;
    font-weight: 600;
    color: {p['text_primary']};
}}
QLabel#mutedText {{
    color: {p['text_tertiary']};
}}
QLabel#current_task_label {{
    color: {p['text_primary']};
    font-size: 13px;
}}

/* --- Home page: greeting / state word / prompt / divider / activity ---- */
QLabel#home_greeting_label {{
    font-size: 15px;
    font-weight: 500;
    color: {p['text_secondary']};
}}
QLabel#home_state_label {{
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 2px;
    color: {p['text_secondary']};
}}
QLabel#home_prompt_label {{
    font-size: 13px;
    font-style: italic;
    color: {p['text_tertiary']};
}}
QFrame#divider {{
    background-color: {p['hairline']};
    max-height: 1px;
    min-height: 1px;
    border: none;
    margin: 10px 0;
}}
QPushButton[secondaryAction="true"] {{
    background-color: transparent;
    border: 1px solid {p['hairline']};
    color: {p['text_secondary']};
    padding: 12px 28px;
    border-radius: 22px;
    font-weight: 500;
}}
QPushButton[secondaryAction="true"]:hover {{
    background-color: {p['surface_raised']};
}}
QListWidget#recent_activity_list {{
    background-color: transparent;
    border: none;
    padding: 0;
}}
QListWidget#recent_activity_list::item {{
    color: {p['text_secondary']};
    font-size: 12px;
    padding: 3px 4px;
}}
QLabel#recentActivityEmpty {{
    color: {p['text_tertiary']};
    font-size: 12px;
    font-style: italic;
    padding: 6px 2px;
}}

QLabel#settingKey {{
    color: {p['text_tertiary']};
    font-weight: 500;
}}
QLabel#settingValue {{
    color: {p['text_primary']};
    font-weight: 500;
}}

/* --- Lists ----------------------------------------------------------------- */
QListWidget {{
    background-color: {p['surface']};
    border: 1px solid {p['hairline']};
    border-radius: 12px;
    padding: 4px;
    outline: none;
}}
QListWidget::item {{
    padding: 8px 10px;
    border-radius: 7px;
    color: {p['text_primary']};
}}
QListWidget::item:selected {{
    background-color: {a_soft};
    color: {p['text_primary']};
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {p['scrollbar']};
    border-radius: 4px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {p['scrollbar_hover']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

/* --- Buttons ----------------------------------------------------------------- */
QPushButton {{
    background-color: {p['surface_raised']};
    border: 1px solid {p['hairline']};
    border-radius: 9px;
    padding: 8px 14px;
    color: {p['text_primary']};
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: {p['surface']};
}}
QPushButton:pressed {{
    background-color: {p['background']};
}}
QPushButton:disabled {{
    color: {p['text_tertiary']};
    background-color: {p['surface']};
    border: 1px solid {p['hairline']};
}}
QPushButton[quickAction="true"] {{
    text-align: left;
    padding: 9px 12px;
    background-color: transparent;
    border: none;
}}
QPushButton[quickActionTile="true"] {{
    padding: 7px 9px;
    font-size: 12px;
    border-radius: 7px;
}}
QPushButton[quickAction="true"]:hover {{
    background-color: {p['surface_raised']};
}}
QPushButton[quickAction="true"]:pressed {{
    background-color: {p['surface']};
}}

QPushButton#record_button {{
    background-color: qlineargradient(
        x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENTS['blue']}, stop:1 {ACCENTS['violet']}
    );
    border: none;
    border-radius: 22px;
    padding: 12px 40px;
    font-size: 14px;
    font-weight: 600;
    color: #ffffff;
}}
QPushButton#record_button:hover {{
    background-color: qlineargradient(
        x1:0, y1:0, x2:1, y2:0, stop:0 #2894ff, stop:1 #9d6ff7
    );
}}
QPushButton#record_button:pressed {{
    background-color: qlineargradient(
        x1:0, y1:0, x2:1, y2:0, stop:0 #0870d6, stop:1 #7142d9
    );
}}
QPushButton#record_button:disabled {{
    background-color: {p['surface_raised']};
    color: {p['text_tertiary']};
}}

/* --- Command palette --------------------------------------------------------- */
QDialog#commandPalette {{
    background-color: {p['surface_raised']};
    border: 1px solid {a_border};
    border-radius: 14px;
}}
QLineEdit#commandPaletteInput {{
    background-color: transparent;
    border: none;
    border-bottom: 1px solid {p['hairline']};
    padding: 12px 16px;
    font-size: 15px;
    color: {p['text_primary']};
}}
QListWidget#commandPaletteList {{
    background-color: transparent;
    border: none;
    padding: 4px;
}}

/* --- Toasts ------------------------------------------------------------------ */
QFrame#toast {{
    background-color: {p['surface_raised']};
    border: 1px solid {a_border};
    border-radius: 10px;
}}
QLabel#toastLabel {{
    color: {p['text_primary']};
    font-size: 12.5px;
    font-weight: 500;
}}

/* --- Form (Settings page) ----------------------------------------------------- */
QCheckBox {{
    font-weight: 500;
    spacing: 8px;
    padding: 4px 0;
    color: {p['text_primary']};
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {p['hairline']};
    border-radius: 4px;
    background-color: {p['surface']};
}}
QCheckBox::indicator:checked {{
    background-color: {a};
    border: 1px solid {a};
}}
QComboBox {{
    background-color: {p['surface']};
    border: 1px solid {p['hairline']};
    border-radius: 7px;
    padding: 5px 10px;
    color: {p['text_primary']};
}}
QComboBox QAbstractItemView {{
    background-color: {p['surface_raised']};
    color: {p['text_primary']};
    selection-background-color: {a_soft};
    border: 1px solid {p['hairline']};
}}
"""
