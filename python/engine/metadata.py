# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Metadata lookup: fetch a BookRecord for a scanned/typed ISBN by querying Open Library, Google
Books, and the German (DNB) and French (BNF) national library SRU catalogs concurrently and
merging their results, or search by title/author for the AI describe-to-find feature and OCR
candidate resolution.

Both public functions are network-call-testable: all HTTP access goes through plain
`requests.get(url, timeout=...)` calls (no session objects) so tests can monkeypatch/mock
`requests.get` directly. Neither function ever raises out to the caller -- any network/parsing
failure is logged and treated as "no data from that source" (fetch_by_isbn only returns None if
every source found nothing; search_by_title_author returns [] on any error).

`fetch_by_isbn`'s optional `on_source_done` callback exists purely so a caller with a live UI
(engine/library.py -> main.py's Socket.IO wiring -> app.js's lookup-status checklist) can report
per-source progress as each of the four concurrent fetches actually completes, rather than
faking a sequential "checking A... checking B..." that doesn't match how the ThreadPoolExecutor
below really runs them. It's entirely optional and side-channel: the return value and merge
logic are identical whether or not a callback is passed.
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

import requests

from .models import BookRecord

log = logging.getLogger(__name__)

_TIMEOUT = 5
_MIN_COVER_BYTES = 1024  # Open Library's "no cover" placeholder is a tiny real gif under ~1KB.

# The four catalog sources fetch_by_isbn dispatches concurrently, in the fixed precedence order
# field-merging uses (see `merged()` below) -- exposed here so a caller that wants to announce
# "now checking: Open Library, Google Books, DNB, BNF" (see engine/library.py's lookup_isbn) has
# one place to get that list from, rather than hardcoding it a second time.
SOURCE_NAMES = ("openlibrary", "googlebooks", "dnb", "bnf")

_OPENLIBRARY_EDITION_URL = "https://openlibrary.org/isbn/{isbn}.json"
_OPENLIBRARY_RESOURCE_URL = "https://openlibrary.org{key}.json"
_OPENLIBRARY_COVER_URL = "https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
_OPENLIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
_OPENLIBRARY_COVER_BY_ID_URL = "https://covers.openlibrary.org/b/id/{cover_id}-M.jpg"
_MAX_AUTHOR_LOOKUPS = 5  # cap per-author name resolution calls for anthologies/edited volumes.
_GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"
_DNB_SRU_URL = "https://services.dnb.de/sru/dnb"
_BNF_SRU_URL = "https://catalogue.bnf.fr/api/SRU"

# Role/relator annotations that national-library catalogs append to creator names, e.g.
# "Bloch, Joshua [Verfasser]" (DNB) or "Camus, Albert (1913-1960). Auteur du texte" (BNF).
_CREATOR_ROLE_RE = re.compile(
    r"\.\s*(?:Auteur|Autrice|Verfasser|Editor|Éditeur|Illustrator|Übers)", re.IGNORECASE
)
_BRACKETED_RE = re.compile(r"[\[\(][^\]\)]*[\]\)]")


def _clean_isbn(isbn: str) -> str:
    """Strip everything but digits -- accepts ISBN-10, ISBN-13, or a raw scanned EAN-13 barcode
    string (which for books IS the ISBN-13)."""
    return re.sub(r"[^0-9Xx]", "", isbn or "")


