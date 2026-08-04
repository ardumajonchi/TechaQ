# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""The ONE real code path for every TechaQ mutation/query. Both python/main.py (WebUI REST/WS
handlers) and python/cli.py call into this module's `Library` class exclusively -- neither surface
is ever allowed to touch `BookDB`, `Hardware`, `metadata`, `ai_search`, or `ocr` directly, so
there's exactly one place that decides what "add a book", "search", "delete", etc. actually mean.

Sibling modules may not exist on a given board, or may exist but fail to construct/run (no
network, no LLM brick, tesseract missing, etc.):
  - engine/metadata.py   -- fetch_by_isbn(isbn) -> BookRecord | None
                             search_by_title_author(title, author="") -> list[BookRecord]
  - engine/ai_search.py  -- class AISearchAgent with .available: bool and
                             .describe_to_find(description) -> list[BookRecord]
  - engine/web_lookup.py -- class WebMetadataFallback with .available: bool and
                             .lookup(isbn) -> dict of a web-search-derived {"title","author"}
                             guess (NOT resolved yet -- see _web_fallback_lookup below)
  - engine/ocr.py        -- process_shelf_photo(image_bytes, llm=None) -> list[dict] of
                             {"title":..., "author":...} candidate guesses (NOT resolved yet)
  - hw.py (python/hw.py) -- class Hardware with play_scan/play_save/play_search/play_error/
                             play_delete/play_startup, each already a no-op-on-failure per its own
                             docstring; constructing Hardware() itself can raise with no MCU/Bridge.

