"""Downloads the real Inter + JetBrains Mono .ttf files into assets/fonts/,
for the eighth GUI design pass's typography (see "Typography and polish" in
the redesign brief, and jarvis/gui/theme.py's FONT_UI/FONT_MONO).

**Why this is a separate script run by hand, not something the agent did
for you:** the sandbox this was built in proxies all outbound traffic
through an allowlist that covers pypi.org/npm's registries but refuses
raw.githubusercontent.com (confirmed: a 403 from the proxy, not from
GitHub), so the actual font bytes could not be fetched from there. Your own
machine has ordinary internet access, so this script does the one-time
fetch from here instead. Run once, from the repo root:

    .\\.venv\\Scripts\\python.exe tools\\fetch_fonts.py

**Bundling is optional, not required.** `gui_main.py` calls
`QFontDatabase.addApplicationFont()` for each file in assets/fonts/ at
startup and simply skips any that aren't there; `theme.py`'s font-family
strings already list "Segoe UI"/"Cascadia Mono, Consolas, monospace" as the
fallback, the same discipline every other font declaration in this project
already follows. The app looks and runs identically either way -- this
script only makes the UI actually render in Inter/JetBrains Mono instead of
falling back to what Windows ships with.
"""

from __future__ import annotations

import pathlib
import urllib.request

FONTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "assets" / "fonts"

# (destination filename, source URL) -- both families' real, open-source
# (SIL OFL) font files, from their own upstream GitHub repos.
_FILES = [
    (
        "Inter-Regular.ttf",
        "https://raw.githubusercontent.com/rsms/inter/master/docs/font-files/Inter-Regular.ttf",
    ),
    (
        "Inter-Medium.ttf",
        "https://raw.githubusercontent.com/rsms/inter/master/docs/font-files/Inter-Medium.ttf",
    ),
    (
        "Inter-SemiBold.ttf",
        "https://raw.githubusercontent.com/rsms/inter/master/docs/font-files/Inter-SemiBold.ttf",
    ),
    (
        "Inter-Bold.ttf",
        "https://raw.githubusercontent.com/rsms/inter/master/docs/font-files/Inter-Bold.ttf",
    ),
    (
        "JetBrainsMono-Regular.ttf",
        "https://raw.githubusercontent.com/JetBrains/JetBrainsMono/master/fonts/ttf/JetBrainsMono-Regular.ttf",
    ),
    (
        "JetBrainsMono-Medium.ttf",
        "https://raw.githubusercontent.com/JetBrains/JetBrainsMono/master/fonts/ttf/JetBrainsMono-Medium.ttf",
    ),
]


def main() -> None:
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, url in _FILES:
        dest = FONTS_DIR / filename
        print(f"Fetching {filename} ...")
        urllib.request.urlretrieve(url, dest)
        print(f"  -> {dest} ({dest.stat().st_size} bytes)")
    print("Done. Restart gui_main.py to pick up the bundled fonts.")


if __name__ == "__main__":
    main()
