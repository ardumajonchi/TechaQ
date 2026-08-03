# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Orchestrator: wires `engine.library.Library` (the one real code path -- see library.py's
docstring) to the arduino:web_ui Brick, and to the `/api/scan` endpoint that host/scanner_reader.py
(a separate process running on the host, outside this container, watching a USB/Bluetooth HID
barcode scanner) POSTs to whenever it reads a barcode.

REST API (all bodies/responses are JSON except the cover-image route, see below):
  GET    /api/books                    -- list all books, newest-updated first.
                                            Optional query params (checked in this precedence
                                            order, first match wins -- a request should pass at
                                            most one filter style):
                                              ?q=<keyword>                        -- BookDB.search
                                              ?room=&floor=&column=&shelf=        -- BookDB.filter_by_location
                                            (any subset of the four location params may be given)
  GET    /api/books/{book_id}          -- one book, 404-shaped {"error": "not found"} if missing.
  POST   /api/books                    -- manual add. Body: BookIn (see below), same field names
                                            as BookRecord minus id/created_at/updated_at/cover_image
                                            (cover bytes aren't accepted over this JSON route --
                                            there's no photo-upload UI for a manual add's cover in
                                            this iteration). Returns the saved book.
  PUT    /api/books/{book_id}          -- edit. Body: BookIn. Returns the updated book.
  DELETE /api/books/{book_id}          -- delete. Returns {"ok": true}.
  POST   /api/scan                     -- scanner_reader.py's integration point. Body:
                                            {"code": "<digits>", "device": "<name>"}. Treats `code`
                                            as an ISBN/EAN, looks it up, and auto-saves on a hit
                                            (the exact same Library.add_by_isbn() path a manual
                                            "look up + save" button would use). Always also
                                            broadcasts a "scan_event" WebSocket message (see
                                            below) so any open browser tab can toast it, whether
                                            or not the lookup succeeded. Returns
                                            {"ok": true, "book": {...}} or
                                            {"ok": false, "reason": "not_found"}.
  POST   /api/lookup/{isbn}            -- look up an ISBN WITHOUT saving (scan/manual "preview
                                            before you commit" UX). Returns
                                            {"found": true, "book": {...}} or {"found": false}.
  POST   /api/ai_search                -- body {"description": "..."}. Returns
                                            {"available": bool, "results": [...]} -- `available`
                                            mirrors AISearchAgent.available so the frontend can
                                            show "AI unavailable" instead of an empty-results state.
  POST   /api/shelf_photo               -- body {"image_b64": "<base64-encoded image bytes, no
                                            data: URI prefix>"}. Runs OCR + metadata resolution,
                                            returns {"candidates": [...]} -- nothing is saved yet.
                                            (Why base64-JSON and not multipart/raw bytes: this
                                            brick version's expose_api() is undocumented for
                                            multipart bodies, whereas base64-in-JSON is exactly
                                            what this workspace's other WebUI apps already do for
                                            image payloads, e.g. progq's/code-detector's frame
                                            encoding -- see main.py docstring's "Cover images"
                                            note below for the one place we chose differently.)
  POST   /api/shelf_photo/confirm      -- body {"books": [{...}, ...]} -- the subset of candidates
                                            (each dict shaped like a book_to_dict() result, edited
                                            client-side) the user picked "yes, save this" on.
                                            Returns {"ids": [...]}.
  GET    /api/locations                -- {"room": [...], "floor": [...], "column": [...],
                                            "shelf": [...]} distinct values, for autocomplete.
  GET    /api/books/{book_id}/cover    -- raw image bytes with the book's real cover_mime as
                                            Content-Type (404 if the book has no cover). This is
                                            the one route NOT registered via ui.expose_api(): the
                                            brick's expose_api() return-value contract is
                                            documented as producing a JSON response, with no
                                            documented way to hand back arbitrary bytes/headers, so
                                            (following the precedent set by scummvm-q's MJPEG
                                            passthrough) this is mounted directly on `ui.app`,
                                            WebUI's own public FastAPI instance, returning a plain
                                            fastapi.Response. Every other book JSON payload
                                            (book_to_dict) instead carries `cover_url` pointing at
                                            this route (or null if there's no cover) rather than
                                            inlining the bytes -- keeping the list/search JSON
                                            small. The one exception is unsaved OCR candidates
                                            (no id yet to hang a URL off), which get a ready-to-use
                                            `cover_data_uri` (data: URI, base64) instead -- see
                                            book_to_dict()'s docstring in library.py.

WebSocket messages (Socket.IO, via the WebUI Brick):
  server -> client, event "scan_event":
    {"ok": true, "code": "...", "device": "...", "book": {...}}   -- scanned + auto-saved
    {"ok": false, "code": "...", "device": "...", "reason": "not_found"} -- scanned, no metadata hit
  Client and event "shelf_candidates" are NOT sent over the socket -- shelf-photo processing is a
  synchronous REST call (POST /api/shelf_photo) since it's user-initiated with a result the same
  tab is waiting on, unlike a scan which can land from a device no browser tab is looking at.
  No other WebSocket messages are defined in this app (unlike progq/conquest-q, there's no
  continuously-ticking simulation state to broadcast -- every view is REST-driven and only the
  scan toast needs push).
"""

from __future__ import annotations

import base64

from fastapi import Response
from pydantic import BaseModel

from arduino.app_bricks.web_ui import WebUI
from arduino.app_utils import App

from engine.library import Library, book_to_dict, create_library
from engine.models import BookRecord

_DB_NAME = "techaq.db"


# -- POST/PUT body shapes ------------------------------------------------------------------------
# Pydantic models, matching the exact idiom civitas-q's python/ui/server.py uses for its own
# expose_api-equivalent FastAPI POST routes (PlayerAction/AdvisorQuestion/PlayerName there) --
# this brick wraps FastAPI, and FastAPI's own body-binding rules apply to whatever callable
# expose_api() registers, so a Pydantic-typed parameter is bound from the JSON body exactly like
# it would be on a route declared with @app.post(...) directly.

class BookIn(BaseModel):
    isbn13: str = ""
    isbn10: str = ""
    title: str = ""
    subtitle: str = ""
    authors: list[str] = []
    publisher: str = ""
    published_date: str = ""
    description: str = ""
    cover_mime: str = ""
    page_count: int | None = None
    categories: list[str] = []
    language: str = ""
    source: str = "manual"
    room: str = ""
    floor: str = ""
    column: str = ""
    shelf: str = ""
    notes: str = ""

    def to_book(self) -> BookRecord:
        return BookRecord(**self.dict())


class ScanIn(BaseModel):
    code: str
    device: str = ""


class AISearchIn(BaseModel):
    description: str


class ShelfPhotoIn(BaseModel):
    image_b64: str


class ShelfConfirmIn(BaseModel):
    books: list[dict]


def main():
    library = create_library(db_name=_DB_NAME)
    library.notify_startup()

    ui = WebUI()

    # -- books CRUD ---------------------------------------------------------------------------

    def list_books(q: str = "", room: str = "", floor: str = "", column: str = "", shelf: str = ""):
        if q:
            books = library.search_books(q)
        elif room or floor or column or shelf:
            books = library.list_by_location(room=room, floor=floor, column=column, shelf=shelf)
        else:
            books = library.list_all_books()
        return {"books": [book_to_dict(b) for b in books]}

    def get_book(book_id: int):
        book = library.get_book(book_id)
        if book is None:
            return {"error": "not found"}
        return book_to_dict(book)

    def create_book(body: BookIn):
        book = body.to_book()
        book_id = library.add_book(book)
        book.id = book_id
        return book_to_dict(book)

    def update_book(book_id: int, body: BookIn):
        book = body.to_book()
        book.id = book_id
        library.update_book(book_id, book)
        saved = library.get_book(book_id)
        return book_to_dict(saved) if saved else {"error": "not found"}

    def delete_book(book_id: int):
        library.delete_book(book_id)
        return {"ok": True}

    def get_locations():
        return library.distinct_locations()

    # -- scanner integration --------------------------------------------------------------------

    def handle_scan(body: ScanIn):
        library.notify_scan_received()
        book = library.add_by_isbn(body.code)
        if book is not None:
            payload = {"ok": True, "code": body.code, "device": body.device, "book": book_to_dict(book)}
            ui.send_message("scan_event", payload)
            return payload
        payload = {"ok": False, "code": body.code, "device": body.device, "reason": "not_found"}
        ui.send_message("scan_event", payload)
        return payload

    def lookup_isbn(isbn: str):
        book = library.lookup_isbn(isbn)
        if book is None:
            return {"found": False}
        return {"found": True, "book": book_to_dict(book, include_cover_data_uri=True)}

    # -- AI describe-to-find --------------------------------------------------------------------

    def ai_search(body: AISearchIn):
        available = bool(library.ai_agent and getattr(library.ai_agent, "available", False))
        results = library.ai_describe_search(body.description) if available else []
        return {"available": available, "results": [book_to_dict(b) for b in results]}

    # -- shelf photo OCR ---------------------------------------------------------------------

    def shelf_photo(body: ShelfPhotoIn):
        try:
            image_bytes = base64.b64decode(body.image_b64)
        except Exception as exc:
            return {"error": f"invalid base64 image: {exc!r}", "candidates": []}
        candidates = library.process_shelf_image(image_bytes)
        return {"candidates": candidates}

    def shelf_photo_confirm(body: ShelfConfirmIn):
        ids = library.confirm_shelf_candidates(body.books)
        return {"ids": ids}

    ui.expose_api("GET", "/api/books", list_books)
    ui.expose_api("GET", "/api/books/{book_id}", get_book)
    ui.expose_api("POST", "/api/books", create_book)
    ui.expose_api("PUT", "/api/books/{book_id}", update_book)
    ui.expose_api("DELETE", "/api/books/{book_id}", delete_book)
    ui.expose_api("GET", "/api/locations", get_locations)

    ui.expose_api("POST", "/api/scan", handle_scan)
    ui.expose_api("POST", "/api/lookup/{isbn}", lookup_isbn)

    ui.expose_api("POST", "/api/ai_search", ai_search)

    ui.expose_api("POST", "/api/shelf_photo", shelf_photo)
    ui.expose_api("POST", "/api/shelf_photo/confirm", shelf_photo_confirm)

    # Raw-bytes route, mounted directly on the Brick's own FastAPI instance -- see module
    # docstring's "GET /api/books/{book_id}/cover" entry for why this bypasses expose_api().
    @ui.app.get("/api/books/{book_id}/cover")
    def get_cover(book_id: int):
        book = library.get_book(book_id)
        if book is None or not book.cover_image:
            return Response(status_code=404)
        return Response(content=book.cover_image, media_type=book.cover_mime or "application/octet-stream")

    App.run()  # blocks until the app is stopped


if __name__ == "__main__":
    main()
