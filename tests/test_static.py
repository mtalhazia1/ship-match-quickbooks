"""The stylesheet is shared by every feature; a lost brace silently moves later rules into a media query."""
import re
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "static" / "css" / "app.css"


def test_stylesheet_braces_are_balanced_and_every_section_starts_at_top_level():
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), CSS.read_text(), flags=re.S)
    raw = CSS.read_text().split("\n")
    depth = 0
    for number, line in enumerate(text.split("\n"), 1):
        if "=====" in raw[number - 1]:
            assert depth == 0, f"section at line {number} starts inside an unclosed block"
        depth += line.count("{") - line.count("}")
        assert depth >= 0, f"extra closing brace at line {number}"
    assert depth == 0
