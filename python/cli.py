#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Command-line front-end for TechaQ, calling the EXACT SAME `engine.library.Library` methods
that python/main.py's WebUI handlers call -- there is no separate CLI code path for "add a book"
or "search"; this file is purely argument parsing + stdout formatting glued onto library.py.

Meant to be run from inside the running app container, e.g.:

    docker exec -it <techaq-container-name> python3 /app/python/cli.py list
    docker exec -it <techaq-container-name> python3 /app/python/cli.py add-isbn 9780134685991
    docker exec -it <techaq-container-name> python3 /app/python/cli.py search "brief history"

(exact container name/App Lab invocation to be confirmed against the hardware teammate's deploy
docs -- see host/scanner_reader.py's README for how it locates the running container.)

Subcommands: add-isbn, add-manual, search, list, list-location, ai-search, delete, show.
"""

from __future__ import annotations

import argparse
import sys

from engine.library import create_library
from engine.models import BookRecord

_DB_NAME = "techaq.db"


def _fmt_book(book: BookRecord) -> str:
    authors = ", ".join(book.authors) if book.authors else "(unknown author)"
    isbn = book.isbn13 or book.isbn10 or "(no isbn)"
    location = "/".join(p for p in (book.room, book.floor, book.column, book.shelf) if p) or "(no location)"
    return f"[{book.id}] {book.title or '(untitled)'} -- {authors}\n     isbn={isbn}  location={location}"


def _print_books(books: list[BookRecord]) -> None:
    if not books:
        print("(no books)")
        return
    for book in books:
        print(_fmt_book(book))


def cmd_add_isbn(library, args) -> int:
    book = library.add_by_isbn(args.isbn)
    if book is None:
        print(f"No metadata found for ISBN {args.isbn!r} (or metadata lookup is unavailable).")
        return 1
    print("Saved:")
    print(_fmt_book(book))
    return 0


def cmd_add_manual(library, args) -> int:
    def ask(prompt: str, default: str = "") -> str:
        val = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
        return val or default

    title = args.title or ask("Title")
    if not title:
        print("Title is required.", file=sys.stderr)
        return 1
    authors_raw = args.authors if args.authors is not None else ask("Authors (comma-separated)")
    authors = [a.strip() for a in authors_raw.split(",") if a.strip()]
    book = BookRecord(
        title=title,
        subtitle=args.subtitle or (ask("Subtitle") if args.interactive else ""),
        authors=authors,
        isbn13=args.isbn13 or (ask("ISBN-13") if args.interactive else ""),
        isbn10=args.isbn10 or (ask("ISBN-10") if args.interactive else ""),
        publisher=args.publisher or (ask("Publisher") if args.interactive else ""),
        published_date=args.published_date or (ask("Published date") if args.interactive else ""),
        description=args.description or "",
        page_count=args.page_count,
        language=args.language or "",
        source="manual",
        room=args.room or (ask("Room") if args.interactive else ""),
        floor=args.floor or (ask("Floor") if args.interactive else ""),
        column=args.column or (ask("Column") if args.interactive else ""),
        shelf=args.shelf or (ask("Shelf") if args.interactive else ""),
        notes=args.notes or "",
    )
    book_id = library.add_book(book)
    book.id = book_id
    print("Saved:")
    print(_fmt_book(book))
    return 0


def cmd_search(library, args) -> int:
    _print_books(library.search_books(args.keyword))
    return 0


def cmd_list(library, args) -> int:
    _print_books(library.list_all_books())
    return 0


def cmd_list_location(library, args) -> int:
    books = library.list_by_location(room=args.room, floor=args.floor, column=args.column, shelf=args.shelf)
    _print_books(books)
    return 0


def cmd_ai_search(library, args) -> int:
    if not (library.ai_agent and getattr(library.ai_agent, "available", False)):
        print("AI search is unavailable on this board.")
        return 1
    _print_books(library.ai_describe_search(args.description))
    return 0


def cmd_delete(library, args) -> int:
    if library.get_book(args.id) is None:
        print(f"No book with id {args.id}.", file=sys.stderr)
        return 1
    library.delete_book(args.id)
    print(f"Deleted book {args.id}.")
    return 0


def cmd_show(library, args) -> int:
    book = library.get_book(args.id)
    if book is None:
        print(f"No book with id {args.id}.", file=sys.stderr)
        return 1
    print(_fmt_book(book))
    if book.description:
        print(f"\n{book.description}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="techaq", description="TechaQ book-inventory CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-isbn", help="Fetch metadata for an ISBN/EAN and save it")
    p.add_argument("isbn")
    p.set_defaults(func=cmd_add_isbn)

    p = sub.add_parser("add-manual", help="Add a book by hand (flags, or interactive prompts for anything omitted)")
    p.add_argument("--title")
    p.add_argument("--subtitle")
    p.add_argument("--authors", help="comma-separated")
    p.add_argument("--isbn13")
    p.add_argument("--isbn10")
    p.add_argument("--publisher")
    p.add_argument("--published-date", dest="published_date")
    p.add_argument("--description")
    p.add_argument("--page-count", dest="page_count", type=int)
    p.add_argument("--language")
    p.add_argument("--room")
    p.add_argument("--floor")
    p.add_argument("--column")
    p.add_argument("--shelf")
    p.add_argument("--notes")
    p.add_argument("--interactive", action="store_true", help="prompt for any field not given as a flag")
    p.set_defaults(func=cmd_add_manual)

    p = sub.add_parser("search", help="Keyword search across title/subtitle/authors/description/notes")
    p.add_argument("keyword")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("list", help="List every book")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("list-location", help="Filter books by shelf location")
    p.add_argument("--room", default="")
    p.add_argument("--floor", default="")
    p.add_argument("--column", default="")
    p.add_argument("--shelf", default="")
    p.set_defaults(func=cmd_list_location)

    p = sub.add_parser("ai-search", help="Natural-language 'describe the book you're thinking of' search")
    p.add_argument("description")
    p.set_defaults(func=cmd_ai_search)

    p = sub.add_parser("delete", help="Delete a book by id")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("show", help="Show full detail for one book")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    library = create_library(db_name=_DB_NAME)
    try:
        return args.func(library, args)
    finally:
        library.close()


if __name__ == "__main__":
    sys.exit(main())
