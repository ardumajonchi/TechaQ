# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Thin wrapper over arduino:dbstorage_sqlstore's SQLStore. Owns the `books` table schema.
No other module should reach into SQLStore directly -- always go through here, and always use
bound parameters for any query touching user-supplied strings (see search_books' WHERE clause
construction), never raw string interpolation.
"""

from arduino.app_bricks.dbstorage_sqlstore import SQLStore

from .models import BookRecord

DB_NAME = "techaq.db"
TABLE = "books"

SCHEMA = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "isbn13": "TEXT",
    "isbn10": "TEXT",
    "title": "TEXT",
    "subtitle": "TEXT",
    "authors": "TEXT",
    "publisher": "TEXT",
    "published_date": "TEXT",
    "description": "TEXT",
    "cover_image": "BLOB",
    "cover_mime": "TEXT",
    "page_count": "INTEGER",
    "categories": "TEXT",
    "language": "TEXT",
    "source": "TEXT",
    "room": "TEXT",
    "floor": "TEXT",
    "column": "TEXT",
    "shelf": "TEXT",
    "notes": "TEXT",
    "is_read": "INTEGER",
    "in_reading_list": "INTEGER",
    "is_favorite": "INTEGER",
    "created_at": "TEXT",
    "updated_at": "TEXT",
}


class BookDB:
    def __init__(self, database_name: str = DB_NAME):
        self._store = SQLStore(database_name)
        self._store.start()
        self._store.create_table(TABLE, SCHEMA)
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """CREATE TABLE IF NOT EXISTS never adds columns to a table that already exists --
        migrate any pre-existing `books` table (from before is_read/in_reading_list/is_favorite
        existed) in place, so the on-device DB doesn't need to be dropped after this change."""
        existing = {row["name"] for row in (self._store.execute_sql(f"PRAGMA table_info({TABLE})") or [])}
        for column, sqltype in SCHEMA.items():
            if column not in existing:
                self._store.execute_sql(f"ALTER TABLE {TABLE} ADD COLUMN {column} {sqltype}")

    def stop(self) -> None:
        self._store.stop()

    def insert(self, book: BookRecord) -> int:
        book.touch()
        self._store.store(TABLE, book.to_row(), create_table=False)
        rows = self._store.execute_sql(
            f"SELECT id FROM {TABLE} ORDER BY id DESC LIMIT 1"
        )
        return rows[0]["id"] if rows else -1

    def update(self, book_id: int, book: BookRecord) -> None:
        book.touch()
        row = book.to_row()
        self._store.update(TABLE, row, condition=f"id = {int(book_id)}")

    def delete(self, book_id: int) -> None:
        self._store.delete(TABLE, condition=f"id = {int(book_id)}")

    def get(self, book_id: int) -> BookRecord | None:
        rows = self._store.read(TABLE, condition=f"id = {int(book_id)}", limit=1)
        return BookRecord.from_row(rows[0]) if rows else None

    def get_by_isbn(self, isbn: str) -> BookRecord | None:
        isbn = isbn.strip()
        rows = self._store.execute_sql(
            f"SELECT * FROM {TABLE} WHERE isbn13 = ? OR isbn10 = ? LIMIT 1",
            (isbn, isbn),
        )
        return BookRecord.from_row(rows[0]) if rows else None

    def list_all(self, order_by: str = "updated_at DESC") -> list[BookRecord]:
        rows = self._store.read(TABLE, order_by=order_by)
        return [BookRecord.from_row(r) for r in rows]

    def search(self, keyword: str) -> list[BookRecord]:
        like = f"%{keyword.strip()}%"
        rows = self._store.execute_sql(
            f"SELECT * FROM {TABLE} WHERE title LIKE ? OR subtitle LIKE ? OR authors LIKE ? "
            f"OR description LIKE ? OR notes LIKE ? ORDER BY updated_at DESC",
            (like, like, like, like, like),
        )
        return [BookRecord.from_row(r) for r in (rows or [])]

    def filter_by_location(
        self, room: str = "", floor: str = "", column: str = "", shelf: str = ""
    ) -> list[BookRecord]:
        clauses, args = [], []
        for field, value in (("room", room), ("floor", floor), ("column", column), ("shelf", shelf)):
            if value:
                clauses.append(f"{field} = ?")
                args.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._store.execute_sql(
            f"SELECT * FROM {TABLE} {where} ORDER BY room, floor, column, shelf", tuple(args)
        )
        return [BookRecord.from_row(r) for r in (rows or [])]

    def list_favorites(self, order_by: str = "updated_at DESC") -> list[BookRecord]:
        rows = self._store.execute_sql(f"SELECT * FROM {TABLE} WHERE is_favorite = 1 ORDER BY {order_by}")
        return [BookRecord.from_row(r) for r in (rows or [])]

    def distinct_locations(self) -> dict[str, list[str]]:
        out = {}
        for field in ("room", "floor", "column", "shelf"):
            rows = self._store.execute_sql(
                f"SELECT DISTINCT {field} FROM {TABLE} WHERE {field} != '' ORDER BY {field}"
            )
            out[field] = [r[field] for r in (rows or [])]
        return out
