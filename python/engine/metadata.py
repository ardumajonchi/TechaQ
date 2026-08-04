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
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import requests

from .models import BookRecord

log = logging.getLogger(__name__)

_TIMEOUT = 5
_MIN_COVER_BYTES = 1024  # Open Library's "no cover" placeholder is a tiny real gif under ~1KB.

_OPENLIBRARY_BOOKS_URL = "https://openlibrary.org/api/books"
_OPENLIBRARY_COVER_URL = "https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
_OPENLIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
_OPENLIBRARY_COVER_BY_ID_URL = "https://covers.openlibrary.org/b/id/{cover_id}-M.jpg"
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


def _fetch_openlibrary(isbn: str) -> dict:
    """Return a dict of fields found from Open Library, or {} on any failure. Never raises."""
    out: dict = {}
    try:
        resp = requests.get(
            _OPENLIBRARY_BOOKS_URL,
            params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json() or {}
        data = payload.get(f"ISBN:{isbn}") or {}
        if data:
            out["title"] = data.get("title", "")
            out["subtitle"] = data.get("subtitle", "")
            out["authors"] = [a.get("name", "") for a in data.get("authors", []) if a.get("name")]
            publishers = data.get("publishers") or []
            out["publisher"] = publishers[0].get("name", "") if publishers else ""
            out["published_date"] = data.get("publish_date", "")
            out["page_count"] = data.get("number_of_pages")
            out["categories"] = [s.get("name", "") for s in data.get("subjects", []) if s.get("name")]
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


def _fetch_sru_dc(base_url: str, query: str, isbn: str, source_name: str) -> dict:
    """GET an SRU endpoint with an oai_dc/Dublin Core `recordSchema` and return a dict of fields
    parsed from the first matching record, or {} on any failure/no-hit. Never raises. Shared by
    every national-library SRU source (DNB, BNF) since they return the same XML shape."""
    out: dict = {}
    try:
        resp = requests.get(
            base_url,
            params={
                "version": "1.2",
                "operation": "searchRetrieve",
                "query": query,
                "recordSchema": "dc",
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
    catalog, or {} on any failure/no-hit. Never raises."""
    return _fetch_sru_dc(_DNB_SRU_URL, f"isbn={isbn}", isbn, "DNB")


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


def fetch_by_isbn(isbn: str, include_description: bool = False) -> BookRecord | None:
    """Look up a book by ISBN-10/ISBN-13/raw EAN-13 barcode, querying Open Library, Google Books,
    DNB, and BNF concurrently and merging their results into one BookRecord. Returns None only if
    every source found nothing.

    `include_description` defaults to False (skip fetching the synopsis) so a scan/save doesn't
    pay for Google Books' description text on the default path -- every other field still merges
    normally from whichever sources hit, so this doesn't reduce lookup coverage, only the
    synopsis. Pass True (or use `fetch_description` directly) to include it."""
    clean = _clean_isbn(isbn)
    if not clean:
        return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        ol_future = pool.submit(_fetch_openlibrary, clean)
        gb_future = pool.submit(_fetch_googlebooks, clean)
        dnb_future = pool.submit(_fetch_dnb, clean)
        bnf_future = pool.submit(_fetch_bnf, clean)
        ol = ol_future.result()
        gb = gb_future.result()
        dnb = dnb_future.result()
        bnf = bnf_future.result()

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
