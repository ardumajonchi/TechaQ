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

from arduino.app_bricks.dbstorage_sqlstore import SQLStore


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


def test_update_book_preserves_existing_cover_when_edit_carries_none(library):
    """The WebUI edit form never carries cover bytes (no photo-upload UI for edits, see
    main.py's module docstring) -- editing an unrelated field like shelf location must not wipe
    a cover fetched earlier by add_by_isbn/manual entry."""
    book_id = library.add_book(make_book(cover_image=b"fake-jpeg-bytes", cover_mime="image/jpeg"))
    edited = make_book(shelf="4", cover_image=None, cover_mime="")
    library.update_book(book_id, edited)
    fetched = library.get_book(book_id)
    assert fetched.shelf == "4"
    assert fetched.cover_image == b"fake-jpeg-bytes"
    assert fetched.cover_mime == "image/jpeg"


def test_update_book_overwrites_cover_when_edit_carries_one(library):
    book_id = library.add_book(make_book(cover_image=b"old-cover", cover_mime="image/jpeg"))
    edited = make_book(cover_image=b"new-cover", cover_mime="image/png")
    library.update_book(book_id, edited)
    fetched = library.get_book(book_id)
    assert fetched.cover_image == b"new-cover"
    assert fetched.cover_mime == "image/png"


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
        def fetch_by_isbn(isbn, include_description=False):
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
        def fetch_by_isbn(isbn, include_description=False):
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
        def fetch_by_isbn(isbn, include_description=False):
            raise RuntimeError("network down")

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)
    result = library.add_by_isbn("9781111111111")
    assert result is None
    assert "play_error" in library.hw.calls


def test_lookup_isbn_does_not_save(library, monkeypatch):
    found_book = make_book(title="Preview Only", isbn13="9782222222222")

    class FakeMetadata:
        @staticmethod
        def fetch_by_isbn(isbn, include_description=False):
            return found_book

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)
    result = library.lookup_isbn("9782222222222")
    assert result is not None
    assert result.title == "Preview Only"
    assert library.list_all_books() == []  # nothing persisted


def test_lookup_isbn_unavailable_returns_none(library, monkeypatch):
    monkeypatch.setattr(library_mod, "metadata", None)
    assert library.lookup_isbn("9782222222222") is None


def test_add_by_isbn_passes_settings_fetch_synopsis_default(library, monkeypatch):
    seen = {}

    class FakeMetadata:
        @staticmethod
        def fetch_by_isbn(isbn, include_description=False):
            seen["include_description"] = include_description
            return make_book(title="Book", isbn13=isbn)

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)

    library.add_by_isbn("9781111111111")
    assert seen["include_description"] is False

    library.update_settings({"fetch_synopsis_default": True})
    library.add_by_isbn("9781111111111")
    assert seen["include_description"] is True


def test_lookup_isbn_passes_settings_fetch_synopsis_default(library, monkeypatch):
    seen = {}

    class FakeMetadata:
        @staticmethod
        def fetch_by_isbn(isbn, include_description=False):
            seen["include_description"] = include_description
            return make_book(title="Book", isbn13=isbn)

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)

    library.update_settings({"fetch_synopsis_default": True})
    library.lookup_isbn("9782222222222")
    assert seen["include_description"] is True


# ---------------------------------------------------------------------------
# lookup_isbn's optional on_status callback (live per-source UI progress) --
# must be entirely opt-in: a caller (or test double) that never asks for it
# should see identical behavior to before this callback existed, which is
# exactly what every FakeMetadata/FakeMetadataMiss above -- none of which
# declare `on_source_done` or `SOURCE_NAMES` -- already exercises implicitly.
# ---------------------------------------------------------------------------


def test_lookup_isbn_emits_checking_and_source_done_phases(library, monkeypatch):
    class FakeMetadata:
        SOURCE_NAMES = ("openlibrary", "googlebooks", "dnb", "bnf")

        @staticmethod
        def fetch_by_isbn(isbn, include_description=False, on_source_done=None):
            for name in FakeMetadata.SOURCE_NAMES:
                if on_source_done is not None:
                    on_source_done(name, name != "bnf")  # every source hits except bnf
            return make_book(title="Checked Book", isbn13=isbn)

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)

    events = []
    result = library.lookup_isbn("9782222222222", on_status=lambda phase, data: events.append((phase, data)))

    assert result is not None
    assert result.title == "Checked Book"
    assert events[0] == ("checking", {"sources": ["openlibrary", "googlebooks", "dnb", "bnf"]})
    source_done_events = events[1:]
    assert [data["source"] for _, data in source_done_events] == ["openlibrary", "googlebooks", "dnb", "bnf"]
    assert [data["found"] for _, data in source_done_events] == [True, True, True, False]


