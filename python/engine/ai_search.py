# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""AI-powered "describe a book to find it" search, on the official arduino:llm Brick.

Same grounding invariant used elsewhere in this workspace's LLM agents (see conquest-q's
agents/leader.py and progq's agents/operator.py): the model is never allowed to just answer from
its own head. It must call the search_books tool, and describe_to_find() only ever returns real
BookRecord results stashed by that tool call -- never anything the model claims exists.

The `arduino.app_bricks.llm` import is wrapped in a try/except so this module (and its tests)
stay importable on a dev machine without the `arduino` package installed at all -- AISearchAg
degrades to "unavailable" (self.available = False) rather than failing to import.
"""

from __future__ import annotations

import json
import logging

try:
    from arduino.app_bricks.llm import LargeLanguageModel, tool
except ImportError:  # pragma: no cover - exercised via tests monkeypatching this module's symbol
    LargeLanguageModel = None

    def tool(fn):  # no-op stand-in so @tool stays applicable when the real Brick isn't installed
        return fn

from .metadata import search_by_title_author
from .models import BookRecord

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You help find books in a home library by turning a natural-language description into a "
    "search. Always call the search_books tool with your best guess at title and/or author -- "
    "never claim a book exists without calling the tool."
)


class AISearchAgent:
    def __init__(self, model: str | None = None):
        self.available = False
        self._llm = None
        self._last_results: list[BookRecord] = []

        if LargeLanguageModel is None:
            log.warning("arduino.app_bricks.llm unavailable; AI search disabled")
            return

        @tool
        def search_books(title: str, author: str = "") -> str:
            """Search the home library's book metadata sources for a book by title and/or
            author. Call this with your best guess at the title and/or author extracted from the
            user's natural-language description. Returns a JSON-encoded list of matching books.

            Args:
                title: the book's title, or your best guess at it.
                author: the book's author, if known or guessed.
            """
            results = search_by_title_author(title, author)
            self._last_results = results
            return json.dumps(
                [
                    {
                        "title": r.title,
                        "authors": r.authors,
                        "published_date": r.published_date,
                        "isbn13": r.isbn13,
                    }
                    for r in results
                ]
            )

        try:
            self._llm = LargeLanguageModel(
                system_prompt=_SYSTEM_PROMPT,
                tools=[search_books],
                model=model,
            )
            self.available = True
        except Exception as exc:
            log.warning("failed to construct LargeLanguageModel; AI search disabled: %s", exc)
            self._llm = None
            self.available = False

    def describe_to_find(self, description: str) -> list[BookRecord]:
        """Turn a natural-language description into real search results via the LLM's single
        tool call. Returns [] if the feature is unavailable or the chat call fails -- the LLM
        itself can never surface a fabricated book, only whatever search_books actually found."""
        if not self.available:
            return []

        self._last_results = []
        try:
            self._llm.chat(description)
        except Exception as exc:
            log.warning("AI search chat() failed: %s", exc)
            return []

        return self._last_results