def _download_cover(url: str) -> tuple[bytes | None, str]:
    """Download an image, returning (bytes, mime) or (None, "") if it looks like a missing-cover
    placeholder, isn't actually an image, or the request fails."""
    try:
        resp = requests.get(url, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        log.warning("cover download failed for %s: %s", url, exc)
        return None, ""
    if resp.status_code != 200:
        return None, ""
    content_type = resp.headers.get("Content-Type", "")
    if "image" not in content_type:
        return None, ""
    data = resp.content
    if len(data) < _MIN_COVER_BYTES:
        # Open Library redirects unknown covers to a real (but tiny) placeholder gif.
        return None, ""
    return data, content_type


def _resolve_openlibrary_author_names(author_keys: list[str]) -> list[str]:
    """Resolve up to `_MAX_AUTHOR_LOOKUPS` Open Library author `/authors/OL...A` keys to display
    names via one GET per key. Skips (never raises for) any key that fails or has no name -- a
    partial author list from this is still strictly better than the whole source contributing
    nothing. Capped because a handful of edited anthologies/textbooks list dozens of contributors,
    which would otherwise turn one ISBN lookup into dozens of sequential HTTP calls."""
    names: list[str] = []
    for key in author_keys[:_MAX_AUTHOR_LOOKUPS]:
        try:
            resp = requests.get(_OPENLIBRARY_RESOURCE_URL.format(key=key), timeout=_TIMEOUT)
            resp.raise_for_status()
            name = (resp.json() or {}).get("name", "")
            if name:
                names.append(name)
        except requests.RequestException as exc:
            log.warning("Open Library author lookup failed for %s: %s", key, exc)
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("Open Library author parse failed for %s: %s", key, exc)
    return names


def _fetch_openlibrary(isbn: str) -> dict:
    """Return a dict of fields found from Open Library, or {} on any failure. Never raises.

    Uses the single-edition REST endpoint (`/isbn/{isbn}.json`) rather than the older aggregate
    `/api/books?...&jscmd=data` endpoint this used to call: as of 2026-08, the `/api/books`
    endpoint (and `/search.json`, used by `search_by_title_author` below) is unreliable from at
    least some network paths -- observed hanging to a curl timeout or returning HTTP 503 on the
    large majority of requests regardless of how long the timeout is (tried up to 40s), while
    `/isbn/{isbn}.json`, `/authors/{key}.json`, and `/works/{key}.json` consistently respond in
    1-3 seconds. This is presumably Open Library rate-limiting or otherwise deprioritizing that
    legacy aggregate endpoint server-side, not a client-side timeout problem, since no timeout
    length made it reliable. The per-resource endpoints require an extra round trip to resolve
    author names (and, best-effort, subjects) instead of getting them inline, which is the
    tradeoff for actually getting a response."""
    out: dict = {}
    try:
        resp = requests.get(_OPENLIBRARY_EDITION_URL.format(isbn=isbn), timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json() or {}
        out["title"] = data.get("title", "")
        out["subtitle"] = data.get("subtitle", "")
        publishers = data.get("publishers") or []
        out["publisher"] = publishers[0] if publishers else ""
        out["published_date"] = data.get("publish_date", "")
        out["page_count"] = data.get("number_of_pages")

        author_keys = [a.get("key", "") for a in data.get("authors", []) or [] if a.get("key")]
        if author_keys:
            names = _resolve_openlibrary_author_names(author_keys)
            if names:
                out["authors"] = names

        work_keys = [w.get("key", "") for w in data.get("works", []) or [] if w.get("key")]
        if work_keys:
            try:
                work_resp = requests.get(
                    _OPENLIBRARY_RESOURCE_URL.format(key=work_keys[0]), timeout=_TIMEOUT
                )
                work_resp.raise_for_status()
                subjects = (work_resp.json() or {}).get("subjects") or []
                if subjects:
                    out["categories"] = [s for s in subjects if isinstance(s, str)]
            except requests.RequestException as exc:
                log.warning("Open Library work lookup failed for isbn %s: %s", isbn, exc)
            except (ValueError, KeyError, TypeError) as exc:
                log.warning("Open Library work parse failed for isbn %s: %s", isbn, exc)
    except requests.RequestException as exc:
        log.warning("Open Library metadata lookup failed for isbn %s: %s", isbn, exc)
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("Open Library metadata parse failed for isbn %s: %s", isbn, exc)

    try:
        cover, mime = _download_cover(_OPENLIBRARY_COVER_URL.format(isbn=isbn))
        if cover:
            out["cover_image"] = cover
            out["cover_mime"] = mime
    except Exception as exc:  # pragma: no cover - defensive, _download_cover already guards
        log.warning("Open Library cover lookup failed for isbn %s: %s", isbn, exc)

    return out


def _fetch_googlebooks(isbn: str) -> dict:
    """Return a dict of fields found from Google Books, or {} on any failure (including
    429/rate-limiting, which is common without an API key). Never raises."""
    out: dict = {}
    params = {"q": f"isbn:{isbn}"}
    api_key = os.environ.get("GOOGLE_BOOKS_API_KEY")
    if api_key:
        params["key"] = api_key
    try:
        resp = requests.get(_GOOGLE_BOOKS_URL, params=params, timeout=_TIMEOUT)
        if resp.status_code != 200:
            log.warning("Google Books lookup for isbn %s returned HTTP %s", isbn, resp.status_code)
            return out
        payload = resp.json() or {}
        items = payload.get("items") or []
        if not items:
            return out
        volume = items[0].get("volumeInfo", {}) or {}
        out["title"] = volume.get("title", "")
        out["subtitle"] = volume.get("subtitle", "")
        out["authors"] = list(volume.get("authors", []) or [])
        out["publisher"] = volume.get("publisher", "")
        out["published_date"] = volume.get("publishedDate", "")
        out["description"] = volume.get("description", "")
        out["categories"] = list(volume.get("categories", []) or [])
        out["page_count"] = volume.get("pageCount")
        out["language"] = volume.get("language", "")
        thumbnail = (volume.get("imageLinks") or {}).get("thumbnail", "")
        if thumbnail:
            out["_thumbnail_url"] = thumbnail
    except requests.RequestException as exc:
        log.warning("Google Books lookup failed for isbn %s: %s", isbn, exc)
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("Google Books parse failed for isbn %s: %s", isbn, exc)
    return out


def _clean_creator_name(raw: str) -> str:
    """Best-effort tidy-up of a Dublin Core dc:creator value from a library-catalog SRU record:
    strip trailing role annotations (e.g. "[Verfasser]", ". Auteur du texte") and reorder a
    "Last, First" shape to "First Last". Falls back to the raw string if it doesn't match."""
    name = _BRACKETED_RE.sub("", raw)
    name = _CREATOR_ROLE_RE.split(name)[0]
    name = name.strip().rstrip(".")
    if "," in name:
        last, _, first = name.partition(",")
        last, first = last.strip(), first.strip()
        # Drop a trailing "(1913-1960)"-style date span that partition may have left on `first`.
        first = re.sub(r"\(\d{4}-?\d{0,4}\)\s*$", "", first).strip()
        if last and first:
            name = f"{first} {last}"
        else:
            name = last or first or name
    return name.strip()


def _fetch_sru_dc(
    base_url: str, query: str, isbn: str, source_name: str, version: str = "1.2", record_schema: str = "dc"
) -> dict:
    """GET an SRU endpoint with a Dublin Core `recordSchema` and return a dict of fields parsed
    from the first matching record, or {} on any failure/no-hit. Never raises. Shared by every
    national-library SRU source (DNB, BNF) since they return the same XML shape -- but each
    catalog enforces its own SRU `version`/`recordSchema` values (DNB rejects version 1.2 and the
    bare "dc" schema, requiring 1.1/"oai_dc" instead; BNF requires the opposite), so callers pass
    theirs explicitly rather than sharing one hardcoded pair."""
    out: dict = {}
    try:
        resp = requests.get(
            base_url,
            params={
                "version": version,
                "operation": "searchRetrieve",
                "query": query,
                "recordSchema": record_schema,
                "maximumRecords": 1,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except requests.RequestException as exc:
        log.warning("%s SRU lookup failed for isbn %s: %s", source_name, isbn, exc)
        return out
    except ET.ParseError as exc:
        log.warning("%s SRU response parse failed for isbn %s: %s", source_name, isbn, exc)
        return out

    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    record = root.find(".//dc:title/..", ns)
    if record is None:
        return out

    def texts(tag: str) -> list[str]:
        return [el.text.strip() for el in record.findall(f"dc:{tag}", ns) if el.text and el.text.strip()]

    titles = texts("title")
    if titles:
        out["title"] = titles[0]
    creators = [_clean_creator_name(c) for c in texts("creator")]
    if creators:
        out["authors"] = [c for c in creators if c]
    publishers = texts("publisher")
    if publishers:
        out["publisher"] = publishers[-1]  # national catalogs often list place then publisher.
    dates = texts("date")
    if dates:
        out["published_date"] = dates[0]
    subjects = texts("subject")
    if subjects:
        out["categories"] = subjects

    return out


def _fetch_dnb(isbn: str) -> dict:
    """Return a dict of fields found from the Deutsche Nationalbibliothek's free, keyless SRU
    catalog, or {} on any failure/no-hit. Never raises. DNB's SRU service rejects the SRU 1.2
    protocol version and the bare "dc" recordSchema (both fine for BNF) with a diagnostic
    response, not an HTTP error -- it needs 1.1/"oai_dc" specifically."""
    return _fetch_sru_dc(_DNB_SRU_URL, f"isbn={isbn}", isbn, "DNB", version="1.1", record_schema="oai_dc")


def _fetch_bnf(isbn: str) -> dict:
    """Return a dict of fields found from the Bibliothèque nationale de France's free, keyless
    SRU catalog, or {} on any failure/no-hit. Never raises. Uses the "fuzzyIsbn" index, which
    (unlike the plain "isbn" index) reliably matches regardless of hyphenation."""
    return _fetch_sru_dc(_BNF_SRU_URL, f'bib.fuzzyIsbn all "{isbn}"', isbn, "BNF")


def _richer(a, b):
    """Pick the "richer" of two values of the same field when both sources have one: longer
    string, or the list with more entries. Falls back to whichever is truthy."""
    if not a:
        return b
    if not b:
        return a
    if isinstance(a, str) and isinstance(b, str):
        return a if len(a) >= len(b) else b
    if isinstance(a, list) and isinstance(b, list):
        return a if len(a) >= len(b) else b
    return a or b


def fetch_by_isbn(
    isbn: str,
    include_description: bool = False,
    on_source_done: Callable[[str, bool], None] | None = None,
) -> BookRecord | None:
    """Look up a book by ISBN-10/ISBN-13/raw EAN-13 barcode, querying Open Library, Google Books,
    DNB, and BNF concurrently and merging their results into one BookRecord. Returns None only if
    every source found nothing.

    `include_description` defaults to False (skip fetching the synopsis) so a scan/save doesn't
    pay for Google Books' description text on the default path -- every other field still merges
    normally from whichever sources hit, so this doesn't reduce lookup coverage, only the
    synopsis. Pass True (or use `fetch_description` directly) to include it.

    `on_source_done`, if given, is called once per source (name from SOURCE_NAMES, hit: bool) the
    moment that source's own thread finishes -- in whatever order they actually complete, since
    all four fire at once rather than sequentially. This exists so a caller with a UI to update
    (see engine/library.py's lookup_isbn -> main.py's Socket.IO wiring) can report live per-source
    progress instead of one static "looking up" message for the whole call. It is never required:
    omit it (the default) for a plain blocking call, exactly as every existing caller/test does.
    Exceptions raised by the callback itself propagate out of `.result()` on the *next* future
    processed by as_completed() below, which would incorrectly abort the whole lookup for what's
    just a UI-side bug -- so the callback is wrapped in its own try/except here, never the
    fetch functions' own results."""
    clean = _clean_isbn(isbn)
    if not clean:
        return None

    fetchers = {
        "openlibrary": _fetch_openlibrary,
        "googlebooks": _fetch_googlebooks,
        "dnb": _fetch_dnb,
        "bnf": _fetch_bnf,
    }

    with ThreadPoolExecutor(max_workers=4) as pool:
        future_to_name = {pool.submit(fn, clean): name for name, fn in fetchers.items()}
        by_name: dict[str, dict] = {}
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            data = future.result()
            by_name[name] = data
            if on_source_done is not None:
                try:
                    on_source_done(name, bool(data))
                except Exception as exc:
                    log.warning("on_source_done callback failed for source %s: %s", name, exc)

    ol, gb, dnb, bnf = (by_name[name] for name in SOURCE_NAMES)
    results = [("openlibrary", ol), ("googlebooks", gb), ("dnb", dnb), ("bnf", bnf)]
    sources = [name for name, data in results if data]
    if not sources:
        return None

    def merged(field: str):
        value = None
        for _, data in results:
            value = _richer(value, data.get(field))
        return value

    title = merged("title")
    subtitle = merged("subtitle")
    authors = merged("authors")
    publisher = merged("publisher")
    published_date = merged("published_date")
    categories = merged("categories")
    language = ol.get("language") or gb.get("language") or ""

    description = ""
    if include_description:
        description = merged("description") or ""

    page_count = ol.get("page_count")
    if page_count is None:
        page_count = gb.get("page_count")

    cover_image = ol.get("cover_image")
    cover_mime = ol.get("cover_mime", "")
    if not cover_image:
        thumb_url = gb.get("_thumbnail_url")
        if thumb_url:
            cover_image, cover_mime = _download_cover(thumb_url)

    record = BookRecord(
        title=title or "",
        subtitle=subtitle or "",
        authors=authors or [],
        publisher=publisher or "",
        published_date=published_date or "",
        description=description,
        cover_image=cover_image,
        cover_mime=cover_mime or "",
        page_count=page_count,
        categories=categories or [],
        language=language,
        source="+".join(sources),
    )

    if len(clean) == 13:
        record.isbn13 = clean
    elif len(clean) == 10:
        record.isbn10 = clean
    else:
        # Neither a clean 10 nor 13 digit code -- store as given rather than guess.
        record.isbn13 = clean

    return record


def fetch_description(isbn: str) -> str:
    """Fetch only the synopsis/description for an ISBN, for the manual "fetch synopsis" button --
    a deliberately narrow, single-source, fast call, since Google Books is the only integrated
    source that ever has a description. Returns "" on any failure or no-hit. Never raises."""
    clean = _clean_isbn(isbn)
    if not clean:
        return ""
    return _fetch_googlebooks(clean).get("description", "") or ""


def search_by_title_author(title: str, author: str = "") -> list[BookRecord]:
    """Search Open Library by title/author, returning up to 5 BookRecord-shaped results. Returns
    an empty list on any error -- never raises."""
    query = " ".join(part for part in (title or "", author or "") if part).strip()
    if not query:
        return []

    try:
        resp = requests.get(
            _OPENLIBRARY_SEARCH_URL,
            params={"q": query, "limit": 5},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json() or {}
        docs = payload.get("docs") or []
    except requests.RequestException as exc:
        log.warning("Open Library search failed for %r: %s", query, exc)
        return []
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("Open Library search parse failed for %r: %s", query, exc)
        return []

    results: list[BookRecord] = []
    for doc in docs:
        try:
            isbns = doc.get("isbn") or []
            cover_id = doc.get("cover_i")
            cover_image, cover_mime = (None, "")
            if cover_id:
                cover_image, cover_mime = _download_cover(
                    _OPENLIBRARY_COVER_BY_ID_URL.format(cover_id=cover_id)
                )
            record = BookRecord(
                title=doc.get("title", "") or "",
                authors=list(doc.get("author_name", []) or []),
                published_date=str(doc.get("first_publish_year", "") or ""),
                isbn13=isbns[0] if isbns else "",
                cover_image=cover_image,
                cover_mime=cover_mime,
                source="openlibrary",
            )
            results.append(record)
        except Exception as exc:
            log.warning("skipping malformed Open Library search result: %s", exc)
            continue

    return results