def test_lookup_isbn_emits_web_fallback_phase_when_metadata_misses(library, monkeypatch):
    class FakeMetadata:
        SOURCE_NAMES = ("openlibrary", "googlebooks", "dnb", "bnf")

        @staticmethod
        def fetch_by_isbn(isbn, include_description=False, on_source_done=None):
            for name in FakeMetadata.SOURCE_NAMES:
                if on_source_done is not None:
                    on_source_done(name, False)
            return None

        @staticmethod
        def search_by_title_author(title, author=""):
            return []

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)
    library.web_fallback = FakeWebFallback(available=False)

    events = []
    result = library.lookup_isbn("9782222222222", on_status=lambda phase, data: events.append((phase, data)))

    assert result is None
    assert [phase for phase, _ in events][:1] == ["checking"]
    assert ("web_fallback", {}) in events


def test_lookup_isbn_broken_on_status_callback_does_not_break_lookup(library, monkeypatch):
    class FakeMetadata:
        SOURCE_NAMES = ("openlibrary", "googlebooks", "dnb", "bnf")

        @staticmethod
        def fetch_by_isbn(isbn, include_description=False, on_source_done=None):
            if on_source_done is not None:
                on_source_done("openlibrary", True)
            return make_book(title="Still Works", isbn13=isbn)

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)

    def broken_on_status(phase, data):
        raise RuntimeError("frontend socket blew up")

    result = library.lookup_isbn("9782222222222", on_status=broken_on_status)
    assert result is not None
    assert result.title == "Still Works"


def test_lookup_isbn_without_on_status_never_touches_source_names(library, monkeypatch):
    """A FakeMetadata that doesn't define SOURCE_NAMES/on_source_done at all (matching every
    plain fetch_by_isbn(isbn, include_description=...) double elsewhere in this file) must still
    work when on_status is omitted -- lookup_isbn should never dereference metadata.SOURCE_NAMES
    or pass on_source_done unless a caller actually asked for status updates."""

    class FakeMetadata:
        @staticmethod
        def fetch_by_isbn(isbn, include_description=False):
            return make_book(title="No Callback Needed", isbn13=isbn)

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)
    result = library.lookup_isbn("9782222222222")
    assert result is not None
    assert result.title == "No Callback Needed"


# ---------------------------------------------------------------------------
# add_by_isbn / lookup_isbn web-search fallback (last-resort step when
# metadata.fetch_by_isbn misses every real catalog source)
# ---------------------------------------------------------------------------


class FakeWebFallback:
    def __init__(self, available=True, guess=None):
        self.available = available
        self._guess = {} if guess is None else guess
        self.lookup_calls = []

    def lookup(self, isbn):
        self.lookup_calls.append(isbn)
        return self._guess


class FakeMetadataMiss:
    """fetch_by_isbn always misses (like every real source struck out), so add_by_isbn/
    lookup_isbn fall through to web_fallback; search_by_title_author is the grounding step
    the web guess must resolve against before ever counting as a match."""

    resolved_matches: list = []
    search_calls: list = []

    @staticmethod
    def fetch_by_isbn(isbn, include_description=False):
        return None

    @classmethod
    def search_by_title_author(cls, title, author=""):
        cls.search_calls.append((title, author))
        return cls.resolved_matches

    @staticmethod
    def _clean_isbn(isbn):
        return "".join(c for c in isbn if c.isdigit() or c.upper() == "X")


def test_add_by_isbn_falls_back_to_web_search_when_metadata_misses(library, monkeypatch):
    FakeMetadataMiss.resolved_matches = [make_book(title="Dune", isbn13="9999999999999", source="openlibrary")]
    FakeMetadataMiss.search_calls = []
    monkeypatch.setattr(library_mod, "metadata", FakeMetadataMiss)
    library.web_fallback = FakeWebFallback(available=True, guess={"title": "Dune", "author": "Frank Herbert"})

    result = library.add_by_isbn("9780441172719")
    assert result is not None
    assert result.id is not None
    assert result.title == "Dune"
    # matched edition's own ISBN is discarded -- the originally-requested one is kept
    assert result.isbn13 == "9780441172719"
    assert result.source == "websearch+openlibrary"
    assert FakeMetadataMiss.search_calls == [("Dune", "Frank Herbert")]
    assert "play_save" in library.hw.calls


