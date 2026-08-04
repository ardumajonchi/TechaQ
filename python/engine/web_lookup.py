# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Last-resort metadata fallback for an ISBN that misses all four of metadata.py's real catalog
sources (Open Library, Google Books, DNB, BNF): scrape a handful of web search-result snippets
for the ISBN and have the local LLM guess a title/author from them.

No keyless web-search API tested during development proved both reliable and free -- DuckDuckGo
Lite intermittently bot-blocks scraping requests instead of returning real results. This is
accepted as a best-effort fallback: it sometimes still finds nothing even when a live web search
would have, exactly like this app's other free/keyless sources degrade on an outage or 429.

Same grounding invariant as ai_search.py/ocr.py: the LLM only ever proposes a title/author guess
from real snippet text it was given (never inventing one), and library.py resolves that guess
against metadata.search_by_title_author()'s real catalog search before it's ever treated as an
actual match -- this module alone can never hand back a fabricated book.

The `arduino.app_bricks.llm` import is wrapped in a try/except so this module (and its tests)
stay importable on a dev machine without the `arduino` package installed at all --
WebMetadataFallback degrades to "unavailable" (self.available = False) rather than failing to
import, same as AISearchAgent.
"""

from __future__ import annotations

import json
import logging
import re

import requests

try:
    from arduino.app_bricks.llm import LargeLanguageModel
except ImportError:  # pragma: no cover - exercised via tests monkeypatching this module's symbol
    LargeLanguageModel = None

log = logging.getLogger(__name__)

_DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
_REQUEST_TIMEOUT_S = 8
# A plain requests.get default User-Agent gets bot-checked far more often than a browser-shaped
# one -- this doesn't make DuckDuckGo Lite scraping reliable (it still intermittently blocks even
# with this header), just less unreliable than the bare default.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_RESULT_LINK_RE = re.compile(r'<a[^>]*class="result-link"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_RESULT_SNIPPET_RE = re.compile(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(html_fragment: str) -> str:
    return _TAG_RE.sub("", html_fragment).strip()


def _search_ddg_lite(query: str) -> str:
    """GET DuckDuckGo Lite's HTML results page for `query` and return a plain-text blob joining
    every result's title + snippet, or "" on any failure, non-200, or a response that doesn't
    contain any recognizable result markup (e.g. DuckDuckGo's own bot-check page). Never raises.
    """
    try:
        resp = requests.get(
            _DDG_LITE_URL,
            params={"q": query},
            headers={"User-Agent": _USER_AGENT},
            timeout=_REQUEST_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        log.warning("DuckDuckGo Lite request failed for %r: %s", query, exc)
        return ""

    if resp.status_code != 200:
        log.warning("DuckDuckGo Lite returned HTTP %s for %r", resp.status_code, query)
        return ""

    html = resp.text
    titles = [_strip_tags(m) for m in _RESULT_LINK_RE.findall(html)]
    snippets = [_strip_tags(m) for m in _RESULT_SNIPPET_RE.findall(html)]
    pieces = [p for p in (titles + snippets) if p]
    return "\n".join(pieces)


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]  # drop opening ```json / ```
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines)
    return stripped.strip()


_WEB_LLM_PROMPT_TEMPLATE = """The following are web search-result snippets for the query \
"isbn {isbn} book" (title/snippet text may be noisy, off-topic, or in a language you don't \
recognize).

Try to identify the specific book with ISBN {isbn} from these snippets. Respond with ONLY a
single JSON object in this exact shape, no commentary, no markdown code fences:
{{"title": "...", "author": "..."}}

Rules:
- Only fill in "title" if you can confidently identify it from the snippets below -- never invent
  a title that doesn't actually appear in the text.
- Leave "author" as an empty string if you can't confidently identify one -- never invent one.
- If nothing here plausibly identifies this book, respond with {{"title": "", "author": ""}}.

Search snippets:
{snippets}
"""


class WebMetadataFallback:
    def __init__(self, model: str | None = None):
        self.available = False
        self._llm = None

        if LargeLanguageModel is None:
            log.warning("arduino.app_bricks.llm unavailable; web metadata fallback disabled")
            return

        try:
            self._llm = LargeLanguageModel(
                system_prompt=(
                    "You extract a book's title/author from noisy web search-result snippets. "
                    "Never invent a title or author that doesn't actually appear in the text."
                ),
                model=model,
            )
            self.available = True
        except Exception as exc:
            log.warning("failed to construct LargeLanguageModel; web metadata fallback disabled: %s", exc)
            self._llm = None
            self.available = False

    def lookup(self, isbn: str) -> dict:
        """Best-effort: scrape web search snippets for `isbn`, ask the LLM to guess a
        {"title","author"} from them. Returns {} if unavailable, if the scrape found nothing
        (e.g. DuckDuckGo Lite bot-blocked this request), or if the LLM couldn't confidently
        identify a title. Never raises."""
        if not self.available:
            return {}

        snippets = _search_ddg_lite(f"isbn {isbn} book")
        if not snippets.strip():
            return {}

        try:
            prompt = _WEB_LLM_PROMPT_TEMPLATE.format(isbn=isbn, snippets=snippets)
            response = self._llm.chat(prompt)
        except Exception as exc:
            log.warning("WebMetadataFallback: LLM call failed for isbn %s: %s", isbn, exc)
            return {}

        try:
            cleaned = _strip_code_fences(response)
            parsed = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            log.warning("WebMetadataFallback: failed to parse LLM JSON response for isbn %s: %s", isbn, exc)
            return {}

        if not isinstance(parsed, dict):
            return {}

        title = str(parsed.get("title", "") or "").strip()
        if not title:
            return {}
        author = str(parsed.get("author", "") or "").strip()
        return {"title": title, "author": author}
