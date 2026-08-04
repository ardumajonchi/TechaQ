# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Tests for engine/settings.py's SettingsStore -- same real-sqlite-via-conftest-stub approach as
test_library.py uses for BookDB, against a temp file path per test."""

from __future__ import annotations

import pytest

from engine.settings import SettingsStore


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_techaq.db")


@pytest.fixture
def store(db_path):
    s = SettingsStore(db_path)
    yield s
    s.stop()


def test_defaults_on_first_run(store):
    assert store.get() == {
        "fetch_synopsis_default": False,
        "ui_language": "en",
        "ui_theme": "dark",
    }


def test_construction_is_idempotent_and_preserves_existing_row(db_path):
    first = SettingsStore(db_path)
    first.update({"ui_theme": "light"})
    first.stop()

    second = SettingsStore(db_path)
    assert second.get()["ui_theme"] == "light"
    second.stop()


def test_update_fetch_synopsis_default(store):
    result = store.update({"fetch_synopsis_default": True})
    assert result["fetch_synopsis_default"] is True
    assert store.get()["fetch_synopsis_default"] is True


def test_update_ui_language(store):
    result = store.update({"ui_language": "it"})
    assert result["ui_language"] == "it"
    assert store.get()["ui_language"] == "it"


def test_update_ui_theme(store):
    result = store.update({"ui_theme": "light"})
    assert result["ui_theme"] == "light"
    assert store.get()["ui_theme"] == "light"


def test_update_rejects_unsupported_language(store):
    with pytest.raises(ValueError):
        store.update({"ui_language": "klingon"})
    assert store.get()["ui_language"] == "en"  # unchanged


def test_update_rejects_unsupported_theme(store):
    with pytest.raises(ValueError):
        store.update({"ui_theme": "neon"})
    assert store.get()["ui_theme"] == "dark"  # unchanged


def test_partial_update_leaves_other_fields_untouched(store):
    store.update({"ui_theme": "light"})
    store.update({"ui_language": "fr"})
    settings = store.get()
    assert settings["ui_theme"] == "light"
    assert settings["ui_language"] == "fr"
    assert settings["fetch_synopsis_default"] is False


def test_update_empty_partial_returns_current_settings(store):
    store.update({"ui_theme": "light"})
    assert store.update({}) == store.get()


def test_update_multiple_fields_at_once(store):
    result = store.update(
        {"fetch_synopsis_default": True, "ui_language": "de", "ui_theme": "light"}
    )
    assert result == {
        "fetch_synopsis_default": True,
        "ui_language": "de",
        "ui_theme": "light",
    }
