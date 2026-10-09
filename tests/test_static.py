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


def test_production_static_manifest_has_every_file_templates_ask_for(settings, tmp_path):
    """QA-063: production serves hashed file names (cached for good), and a {% static %} path missing from the
    manifest is a server error there. Builds the manifest as `collectstatic` does in production."""
    import json

    from django.core.management import call_command

    root = Path(settings.BASE_DIR)
    settings.STATIC_ROOT = tmp_path
    settings.STORAGES = {**settings.STORAGES,
                         "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}}
    call_command("collectstatic", interactive=False, verbosity=0)   # Django resets the storage on these settings
    manifest = json.loads((tmp_path / "staticfiles.json").read_text())["paths"]
    asked = set()
    for folder in ("templates", "apps"):
        for html in (root / folder).rglob("*.html"):
            asked |= set(re.findall(r"""\{%\s*static\s+['"]([^'"]+)['"]""", html.read_text(encoding="utf-8")))
    assert asked, "no {% static %} tags found: the pattern above is out of date"
    assert sorted(asked - set(manifest)) == []


def test_urls_load_in_production_before_collectstatic(settings, tmp_path):
    """The production container runs migrate (whose checks import the URLs) before collectstatic, so nothing may
    look up a hashed static name at import time."""
    import importlib

    import config.urls

    settings.STATIC_ROOT = tmp_path   # empty: no manifest yet
    settings.STORAGES = {**settings.STORAGES,
                         "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}}
    try:
        importlib.reload(config.urls)
    finally:
        settings.STORAGES = {**settings.STORAGES,
                             "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
        importlib.reload(config.urls)