def test_lookup_isbn_falls_back_to_web_search_when_metadata_misses(library, monkeypatch):
    FakeMetadataMiss.resolved_matches = [make_book(title="Dune", isbn13="9999999999999", source="openlibrary")]
    FakeMetadataMiss.search_calls = []
    monkeypatch.setattr(library_mod, "metadata", FakeMetadataMiss)
    library.web_fallback = FakeWebFallback(available=True, guess={"title": "Dune", "author": "Frank Herbert"})

    result = library.lookup_isbn("9780441172719")
    assert result is not None
    assert result.title == "Dune"
    assert result.isbn13 == "9780441172719"
    assert result.source == "websearch+openlibrary"
    assert library.list_all_books() == []  # lookup_isbn never saves


def test_add_by_isbn_web_fallback_no_guess_returns_none(library, monkeypatch):
    FakeMetadataMiss.search_calls = []
    monkeypatch.setattr(library_mod, "metadata", FakeMetadataMiss)
    library.web_fallback = FakeWebFallback(available=True, guess={})  # scrape/LLM found nothing

    result = library.add_by_isbn("9780441172719")
    assert result is None
    assert "play_error" in library.hw.calls
    assert FakeMetadataMiss.search_calls == []  # never even attempted a grounding search


def test_add_by_isbn_web_fallback_unavailable_returns_none(library, monkeypatch):
    monkeypatch.setattr(library_mod, "metadata", FakeMetadataMiss)
    fallback = FakeWebFallback(available=False, guess={"title": "Dune", "author": "Frank Herbert"})
    library.web_fallback = fallback

    result = library.add_by_isbn("9780441172719")
    assert result is None
    assert "play_error" in library.hw.calls
    assert fallback.lookup_calls == []  # unavailable fallback is never even called


def test_add_by_isbn_web_fallback_guess_does_not_resolve_returns_none(library, monkeypatch):
    FakeMetadataMiss.resolved_matches = []  # guess doesn't resolve to any real catalog hit
    FakeMetadataMiss.search_calls = []
    monkeypatch.setattr(library_mod, "metadata", FakeMetadataMiss)
    library.web_fallback = FakeWebFallback(available=True, guess={"title": "Nonexistent Book", "author": ""})

    result = library.add_by_isbn("9780441172719")
    assert result is None
    assert "play_error" in library.hw.calls


def test_add_by_isbn_web_fallback_not_consulted_when_metadata_succeeds(library, monkeypatch):
    found_book = make_book(title="Metadata Book", isbn13="9781111111111")

    class FakeMetadata:
        @staticmethod
        def fetch_by_isbn(isbn, include_description=False):
            return found_book

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)
    fallback = FakeWebFallback(available=True, guess={"title": "Should Not Be Used"})
    library.web_fallback = fallback

    result = library.add_by_isbn("9781111111111")
    assert result.title == "Metadata Book"
    assert fallback.lookup_calls == []


def test_add_by_isbn_web_fallback_none_leaves_existing_behavior_unchanged(library, monkeypatch):
    monkeypatch.setattr(library_mod, "metadata", FakeMetadataMiss)
    library.web_fallback = None

    result = library.add_by_isbn("9780441172719")
    assert result is None
    assert "play_error" in library.hw.calls


# ---------------------------------------------------------------------------
# settings (get_settings / update_settings)
# ---------------------------------------------------------------------------


def test_get_settings_returns_defaults(library):
    settings = library.get_settings()
    assert settings == {
        "fetch_synopsis_default": False,
        "ui_language": "en",
        "ui_theme": "dark",
    }


def test_update_settings_persists_partial_change(library):
    result = library.update_settings({"ui_theme": "light"})
    assert result["ui_theme"] == "light"
    assert result["ui_language"] == "en"  # untouched
    assert library.get_settings()["ui_theme"] == "light"


def test_update_settings_rejects_unsupported_value(library):
    with pytest.raises(ValueError):
        library.update_settings({"ui_language": "klingon"})


# ---------------------------------------------------------------------------
# fetch_synopsis (manual synopsis-fetch button contract)
# ---------------------------------------------------------------------------


