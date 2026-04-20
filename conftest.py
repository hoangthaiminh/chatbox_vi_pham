"""Pytest configuration for the violations app.

Running:
    pip install pytest pytest-django
    pytest -v

Tests that require Django (views, image uploads, models) use pytest-django's
`db` fixture. Tests for pure-Python helpers (regex, services) don't need the
DB and run without fixtures.
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "chatbox_vi_pham.settings")
