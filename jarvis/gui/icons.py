"""Small, original line icons for the eighth design pass -- replacing the
Unicode glyphs (⌂◉✓▣⚙⚡◌) the seventh pass used for the sidebar and the
plain painted dots `_dot_icon()` used for quick actions, per the redesign
brief's "add proper icons instead of text symbols".

Every icon here is a short, hand-written inline SVG path -- nobody's brand
mark, nobody's icon-font glyph, drawn specifically for this app the same
way `_Orb` is an original avatar rather than a copy of Iron Man's helmet
(see main_window.py's own docstring). `icon(name, color)` renders one at a
requested size via `QSvgRenderer` into a `QPixmap`, the same "draw it once,
hand back a QIcon" shape `_dot_icon()` used, so every existing call site
that calls `button.setIcon(...)` keeps working whether the icon came from
here or from a painted circle -- `QPushButton.setIcon()` never touches
`.text()`, so no object-name or exact-label assertion in
`tests/test_gui_shell.py` is affected by swapping one icon source for
another.

Rendering from an in-memory SVG string (not a bundled .svg *file*) avoids
a second kind of asset path to manage alongside the bundled fonts --
there's nothing here that benefits from being on disk rather than in code,
unlike a real font file, which has to be a file for QFontDatabase to load."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# Each path is drawn on a 24x24 grid, stroke-only, 1.6px stroke -- a
# consistent, restrained line-icon style (closer to Linear/Raycast's own
# icon set in spirit, not a literal copy of either) rather than mixing
# filled glyphs of different weights.
_STROKE = (
    'fill="none" stroke="{color}" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round"'
)

_PATHS: dict[str, str] = {
    "home": '<path {stroke} d="M3 11.5 12 4l9 7.5"/><path {stroke} d="M5.5 10v9h13v-9"/>'
    '<path {stroke} d="M10 19v-5h4v5"/>',
    "chat": '<path {stroke} d="M4 5.5h16v10.5H9l-4 3.5v-3.5H4z"/>'
    '<path {stroke} d="M8 9.5h8M8 12.5h5"/>',
    "tasks": '<path {stroke} d="M5 12.5 9 16l10-10"/>',
    "files": '<path {stroke} d="M5 5.5h5.5L13 8h5.5v10.5H5z"/>',
    "settings": '<circle {stroke} cx="12" cy="12" r="3"/>'
    '<path {stroke} d="M12 4v2.3M12 17.7V20M4 12h2.3M17.7 12H20'
    'M6.3 6.3l1.6 1.6M16.1 16.1l1.6 1.6M17.7 6.3l-1.6 1.6M7.9 16.1l-1.6 1.6"/>',
    "automation-active": '<path {stroke} d="M13 3 5 13.5h5.5L11 21l8-10.5h-5.5z"/>',
    "automation-history": '<circle {stroke} cx="12" cy="12" r="8"/>'
    '<path {stroke} d="M12 7.5V12l3 2"/>',
    "site": '<circle {stroke} cx="12" cy="12" r="8"/>'
    '<path {stroke} d="M4 12h16M12 4c2.2 2.2 3.3 5.1 3.3 8s-1.1 5.8-3.3 8'
    'c-2.2-2.2-3.3-5.1-3.3-8S9.8 6.2 12 4z"/>',
    "app": '<rect {stroke} x="5" y="5" width="14" height="14" rx="3"/>'
    '<path {stroke} d="M9 12h6M12 9v6"/>',
    "minimize": '<path {stroke} d="M6 12h12"/>',
    "maximize": '<rect {stroke} x="6.5" y="6.5" width="11" height="11" rx="1.5"/>',
    "close": '<path {stroke} d="M7 7l10 10M17 7 7 17"/>',
    "palette": '<circle {stroke} cx="12" cy="12" r="8"/>'
    '<circle fill="{color}" cx="9" cy="10" r="1.1"/>'
    '<circle fill="{color}" cx="12.5" cy="8.3" r="1.1"/>'
    '<circle fill="{color}" cx="15.3" cy="11" r="1.1"/>',
    "empty-activity": '<circle {stroke} cx="12" cy="12" r="8.2"/>'
    '<path {stroke} d="M12 8v4.3l2.8 2"/>',
    # The record button's own icon (replacing its "Εγγραφή" text label) --
    # a plain microphone capsule plus its stand, the universal shorthand
    # for "press to talk" that needs no caption next to it. setToolTip()
    # on the button itself still carries "Εγγραφή" for anyone hovering or
    # using a screen reader, so nothing about the button's accessible name
    # is lost by dropping the visible word.
    "mic": '<rect {stroke} x="9" y="3.5" width="6" height="11" rx="3"/>'
    '<path {stroke} d="M6 11v1a6 6 0 0 0 12 0v-1M12 18v3M9 21h6"/>',
}


def _svg_document(name: str, color: str) -> bytes:
    stroke = _STROKE.format(color=color)
    body = _PATHS[name].format(stroke=stroke, color=color)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">{body}</svg>'
    ).encode("utf-8")


def icon(name: str, color: str = "#f5f5f7", size: int = 18) -> QIcon:
    """Renders one of the named icons above at `size` px, tinted `color` --
    a fresh QPixmap every call rather than a cache, since the same icon is
    asked for in more than one colour across a theme change (see
    MainWindow._apply_theme()) and this is a handful of tiny vector paths,
    not an expensive decode."""
    renderer = QSvgRenderer(QByteArray(_svg_document(name, color)))
    pixmap = QPixmap(QSize(size, size))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)