def test_fetch_synopsis_delegates_to_metadata(library, monkeypatch):
    class FakeMetadata:
        @staticmethod
        def fetch_description(isbn):
            assert isbn == "9781111111111"
            return "A gripping tale."

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)
    assert library.fetch_synopsis("9781111111111") == "A gripping tale."


def test_fetch_synopsis_metadata_unavailable_returns_empty_string(library, monkeypatch):
    monkeypatch.setattr(library_mod, "metadata", None)
    assert library.fetch_synopsis("9781111111111") == ""


def test_fetch_synopsis_metadata_raises_is_caught(library, monkeypatch):
    class FakeMetadata:
        @staticmethod
        def fetch_description(isbn):
            raise RuntimeError("network down")

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)
    assert library.fetch_synopsis("9781111111111") == ""


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
# scan_isbn_photo (ocr contract)
# ---------------------------------------------------------------------------


def test_scan_isbn_photo_returns_candidates_from_ocr(library, monkeypatch):
    class FakeOcr:
        @staticmethod
        def process_isbn_photo(image_bytes):
            return ["9780134685991", "0134685997"]

    monkeypatch.setattr(library_mod, "ocr", FakeOcr)
    result = library.scan_isbn_photo(b"fake image bytes")
    assert result == ["9780134685991", "0134685997"]


def test_scan_isbn_photo_ocr_unavailable_returns_empty_list(library, monkeypatch):
    monkeypatch.setattr(library_mod, "ocr", None)
    assert library.scan_isbn_photo(b"fake image bytes") == []


def test_scan_isbn_photo_ocr_raises_is_caught(library, monkeypatch):
    class ExplodingOcr:
        @staticmethod
        def process_isbn_photo(image_bytes):
            raise RuntimeError("ocr_runtime unreachable")

    monkeypatch.setattr(library_mod, "ocr", ExplodingOcr)
    assert library.scan_isbn_photo(b"fake image bytes") == []


# ---------------------------------------------------------------------------
# import_csv
# ---------------------------------------------------------------------------


def test_import_csv_adds_valid_rows(library):
    csv_text = (
        "title,authors,isbn13,room,shelf\n"
        "Dune,Frank Herbert,9780441013593,Living Room,3\n"
        "Foundation,Isaac Asimov,9780553293357,Office,1\n"
    )
    result = library.import_csv(csv_text)
    assert result == {"added": 2, "skipped": 0, "errors": []}
    titles = {b.title for b in library.list_all_books()}
    assert titles == {"Dune", "Foundation"}


def test_import_csv_skips_row_with_existing_isbn(library):
    library.add_book(make_book(title="Dune", isbn13="9780441013593"))
    csv_text = "title,authors,isbn13\nDune (dup),Frank Herbert,9780441013593\n"
    result = library.import_csv(csv_text)
    assert result == {"added": 0, "skipped": 1, "errors": []}
    assert len(library.list_all_books()) == 1


def test_import_csv_row_with_no_isbn_always_added(library):
    csv_text = "title,authors,isbn13\nNo ISBN Book,Some Author,\n"
    result = library.import_csv(csv_text)
    assert result == {"added": 1, "skipped": 0, "errors": []}


def test_import_csv_malformed_row_counted_as_error(library):
    csv_text = "title,page_count\nBad Page Count,not-a-number\n"
    result = library.import_csv(csv_text)
    assert result["added"] == 0
    assert result["skipped"] == 0
    assert len(result["errors"]) == 1


def test_import_csv_empty_string_returns_zero_counts(library):
    assert library.import_csv("") == {"added": 0, "skipped": 0, "errors": []}


def test_import_csv_header_only_returns_zero_counts(library):
    assert library.import_csv("title,authors,isbn13\n") == {"added": 0, "skipped": 0, "errors": []}


def test_import_csv_plays_save_tone_when_rows_added(library):
    library.import_csv("title,isbn13\nSome Book,9781111111111\n")
    assert "play_save" in library.hw.calls


def test_import_csv_semicolon_joined_authors_and_categories(library):
    csv_text = "title,authors,categories\nMulti Author Book,Author One;Author Two,Fiction;Adventure\n"
    library.import_csv(csv_text)
    book = library.list_all_books()[0]
    assert book.authors == ["Author One", "Author Two"]
    assert book.categories == ["Fiction", "Adventure"]


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


# ---------------------------------------------------------------------------
# is_read / in_reading_list / is_favorite boolean fields
# ---------------------------------------------------------------------------


