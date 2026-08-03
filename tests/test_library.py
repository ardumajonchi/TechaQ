# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Tests for engine/library.py -- the one real code path shared by main.py and cli.py.

Uses the REAL BookDB (backed by the sqlite-based SQLStore stub installed in tests/conftest.py,
against a temp file path per test) so CRUD/search/location-filter behavior is exercised
end-to-end. `metadata`, `ai_search` (AISearchAgent), and `ocr` are monkeypatched at the
`engine.library` module level (where Library imported them) so no network access or `arduino`
package is required, per the testing brief.
"""

from __future__ import annotations

import os

import pytest

from engine import library as library_mod
from engine.db import BookDB
from engine.library import Library, book_to_dict
from engine.models import BookRecord


class FakeHardware:
    """Records which play_* methods were called, instead of touching a real Bridge/MCU."""

    def __init__(self):
        self.calls = []

    def _record(self, name):
        self.calls.append(name)

    def play_scan(self):
        self._record("play_scan")

    def play_save(self):
        self._record("play_save")

    def play_search(self):
        self._record("play_search")

    def play_error(self):
        self._record("play_error")

    def play_delete(self):
        self._record("play_delete")

    def play_startup(self):
        self._record("play_startup")


class FakeAISearchAgent:
    def __init__(self, available=True, results=None):
        self.available = available
        self._results = results or []

    def describe_to_find(self, description: str):
        return self._results


def make_book(**overrides) -> BookRecord:
    defaults = dict(
        title="Dune",
        authors=["Frank Herbert"],
        isbn13="9780441013593",
        publisher="Ace",
        room="Living Room",
        floor="1",
        column="A",
        shelf="3",
        source="manual",
    )
    defaults.update(overrides)
    return BookRecord(**defaults)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_techaq.db")


@pytest.fixture
def library(db_path, monkeypatch):
    """A Library wired to a real (temp-file) BookDB, a FakeHardware, and metadata/ai_search/ocr
    left at their default (present-but-monkeypatched-per-test) state. Individual tests further
    monkeypatch engine.library.metadata / .AISearchAgent / .ocr as needed."""
    hw = FakeHardware()
    lib = Library(db_name=db_path, hw=hw, ai_agent=None)
    yield lib
    lib.close()


# ---------------------------------------------------------------------------
# add_book / get_book / update_book / delete_book
# ---------------------------------------------------------------------------


def test_add_book_returns_id_and_persists(library):
    book_id = library.add_book(make_book())
    assert isinstance(book_id, int) and book_id > 0
    fetched = library.get_book(book_id)
    assert fetched is not None
    assert fetched.title == "Dune"
    assert fetched.authors == ["Frank Herbert"]


def test_add_book_plays_save_tone(library):
    library.add_book(make_book())
    assert "play_save" in library.hw.calls


def test_get_book_missing_returns_none(library):
    assert library.get_book(999999) is None


def test_update_book_changes_fields(library):
    book_id = library.add_book(make_book())
    updated = make_book(title="Dune Messiah", shelf="4")
    library.update_book(book_id, updated)
    fetched = library.get_book(book_id)
    assert fetched.title == "Dune Messiah"
    assert fetched.shelf == "4"


def test_delete_book_removes_it_and_plays_delete_tone(library):
    book_id = library.add_book(make_book())
    library.delete_book(book_id)
    assert library.get_book(book_id) is None
    assert "play_delete" in library.hw.calls


# ---------------------------------------------------------------------------
# search_books / list_all_books / list_by_location / distinct_locations
# ---------------------------------------------------------------------------


def test_search_books_matches_title_and_plays_search_tone(library):
    library.add_book(make_book(title="Dune"))
    library.add_book(make_book(title="Foundation", isbn13="9780553293357"))
    results = library.search_books("Dune")
    assert len(results) == 1
    assert results[0].title == "Dune"
    assert "play_search" in library.hw.calls


def test_search_books_no_match_returns_empty_list(library):
    library.add_book(make_book())
    assert library.search_books("nonexistent keyword xyz") == []


def test_list_all_books_returns_everything(library):
    library.add_book(make_book(title="Dune"))
    library.add_book(make_book(title="Foundation", isbn13="9780553293357"))
    books = library.list_all_books()
    assert {b.title for b in books} == {"Dune", "Foundation"}


def test_list_by_location_filters_correctly(library):
    library.add_book(make_book(title="Dune", room="Living Room", shelf="3"))
    library.add_book(make_book(title="Foundation", isbn13="9780553293357", room="Office", shelf="1"))
    results = library.list_by_location(room="Living Room")
    assert len(results) == 1
    assert results[0].title == "Dune"


def test_distinct_locations_aggregates_across_books(library):
    library.add_book(make_book(title="Dune", room="Living Room", floor="1", column="A", shelf="3"))
    library.add_book(
        make_book(title="Foundation", isbn13="9780553293357", room="Office", floor="2", column="B", shelf="1")
    )
    locations = library.distinct_locations()
    assert set(locations["room"]) == {"Living Room", "Office"}
    assert set(locations["floor"]) == {"1", "2"}


# ---------------------------------------------------------------------------
# add_by_isbn / lookup_isbn (metadata contract)
# ---------------------------------------------------------------------------


def test_add_by_isbn_found_saves_and_returns_book_with_id(library, monkeypatch):
    found_book = make_book(title="Metadata Book", isbn13="9781111111111")

    class FakeMetadata:
        @staticmethod
        def fetch_by_isbn(isbn):
            assert isbn == "9781111111111"
            return found_book

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)
    result = library.add_by_isbn("9781111111111")
    assert result is not None
    assert result.id is not None
    assert result.title == "Metadata Book"
    # actually persisted, not just returned
    assert library.get_book(result.id).title == "Metadata Book"
    assert "play_save" in library.hw.calls


def test_add_by_isbn_not_found_plays_error_and_returns_none(library, monkeypatch):
    class FakeMetadata:
        @staticmethod
        def fetch_by_isbn(isbn):
            return None

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)
    result = library.add_by_isbn("0000000000000")
    assert result is None
    assert "play_error" in library.hw.calls


def test_add_by_isbn_metadata_unavailable_returns_none(library, monkeypatch):
    monkeypatch.setattr(library_mod, "metadata", None)
    result = library.add_by_isbn("9781111111111")
    assert result is None
    assert "play_error" in library.hw.calls


def test_add_by_isbn_metadata_raises_is_caught(library, monkeypatch):
    class FakeMetadata:
        @staticmethod
        def fetch_by_isbn(isbn):
            raise RuntimeError("network down")

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)
    result = library.add_by_isbn("9781111111111")
    assert result is None
    assert "play_error" in library.hw.calls


def test_lookup_isbn_does_not_save(library, monkeypatch):
    found_book = make_book(title="Preview Only", isbn13="9782222222222")

    class FakeMetadata:
        @staticmethod
        def fetch_by_isbn(isbn):
            return found_book

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)
    result = library.lookup_isbn("9782222222222")
    assert result is not None
    assert result.title == "Preview Only"
    assert library.list_all_books() == []  # nothing persisted


def test_lookup_isbn_unavailable_returns_none(library, monkeypatch):
    monkeypatch.setattr(library_mod, "metadata", None)
    assert library.lookup_isbn("9782222222222") is None


# ---------------------------------------------------------------------------
# ai_describe_search (AISearchAgent contract)
# ---------------------------------------------------------------------------


def test_ai_describe_search_returns_results_when_available(library):
    match = make_book(title="AI Found Book")
    library.ai_agent = FakeAISearchAgent(available=True, results=[match])
    results = library.ai_describe_search("a book about sand and worms")
    assert results == [match]
    assert "play_search" in library.hw.calls


def test_ai_describe_search_unavailable_returns_empty_list(library):
    library.ai_agent = FakeAISearchAgent(available=False)
    assert library.ai_describe_search("anything") == []


def test_ai_describe_search_no_agent_returns_empty_list(library):
    library.ai_agent = None
    assert library.ai_describe_search("anything") == []


def test_ai_describe_search_agent_raises_is_caught(library):
    class ExplodingAgent:
        available = True

        def describe_to_find(self, description):
            raise RuntimeError("LLM brick unreachable")

    library.ai_agent = ExplodingAgent()
    assert library.ai_describe_search("anything") == []


# ---------------------------------------------------------------------------
# process_shelf_image / confirm_shelf_candidates (ocr + metadata contract)
# ---------------------------------------------------------------------------


def test_process_shelf_image_enriches_candidates_with_resolved_matches(library, monkeypatch):
    class FakeOcr:
        @staticmethod
        def process_shelf_photo(image_bytes, llm=None):
            return [{"title": "Dune", "author": "Frank Herbert"}, {"title": "Unknown Book", "author": ""}]

    resolved_match = make_book(title="Dune", isbn13="9780441013593")

    class FakeMetadata:
        @staticmethod
        def search_by_title_author(title, author=""):
            if title == "Dune":
                return [resolved_match]
            return []

    monkeypatch.setattr(library_mod, "ocr", FakeOcr)
    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)

    candidates = library.process_shelf_image(b"fake image bytes")
    assert len(candidates) == 2
    assert candidates[0]["title"] == "Dune"
    assert candidates[0]["resolved"] is not None
    assert candidates[0]["resolved"]["title"] == "Dune"
    assert candidates[1]["title"] == "Unknown Book"
    assert candidates[1]["resolved"] is None


def test_process_shelf_image_ocr_unavailable_returns_empty_list(library, monkeypatch):
    monkeypatch.setattr(library_mod, "ocr", None)
    assert library.process_shelf_image(b"fake image bytes") == []


def test_process_shelf_image_ocr_raises_is_caught(library, monkeypatch):
    class ExplodingOcr:
        @staticmethod
        def process_shelf_photo(image_bytes, llm=None):
            raise RuntimeError("ocr_runtime unreachable")

    monkeypatch.setattr(library_mod, "ocr", ExplodingOcr)
    assert library.process_shelf_image(b"fake image bytes") == []


def test_confirm_shelf_candidates_saves_each_and_returns_ids(library):
    book_dicts = [
        book_to_dict(make_book(title="Confirmed One", isbn13="9783333333333")),
        book_to_dict(make_book(title="Confirmed Two", isbn13="9784444444444")),
    ]
    ids = library.confirm_shelf_candidates(book_dicts)
    assert len(ids) == 2
    titles = {library.get_book(i).title for i in ids}
    assert titles == {"Confirmed One", "Confirmed Two"}


def test_confirm_shelf_candidates_empty_list_returns_empty_list(library):
    assert library.confirm_shelf_candidates([]) == []


def test_confirm_shelf_candidates_ignores_unknown_fields(library):
    book_dicts = [{"title": "Weird Book", "not_a_real_field": "ignored"}]
    ids = library.confirm_shelf_candidates(book_dicts)
    assert len(ids) == 1
    assert library.get_book(ids[0]).title == "Weird Book"


# ---------------------------------------------------------------------------
# book_to_dict
# ---------------------------------------------------------------------------


def test_book_to_dict_no_cover_has_null_cover_url(library):
    book_id = library.add_book(make_book())
    book = library.get_book(book_id)
    data = book_to_dict(book)
    assert data["cover_url"] is None
    assert data["has_cover"] is False
    assert "cover_image" not in data


def test_book_to_dict_with_cover_and_id_has_cover_url():
    book = make_book()
    book.id = 42
    book.cover_image = b"fake jpeg bytes"
    book.cover_mime = "image/jpeg"
    data = book_to_dict(book)
    assert data["cover_url"] == "/api/books/42/cover"
    assert data["has_cover"] is True
    assert "cover_data_uri" not in data


def test_book_to_dict_include_cover_data_uri():
    book = make_book()
    book.cover_image = b"fake jpeg bytes"
    book.cover_mime = "image/jpeg"
    data = book_to_dict(book, include_cover_data_uri=True)
    assert data["cover_data_uri"].startswith("data:image/jpeg;base64,")


# ---------------------------------------------------------------------------
# degraded construction (no Hardware, no AISearchAgent class available at all)
# ---------------------------------------------------------------------------


def test_library_construction_without_hardware_or_ai_agent_still_works(db_path):
    lib = Library(db_name=db_path, hw=None, ai_agent=None)
    try:
        book_id = lib.add_book(make_book())  # must not raise even with hw=None
        assert lib.get_book(book_id) is not None
        assert lib.ai_describe_search("anything") == []
    finally:
        lib.close()