Every one of those is defensively imported (try/except at import time) and defensively used
(try/except around every call, in addition to whatever the callee already guards) so a missing or
broken teammate module degrades a single feature (metadata lookups return None/[], AI search
reports unavailable, OCR returns no candidates, the buzzer stays silent) without ever taking the
whole app down. This module's own methods therefore never raise for "the dependency isn't ready"
-- only for genuinely programmer-error inputs (e.g. BookDB itself being unreachable).
"""

from __future__ import annotations

import base64
import csv
import io
from dataclasses import asdict

from .db import BookDB
from .models import BookRecord
from .settings import SettingsStore

try:
    from . import metadata
except ImportError as exc:  # engine/metadata.py not written yet, or failed to import
    metadata = None
    print(f"[techaq] engine.metadata unavailable, ISBN/title lookups disabled: {exc!r}")

try:
    from .ai_search import AISearchAgent
except ImportError as exc:  # engine/ai_search.py not written yet, or failed to import
    AISearchAgent = None
    print(f"[techaq] engine.ai_search unavailable, AI describe-to-find disabled: {exc!r}")

try:
    from .web_lookup import WebMetadataFallback
except ImportError as exc:  # engine/web_lookup.py not written yet, or failed to import
    WebMetadataFallback = None
    print(f"[techaq] engine.web_lookup unavailable, web metadata fallback disabled: {exc!r}")

try:
    from . import ocr
except ImportError as exc:  # engine/ocr.py not written yet, or failed to import
    ocr = None
    print(f"[techaq] engine.ocr unavailable, shelf-photo OCR disabled: {exc!r}")

try:
    from hw import Hardware  # python/hw.py; flat import, python/ is the sys.path root (see main.py)
except ImportError as exc:
    Hardware = None
    print(f"[techaq] hw module unavailable, buzzer disabled: {exc!r}")


DB_NAME = "techaq.db"


def book_to_dict(book: BookRecord, include_cover_data_uri: bool = False) -> dict:
    """Render a BookRecord for JSON responses (REST API, OCR-candidate "resolved" field, etc).

    `cover_image` (raw bytes) is never inlined directly -- BLOBs don't belong in a JSON payload
    a browser has to parse. Saved books (book.id is not None) instead get a `cover_url` pointing
    at GET /api/books/{id}/cover (see main.py's docstring for why that's a dedicated binary route
    rather than base64-in-JSON). Books with no id yet (e.g. an OCR/metadata candidate the user
    hasn't confirmed/saved) have no URL to hang a cover off, so when `include_cover_data_uri` is
    set, the cover is instead inlined as a ready-to-use `data:` URI for an <img src=...>.
    """
    data = asdict(book)
    cover_bytes = data.pop("cover_image", None)
    data["has_cover"] = bool(cover_bytes)
    data["cover_url"] = f"/api/books/{book.id}/cover" if (book.id is not None and cover_bytes) else None
    if include_cover_data_uri and cover_bytes:
        mime = book.cover_mime or "image/jpeg"
        data["cover_data_uri"] = f"data:{mime};base64,{base64.b64encode(cover_bytes).decode('ascii')}"
    return data


class Library:
    """Owns one BookDB, one (optional) Hardware, and one (optional) AISearchAgent. Every method
    is safe to call even when a dependency never came up -- callers don't need to check anything
    first, they just get an empty/None result for the degraded feature.
    """

    def __init__(
        self,
        db_name: str | None = None,
        db: BookDB | None = None,
        hw=None,
        ai_agent=None,
        web_fallback=None,
    ):
        self.db = db if db is not None else BookDB(db_name or DB_NAME)
        self.settings = SettingsStore(db_name or DB_NAME)

        if hw is not None:
            self.hw = hw
        elif Hardware is not None:
            try:
                self.hw = Hardware()
            except Exception as exc:
                print(f"[techaq] Hardware init failed, running without MCU/Bridge: {exc!r}")
                self.hw = None
        else:
            self.hw = None

        if ai_agent is not None:
            self.ai_agent = ai_agent
        elif AISearchAgent is not None:
            try:
                self.ai_agent = AISearchAgent()
            except Exception as exc:
                print(f"[techaq] AISearchAgent init failed, AI search disabled: {exc!r}")
                self.ai_agent = None
        else:
            self.ai_agent = None

        if web_fallback is not None:
            self.web_fallback = web_fallback
        elif WebMetadataFallback is not None:
            try:
                self.web_fallback = WebMetadataFallback()
            except Exception as exc:
                print(f"[techaq] WebMetadataFallback init failed, web metadata fallback disabled: {exc!r}")
                self.web_fallback = None
        else:
            self.web_fallback = None

        self.metadata_available = metadata is not None
        self.ocr_available = ocr is not None

    def close(self) -> None:
        self.db.stop()
        self.settings.stop()

    # -- settings -------------------------------------------------------------------------------

    def get_settings(self) -> dict:
        return self.settings.get()

    def update_settings(self, partial: dict) -> dict:
        return self.settings.update(partial)

    # -- buzzer -------------------------------------------------------------------------------

    def _buzz(self, method_name: str) -> None:
        if self.hw is None:
            return
        try:
            getattr(self.hw, method_name)()
        except Exception as exc:
            print(f"[techaq] hw.{method_name}() failed, ignoring: {exc!r}")

    def notify_scan_received(self) -> None:
        """Called the instant a scan code is received (before lookup/save), independent of
        whether the lookup succeeds -- gives the user audible feedback that the scanner worked."""
        self._buzz("play_scan")

    def notify_startup(self) -> None:
        """Called once from main.py right after construction, so the board beeps to confirm the
        app process (and its MCU/Bridge link) came up -- mirrors progq's/scummvm-q's hw.play_startup()."""
        self._buzz("play_startup")

    # -- CRUD -----------------------------------------------------------------------------------

    def add_book(self, book: BookRecord) -> int:
        book_id = self.db.insert(book)
        self._buzz("play_save")
        return book_id

    def add_by_isbn(self, isbn: str) -> BookRecord | None:
        if metadata is None:
            self._buzz("play_error")
            return None
        try:
            book = metadata.fetch_by_isbn(isbn, include_description=self.settings.get()["fetch_synopsis_default"])
        except Exception as exc:
            print(f"[techaq] metadata.fetch_by_isbn({isbn!r}) failed: {exc!r}")
            book = None
        if book is None:
            book = self._web_fallback_lookup(isbn)
        if book is None:
            self._buzz("play_error")
            return None
        book_id = self.add_book(book)
        book.id = book_id
        return book

    def lookup_isbn(self, isbn: str) -> BookRecord | None:
        """Look up only, never saves -- used by POST /api/lookup/{isbn} for a scan-preview UX."""
        if metadata is None:
            return None
        try:
            book = metadata.fetch_by_isbn(isbn, include_description=self.settings.get()["fetch_synopsis_default"])
        except Exception as exc:
            print(f"[techaq] metadata.fetch_by_isbn({isbn!r}) failed: {exc!r}")
            book = None
        if book is None:
            book = self._web_fallback_lookup(isbn)
        return book

    def _web_fallback_lookup(self, isbn: str) -> BookRecord | None:
        """Last-resort step when every real catalog source in metadata.fetch_by_isbn missed:
        ask WebMetadataFallback for a web-search-derived title/author guess, then resolve that
        guess against metadata.search_by_title_author()'s real catalog search -- the guess is
        never treated as a match on its own, only whatever that search actually finds. Returns
        None if the fallback is unavailable, the scrape/LLM found nothing usable, or the guess
        doesn't resolve to a real search hit. Never raises.
        """
        if self.web_fallback is None or not getattr(self.web_fallback, "available", False):
            return None
        clean = metadata._clean_isbn(isbn) if metadata is not None else isbn
        try:
            guess = self.web_fallback.lookup(clean) or {}
        except Exception as exc:
            print(f"[techaq] WebMetadataFallback.lookup({isbn!r}) failed: {exc!r}")
            return None
        title = guess.get("title", "")
        if not title or metadata is None:
            return None
        try:
            matches = metadata.search_by_title_author(title, guess.get("author", ""))
        except Exception as exc:
            print(f"[techaq] metadata.search_by_title_author fallback lookup failed: {exc!r}")
            matches = []
        if not matches:
            return None
        match = matches[0]
        # The matched edition's own ISBN isn't the barcode on the user's physical copy -- keep
        # the originally-requested one so CSV-import/duplicate-detection stays consistent.
        match.isbn13, match.isbn10 = "", ""
        if len(clean) == 13:
            match.isbn13 = clean
        elif len(clean) == 10:
            match.isbn10 = clean
        else:
            match.isbn13 = clean
        match.source = f"websearch+{match.source}"
        return match

    def fetch_synopsis(self, isbn: str) -> str:
        """Fetch only the synopsis for an ISBN, for the manual "fetch synopsis" button -- used
        when the default lookup skipped it (see settings.fetch_synopsis_default). Degrades to ""
        if metadata is unavailable or the fetch fails, never raises."""
        if metadata is None:
            return ""
        try:
            return metadata.fetch_description(isbn)
        except Exception as exc:
            print(f"[techaq] metadata.fetch_description({isbn!r}) failed: {exc!r}")
            return ""

    def update_book(self, book_id: int, book: BookRecord) -> None:
        """Edits never carry cover bytes (there's no photo-upload UI for an edit, see main.py's
        module docstring), so preserve whatever cover the record already had rather than wiping
        it every time a user edits an unrelated field like shelf location."""
        if not book.cover_image:
            existing = self.db.get(book_id)
            if existing is not None:
                book.cover_image = existing.cover_image
                book.cover_mime = existing.cover_mime
        self.db.update(book_id, book)

    def delete_book(self, book_id: int) -> None:
        self.db.delete(book_id)
        self._buzz("play_delete")

    def get_book(self, book_id: int) -> BookRecord | None:
        return self.db.get(book_id)

    def list_all_books(self, order_by: str = "updated_at DESC") -> list[BookRecord]:
        return self.db.list_all(order_by=order_by)

    def search_books(self, keyword: str) -> list[BookRecord]:
        self._buzz("play_search")
        return self.db.search(keyword)

    def list_by_location(
        self, room: str = "", floor: str = "", column: str = "", shelf: str = ""
    ) -> list[BookRecord]:
        return self.db.filter_by_location(room=room, floor=floor, column=column, shelf=shelf)

    def distinct_locations(self) -> dict[str, list[str]]:
        return self.db.distinct_locations()

    # -- AI describe-to-find ---------------------------------------------------------------------

    def ai_describe_search(self, description: str) -> list[BookRecord]:
        self._buzz("play_search")
        if self.ai_agent is None or not getattr(self.ai_agent, "available", False):
            return []
        try:
            return self.ai_agent.describe_to_find(description) or []
        except Exception as exc:
            print(f"[techaq] AISearchAgent.describe_to_find failed: {exc!r}")
            return []

    # -- shelf photo OCR ---------------------------------------------------------------------

    def process_shelf_image(self, image_bytes: bytes) -> list[dict]:
        """Run OCR to get title/author guesses, then resolve each guess against metadata for a
        real BookRecord match. Nothing is saved here -- this is strictly "show the user candidates
        to confirm"; saving happens via confirm_shelf_candidates() once the user picks which ones.
        """
        if ocr is None:
            return []
        try:
            candidates = ocr.process_shelf_photo(image_bytes) or []
        except Exception as exc:
            print(f"[techaq] ocr.process_shelf_photo failed: {exc!r}")
            return []

        enriched = []
        for candidate in candidates:
            title = (candidate or {}).get("title", "") or ""
            author = (candidate or {}).get("author", "") or ""
            resolved = None
            if metadata is not None and (title or author):
                try:
                    matches = metadata.search_by_title_author(title, author)
                except Exception as exc:
                    print(f"[techaq] metadata.search_by_title_author({title!r}, {author!r}) failed: {exc!r}")
                    matches = []
                if matches:
                    resolved = book_to_dict(matches[0], include_cover_data_uri=True)
            enriched.append({**candidate, "resolved": resolved})
        return enriched

    def confirm_shelf_candidates(self, book_dicts: list[dict]) -> list[int]:
        """Save the subset of shelf-photo candidates the user confirmed in the browser. Each dict
        is expected to match BookRecord field names (as produced by book_to_dict / the frontend's
        editable candidate form) -- unknown keys are ignored, missing ones fall back to defaults.
        """
        known_fields = set(BookRecord.__dataclass_fields__) - {"id", "created_at", "updated_at"}
        ids = []
        for data in book_dicts or []:
            data = dict(data or {})
            cover_data_uri = data.pop("cover_data_uri", None)
            values = {f: data[f] for f in known_fields if f in data and f != "cover_image"}
            book = BookRecord(**values)
            if cover_data_uri and isinstance(cover_data_uri, str) and "," in cover_data_uri:
                try:
                    book.cover_image = base64.b64decode(cover_data_uri.split(",", 1)[1])
                except Exception as exc:
                    print(f"[techaq] failed to decode cover_data_uri while confirming candidate: {exc!r}")
            ids.append(self.add_book(book))
        return ids

    # -- photo-to-ISBN OCR -------------------------------------------------------------------

    def scan_isbn_photo(self, image_bytes: bytes) -> list[str]:
        """Run OCR over a photo (of a barcode's printed digits, a book cover, etc.) and return
        plausible ISBN-13/ISBN-10 digit-sequence candidates for the user to pick from/edit before
        looking one up -- nothing is looked up or saved here. Degrades to [] if ocr_runtime is
        unavailable/unreachable or the image is malformed, same as process_shelf_image().
        """
        if ocr is None:
            return []
        try:
            return ocr.process_isbn_photo(image_bytes) or []
        except Exception as exc:
            print(f"[techaq] ocr.process_isbn_photo failed: {exc!r}")
            return []

    # -- CSV import/export --------------------------------------------------------------------

    def import_csv(self, csv_text: str) -> dict:
        """Parse a CSV export (or any CSV with matching column headers) and add each row as a new
        book (via db.insert() directly, not add_book(), so a multi-row import plays one summary
        buzz rather than one per row). A row whose isbn13/isbn10 already matches a book in the
        library is skipped (counted in "skipped") rather than added, to avoid piling up duplicates
        on a re-import; rows with no ISBN at all always get added, since there's nothing to dedupe
        against. A row that fails to parse into a BookRecord is counted in "errors" and otherwise
        ignored -- never raises.
        """
        known_fields = set(BookRecord.__dataclass_fields__) - {"id", "created_at", "updated_at", "cover_image"}
        added = 0
        skipped = 0
        errors: list[str] = []

        try:
            reader = csv.DictReader(io.StringIO(csv_text))
            rows = list(reader)
        except Exception as exc:
            return {"added": 0, "skipped": 0, "errors": [f"failed to parse CSV: {exc!r}"]}

        for i, row in enumerate(rows):
            try:
                values = {}
                for field in known_fields:
                    raw = (row.get(field) or "").strip()
                    if field in ("authors", "categories"):
                        values[field] = [v.strip() for v in raw.split(";") if v.strip()] if raw else []
                    elif field == "page_count":
                        values[field] = int(raw) if raw else None
                    else:
                        values[field] = raw
                book = BookRecord(**values)
            except Exception as exc:
                errors.append(f"row {i + 2}: {exc!r}")
                continue

            isbn = book.isbn13 or book.isbn10
            if isbn and self.db.get_by_isbn(isbn) is not None:
                skipped += 1
                continue

            self.db.insert(book)
            added += 1

        if added:
            self._buzz("play_save")
        return {"added": added, "skipped": skipped, "errors": errors}


def create_library(db_name: str | None = None) -> Library:
    """Factory used by both main.py and cli.py so neither hand-rolls construction differently."""
    return Library(db_name=db_name)