def test_boolean_fields_default_false_and_round_trip(library):
    book_id = library.add_book(make_book())
    book = library.get_book(book_id)
    assert book.is_read is False
    assert book.in_reading_list is False
    assert book.is_favorite is False


def test_boolean_fields_persist_true_through_add_and_update(library):
    book_id = library.add_book(make_book(is_read=True, in_reading_list=True, is_favorite=True))
    book = library.get_book(book_id)
    assert book.is_read is True
    assert book.in_reading_list is True
    assert book.is_favorite is True

    book.is_favorite = False
    library.update_book(book_id, book)
    updated = library.get_book(book_id)
    assert updated.is_favorite is False
    assert updated.is_read is True  # untouched fields survive the update


def test_ensure_columns_migrates_a_pre_existing_table_missing_the_new_columns(db_path):
    """Simulate an on-device DB created before is_read/in_reading_list/is_favorite existed --
    BookDB's _ensure_columns() migration must add them without dropping existing data."""
    from engine.db import TABLE
    old_schema = {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "isbn13": "TEXT",
        "title": "TEXT",
        "authors": "TEXT",
        "created_at": "TEXT",
        "updated_at": "TEXT",
    }
    old_store = SQLStore(db_path)
    old_store.start()
    old_store.create_table(TABLE, old_schema)
    old_store.execute_sql(
        f"INSERT INTO {TABLE} (isbn13, title, authors) VALUES (?, ?, ?)",
        ("9780441013593", "Dune", "[]"),
    )
    old_store.stop()

    db = BookDB(db_path)
    try:
        books = db.list_all()
        assert len(books) == 1
        assert books[0].title == "Dune"  # pre-existing row survived the migration
        assert books[0].is_read is False
        assert books[0].is_favorite is False
    finally:
        db.stop()


def test_import_csv_coerces_boolean_columns(library):
    csv_text = (
        "title,is_read,in_reading_list,is_favorite\n"
        "Read And Favorite,true,0,1\n"
        "Neither,false,,\n"
    )
    library.import_csv(csv_text)
    books = {b.title: b for b in library.list_all_books()}
    assert books["Read And Favorite"].is_read is True
    assert books["Read And Favorite"].in_reading_list is False
    assert books["Read And Favorite"].is_favorite is True
    assert books["Neither"].is_read is False
    assert books["Neither"].is_favorite is False


# ---------------------------------------------------------------------------
# list_favorites / "Desert Island"
# ---------------------------------------------------------------------------


def test_list_favorites_returns_only_favorited_books(library):
    library.add_book(make_book(title="Favorite One", isbn13="9781111111111", is_favorite=True))
    library.add_book(make_book(title="Not Favorite", isbn13="9782222222222", is_favorite=False))
    library.add_book(make_book(title="Favorite Two", isbn13="9783333333333", is_favorite=True))

    favorites = library.list_favorites()
    assert {b.title for b in favorites} == {"Favorite One", "Favorite Two"}


def test_list_favorites_empty_when_none_favorited(library):
    library.add_book(make_book())
    assert library.list_favorites() == []


# ---------------------------------------------------------------------------
# search_add (search-by-title/author add flow)
# ---------------------------------------------------------------------------


def test_search_add_delegates_to_metadata_search_by_title_author(library, monkeypatch):
    found = [make_book(title="Dune", isbn13="9780441013593")]

    class FakeMetadata:
        @staticmethod
        def search_by_title_author(title, author=""):
            assert title == "Dune"
            assert author == "Frank Herbert"
            return found

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)
    results = library.search_add("Dune", "Frank Herbert")
    assert results == found


def test_search_add_metadata_unavailable_returns_empty_list(library, monkeypatch):
    monkeypatch.setattr(library_mod, "metadata", None)
    assert library.search_add("Dune", "Frank Herbert") == []


def test_search_add_blank_query_returns_empty_list(library, monkeypatch):
    class FakeMetadata:
        @staticmethod
        def search_by_title_author(title, author=""):
            raise AssertionError("should never be called for a blank query")

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)
    assert library.search_add("", "") == []


def test_search_add_metadata_raises_is_caught(library, monkeypatch):
    class FakeMetadata:
        @staticmethod
        def search_by_title_author(title, author=""):
            raise RuntimeError("boom")

    monkeypatch.setattr(library_mod, "metadata", FakeMetadata)
    assert library.search_add("Dune", "") == []
