"""URL configuration loads in both debug and production mode (the debug-only branch serves media)."""
import importlib

import config.urls


def test_urls_load_in_debug_and_production(settings):
    try:
        for debug in (True, False):
            settings.DEBUG = debug
            module = importlib.reload(config.urls)
            names = [str(p.pattern) for p in module.urlpatterns]
            assert ("^media/(?P<path>.*)$" in names) is debug
            assert "favicon.ico" in names
    finally:
        settings.DEBUG = False
        importlib.reload(config.urls)
