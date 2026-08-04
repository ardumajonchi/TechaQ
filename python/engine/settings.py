# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Thin wrapper over arduino:dbstorage_sqlstore's SQLStore. Owns the `app_settings` table --
a single fixed row (id=1) holding this app's shared preferences (synopsis-fetch default, UI
language, UI theme). Settings are server-side and shared across every device viewing this app
(phone scanner station, tablet, desktop CLI), the same way books are -- not per-browser
localStorage.

Mirrors db.py's BookDB construction idiom exactly (owns its own SQLStore onto the same database
file, create_table on init)."""

from __future__ import annotations

from arduino.app_bricks.dbstorage_sqlstore import SQLStore

from .db import DB_NAME

TABLE = "app_settings"

SCHEMA = {
    "id": "INTEGER PRIMARY KEY",
    "fetch_synopsis_default": "INTEGER",
    "ui_language": "TEXT",
    "ui_theme": "TEXT",
}

LANGUAGES = ("en", "it", "de", "fr", "es")
THEMES = ("dark", "light")

_DEFAULTS = {
    "fetch_synopsis_default": False,
    "ui_language": "en",
    "ui_theme": "dark",
}


class SettingsStore:
    def __init__(self, database_name: str = DB_NAME):
        self._store = SQLStore(database_name)
        self._store.start()
        self._store.create_table(TABLE, SCHEMA)
        rows = self._store.read(TABLE, condition="id = 1", limit=1)
        if not rows:
            self._store.store(
                TABLE,
                {
                    "id": 1,
                    "fetch_synopsis_default": int(_DEFAULTS["fetch_synopsis_default"]),
                    "ui_language": _DEFAULTS["ui_language"],
                    "ui_theme": _DEFAULTS["ui_theme"],
                },
                create_table=False,
            )

    def stop(self) -> None:
        self._store.stop()

    def get(self) -> dict:
        rows = self._store.read(TABLE, condition="id = 1", limit=1)
        row = rows[0] if rows else {}
        return {
            "fetch_synopsis_default": bool(row.get("fetch_synopsis_default", _DEFAULTS["fetch_synopsis_default"])),
            "ui_language": row.get("ui_language") or _DEFAULTS["ui_language"],
            "ui_theme": row.get("ui_theme") or _DEFAULTS["ui_theme"],
        }

    def update(self, partial: dict) -> dict:
        row: dict = {}
        if "fetch_synopsis_default" in partial:
            row["fetch_synopsis_default"] = int(bool(partial["fetch_synopsis_default"]))
        if "ui_language" in partial:
            language = partial["ui_language"]
            if language not in LANGUAGES:
                raise ValueError(f"unsupported ui_language: {language!r}")
            row["ui_language"] = language
        if "ui_theme" in partial:
            theme = partial["ui_theme"]
            if theme not in THEMES:
                raise ValueError(f"unsupported ui_theme: {theme!r}")
            row["ui_theme"] = theme
        if row:
            self._store.update(TABLE, row, condition="id = 1")
        return self.get()
