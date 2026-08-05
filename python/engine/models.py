# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Shared data shape for a book record. This is the one contract every module in `engine/`
(and the WebUI/CLI layers) agrees on -- metadata.py builds it, db.py persists it, library.py
returns it, ocr.py and ai_search.py produce candidates in this shape for user confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, UTC
import json


@dataclass
class BookRecord:
    id: int | None = None
    isbn13: str = ""
    isbn10: str = ""
    title: str = ""
    subtitle: str = ""
    authors: list[str] = field(default_factory=list)
    publisher: str = ""
    published_date: str = ""
    description: str = ""
    cover_image: bytes | None = None
    cover_mime: str = ""
    page_count: int | None = None
    categories: list[str] = field(default_factory=list)
    language: str = ""
    # Provenance of the metadata: "openlibrary", "googlebooks", "manual", "ocr", or a
    # "+"-joined combination (e.g. "openlibrary+googlebooks") when merged from multiple APIs.
    source: str = "manual"
    room: str = ""
    floor: str = ""
    column: str = ""
    shelf: str = ""
    notes: str = ""
    is_read: bool = False
    in_reading_list: bool = False
    is_favorite: bool = False
    created_at: str = ""
    updated_at: str = ""

    def to_row(self) -> dict:
        """Flatten to the dict shape SQLStore.store()/update() expect (JSON-encode lists)."""
        row = asdict(self)
        row["authors"] = json.dumps(self.authors or [])
        row["categories"] = json.dumps(self.categories or [])
        if row["cover_image"] is None:
            row["cover_image"] = b""
        for bool_field in ("is_read", "in_reading_list", "is_favorite"):
            row[bool_field] = int(bool(row[bool_field]))
        row.pop("id", None)
        return row

    @classmethod
    def from_row(cls, row: dict) -> "BookRecord":
        """Inflate a sqlite3.Row-derived dict (as returned by SQLStore.read()) back into a
        BookRecord, decoding the JSON-encoded list columns."""
        data = dict(row)
        for list_field in ("authors", "categories"):
            raw = data.get(list_field) or "[]"
            try:
                data[list_field] = json.loads(raw) if isinstance(raw, str) else (raw or [])
            except (TypeError, ValueError):
                data[list_field] = []
        cover = data.get("cover_image")
        data["cover_image"] = cover if cover else None
        for bool_field in ("is_read", "in_reading_list", "is_favorite"):
            if bool_field in data:
                data[bool_field] = bool(data[bool_field])
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def touch(self) -> None:
        now = datetime.now(UTC).isoformat()
        if not self.created_at:
            self.created_at = now
        self.updated_at = now
