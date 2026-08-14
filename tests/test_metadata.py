# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Tests for engine/metadata.py: fetch_by_isbn (Open Library + Google Books + DNB + BNF merge
logic, richer-value-wins, graceful 429/error handling, cover-image fallback, deferred-description
gating), fetch_description (Google Books primary, Open Library work-description fallback), and
search_by_title_author (Open Library search parsing, empty-on-error). All network access is
mocked via monkeypatching requests.get -- no real HTTP calls are made.
"""

from __future__ import annotations

import pytest
import requests

from engine import metadata


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b"", headers=None):
        self.status_code = status_code
        self._json_data = json_data
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._json_data is None:
            raise ValueError("no json body")
        return self._json_data

    @property
    def text(self):
        return self.content.decode("utf-8") if isinstance(self.content, bytes) else self.content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")


def _openlibrary_edition_response(title="Dune", author_keys=("/authors/OL79034A",), publisher="Ace",
                                   published_date="1965", page_count=412, subtitle="",
                                   work_keys=("/works/OL893414W",)):
    """A minimal-but-realistic `/isbn/{isbn}.json` single-edition response. Authors/subjects are
    NOT inline here (unlike the old `/api/books?jscmd=data` shape) -- `_fetch_openlibrary` resolves
    author names via a follow-up `/authors/{key}.json` GET per key, and subjects via a follow-up
    `/works/{key}.json` GET on the first work key, matching what the real endpoint returns."""
    return FakeResponse(
        json_data={
            "title": title,
            "subtitle": subtitle,
            "authors": [{"key": k} for k in author_keys],
            "publishers": [publisher] if publisher else [],
            "publish_date": published_date,
            "number_of_pages": page_count,
            "works": [{"key": k} for k in work_keys],
        }
    )


def _openlibrary_author_response(name):
    return FakeResponse(json_data={"name": name})


def _openlibrary_work_response(subjects=("Sci-Fi",), description=None):
    data = {"subjects": list(subjects)}
    if description is not None:
        data["description"] = description
    return FakeResponse(json_data=data)


def _googlebooks_response(title="Dune", authors=("Frank Herbert",), publisher="Ace Books",
                           published_date="1965-06-01", description="A desert planet epic.",
                           categories=("Fiction",), page_count=420, language="en",
                           thumbnail="https://books.google.com/thumb.jpg"):
    image_links = {"thumbnail": thumbnail} if thumbnail else {}
    return FakeResponse(
        json_data={
            "items": [
                {
                    "volumeInfo": {
                        "title": title,
                        "authors": list(authors),
                        "publisher": publisher,
                        "publishedDate": published_date,
                        "description": description,
                        "categories": list(categories),
                        "pageCount": page_count,
                        "language": language,
                        "imageLinks": image_links,
                    }
                }
            ]
        }
    )


def _cover_response(size=5000, content_type="image/jpeg"):
    return FakeResponse(status_code=200, content=b"x" * size, headers={"Content-Type": content_type})


def _no_cover_response():
    # Open Library's "no cover" placeholder: a real but tiny gif (under ~1KB).
    return FakeResponse(status_code=200, content=b"g" * 40, headers={"Content-Type": "image/gif"})


def _sru_dc_response(title="", creator="", publisher="", date="", identifier="", subject=""):
    """A minimal-but-realistic DNB/BNF-shaped SRU searchRetrieve response with one record, or a
    zero-record response if title is empty."""
    if not title:
        return FakeResponse(
            status_code=200,
            content=(
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/">'
                b"<numberOfRecords>0</numberOfRecords><records/></searchRetrieveResponse>"
            ),
        )
    fields = f"<dc:title>{title}</dc:title>"
    if creator:
        fields += f"<dc:creator>{creator}</dc:creator>"
    if publisher:
        fields += f"<dc:publisher>{publisher}</dc:publisher>"
    if date:
        fields += f"<dc:date>{date}</dc:date>"
    if identifier:
        fields += f"<dc:identifier>{identifier}</dc:identifier>"
    if subject:
        fields += f"<dc:subject>{subject}</dc:subject>"
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/">'
        "<numberOfRecords>1</numberOfRecords><records><record>"
        '<recordData><dc xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns="http://www.openarchives.org/OAI/2.0/oai_dc/">'
        f"{fields}</dc></recordData></record></records></searchRetrieveResponse>"
    )
    return FakeResponse(status_code=200, content=xml.encode("utf-8"))


def _empty_sru_response():
    return _sru_dc_response()


def _opacsbn_response(title="", creator="", place_publisher_year=""):
    """A minimal-but-realistic OPAC SBN titles-search-post JSON response with one result, or a
    zero-result response if title is empty."""
    if not title:
        return FakeResponse(status_code=200, json_data={"status": "success", "data": {"total": 0, "results": []}})
    info = f"{title} / {creator}" if creator else title
    return FakeResponse(
        status_code=200,
        json_data={
            "status": "success",
            "data": {
                "total": 1,
                "results": [
                    {
                        "title": {"text": creator, "info": info},
                        "infos": [place_publisher_year] if place_publisher_year else [],
                    }
                ],
            },
        },
    )


def _empty_opacsbn_response():
    return _opacsbn_response()


def _isbnsearch_response(title="", author="", publisher="", published_date="", isbn13="9788807881114"):
    """A minimal-but-realistic isbnsearch.org per-ISBN page, or a bare 404 if title is empty."""
    if not title:
        return FakeResponse(status_code=404)
    fields = f"<h1>{title}</h1><p><strong>ISBN-13:</strong> {isbn13}</p>"
    if author:
        fields += f"<p><strong>Author:</strong> {author}</p>"
    if publisher:
        fields += f"<p><strong>Publisher:</strong> {publisher}</p>"
    if published_date:
        fields += f"<p><strong>Published:</strong> {published_date}</p>"
    return FakeResponse(status_code=200, content=fields.encode("utf-8"))


def _isbnsearch_botcheck_response():
    """isbnsearch.org's reCAPTCHA bot-check page -- HTTP 200, but no "ISBN-13:" field."""
    return FakeResponse(status_code=200, content=b"<h1>Please Verify to Continue</h1>")


def _empty_isbnsearch_response():
    return _isbnsearch_response()


def _dispatch(mapping, default_empty_sru=True):
    """Build a fake requests.get(url, **kwargs) that looks up a canned response by URL prefix.
    DNB/BNF/OPAC SBN/isbnsearch.org default to an empty/no-hit response unless explicitly
    overridden in `mapping`, so existing tests don't need to know about every source."""

    def fake_get(url, **kwargs):
        for prefix, response in mapping.items():
            if url.startswith(prefix):
                return response(**kwargs) if callable(response) and not isinstance(response, FakeResponse) else response
        if default_empty_sru and url in (metadata._DNB_SRU_URL, metadata._BNF_SRU_URL):
            return _empty_sru_response()
        if default_empty_sru and url == metadata._OPAC_SBN_URL:
            return _empty_opacsbn_response()
        if default_empty_sru and url.startswith("https://isbnsearch.org/isbn/"):
            return _empty_isbnsearch_response()
        raise AssertionError(f"unexpected URL requested: {url}")

    return fake_get


# ---------------------------------------------------------------------------
# fetch_by_isbn
# ---------------------------------------------------------------------------


def test_fetch_by_isbn_merges_both_sources(monkeypatch):
    isbn = "9780441172719"
    mapping = {
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): _openlibrary_edition_response(publisher="Ace"),
        metadata._OPENLIBRARY_RESOURCE_URL.format(key="/authors/OL79034A"): _openlibrary_author_response("Frank Herbert"),
        metadata._OPENLIBRARY_RESOURCE_URL.format(key="/works/OL893414W"): _openlibrary_work_response(),
        metadata._OPENLIBRARY_COVER_URL.format(isbn=isbn): _cover_response(),
        metadata._GOOGLE_BOOKS_URL: _googlebooks_response(publisher="Ace Books"),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping))
    monkeypatch.setattr(requests, "post", _dispatch(mapping))

    record = metadata.fetch_by_isbn(isbn, include_description=True)

    assert record is not None
    assert record.title == "Dune"
    assert record.authors == ["Frank Herbert"]
    assert record.isbn13 == isbn
    assert record.source == "openlibrary+googlebooks"
    # Open Library's cover wins when it has one.
    assert record.cover_image == b"x" * 5000
    # description only came from Google Books.
    assert record.description == "A desert planet epic."


def test_fetch_by_isbn_richer_value_wins_for_description_and_authors(monkeypatch):
    isbn = "9780441172719"
    # Both sources provide authors; Google has more of them, so it should win.
    ol_resp = _openlibrary_edition_response(author_keys=("/authors/OL79034A",))
    gb_resp = _googlebooks_response(authors=("Frank Herbert", "Brian Herbert"), thumbnail="")
    mapping = {
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): ol_resp,
        metadata._OPENLIBRARY_RESOURCE_URL.format(key="/authors/OL79034A"): _openlibrary_author_response("Frank Herbert"),
        metadata._OPENLIBRARY_RESOURCE_URL.format(key="/works/OL893414W"): _openlibrary_work_response(),
        metadata._OPENLIBRARY_COVER_URL.format(isbn=isbn): _no_cover_response(),
        metadata._GOOGLE_BOOKS_URL: gb_resp,
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping))
    monkeypatch.setattr(requests, "post", _dispatch(mapping))

    record = metadata.fetch_by_isbn(isbn)

    assert record.authors == ["Frank Herbert", "Brian Herbert"]
    # No cover from either source (OL placeholder rejected, Google had no thumbnail).
    assert record.cover_image is None


def test_fetch_by_isbn_openlibrary_only(monkeypatch):
    isbn = "9780441172719"
    mapping = {
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): _openlibrary_edition_response(),
        metadata._OPENLIBRARY_RESOURCE_URL.format(key="/authors/OL79034A"): _openlibrary_author_response("Frank Herbert"),
        metadata._OPENLIBRARY_RESOURCE_URL.format(key="/works/OL893414W"): _openlibrary_work_response(),
        metadata._OPENLIBRARY_COVER_URL.format(isbn=isbn): _cover_response(),
        metadata._GOOGLE_BOOKS_URL: FakeResponse(status_code=200, json_data={"items": []}),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping))
    monkeypatch.setattr(requests, "post", _dispatch(mapping))

    record = metadata.fetch_by_isbn(isbn)

    assert record is not None
    assert record.source == "openlibrary"
    assert record.title == "Dune"
    assert record.description == ""


def test_fetch_by_isbn_description_falls_back_to_openlibrary_when_googlebooks_misses(monkeypatch):
    isbn = "9780441172719"
    mapping = {
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): _openlibrary_edition_response(),
        metadata._OPENLIBRARY_RESOURCE_URL.format(key="/authors/OL79034A"): _openlibrary_author_response("Frank Herbert"),
        metadata._OPENLIBRARY_RESOURCE_URL.format(key="/works/OL893414W"): _openlibrary_work_response(
            description="Set on the desert planet Arrakis..."
        ),
        metadata._OPENLIBRARY_COVER_URL.format(isbn=isbn): _cover_response(),
        metadata._GOOGLE_BOOKS_URL: FakeResponse(status_code=200, json_data={"items": []}),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping))
    monkeypatch.setattr(requests, "post", _dispatch(mapping))

    record = metadata.fetch_by_isbn(isbn, include_description=True)

    assert record.source == "openlibrary"
    assert record.description == "Set on the desert planet Arrakis..."


def test_fetch_by_isbn_googlebooks_only(monkeypatch):
    isbn = "9780441172719"
    mapping = {
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): FakeResponse(status_code=404),
        metadata._GOOGLE_BOOKS_URL: _googlebooks_response(thumbnail="https://books.google.com/thumb.jpg"),
        "https://books.google.com/thumb.jpg": _cover_response(size=2000, content_type="image/png"),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping))
    monkeypatch.setattr(requests, "post", _dispatch(mapping))

    record = metadata.fetch_by_isbn(isbn, include_description=True)

    assert record is not None
    assert record.source == "googlebooks"
    assert record.title == "Dune"
    assert record.description == "A desert planet epic."
    # falls back to Google's thumbnail since Open Library had no cover at all.
    assert record.cover_image == b"x" * 2000


def test_fetch_by_isbn_all_sources_empty_returns_none(monkeypatch):
    isbn = "0000000000000"
    mapping = {
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): FakeResponse(status_code=404),
        metadata._GOOGLE_BOOKS_URL: FakeResponse(status_code=200, json_data={"items": []}),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping))
    monkeypatch.setattr(requests, "post", _dispatch(mapping))

    assert metadata.fetch_by_isbn(isbn) is None



def test_fetch_by_isbn_google_429_handled_gracefully(monkeypatch):
    isbn = "9780441172719"
    mapping = {
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): _openlibrary_edition_response(),
        metadata._OPENLIBRARY_RESOURCE_URL.format(key="/authors/OL79034A"): _openlibrary_author_response("Frank Herbert"),
        metadata._OPENLIBRARY_RESOURCE_URL.format(key="/works/OL893414W"): _openlibrary_work_response(),
        metadata._OPENLIBRARY_COVER_URL.format(isbn=isbn): _no_cover_response(),
        metadata._GOOGLE_BOOKS_URL: FakeResponse(status_code=429, json_data={"error": "rate limited"}),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping))
    monkeypatch.setattr(requests, "post", _dispatch(mapping))

    record = metadata.fetch_by_isbn(isbn)

    assert record is not None
    assert record.source == "openlibrary"
    assert record.title == "Dune"


def test_fetch_by_isbn_googlebooks_connection_error_does_not_raise(monkeypatch):
    isbn = "9780441172719"

    def fake_get(url, **kwargs):
        if url.startswith(metadata._GOOGLE_BOOKS_URL):
            raise requests.exceptions.ConnectionError("boom")
        if url == metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn):
            return _openlibrary_edition_response()
        if url == metadata._OPENLIBRARY_RESOURCE_URL.format(key="/authors/OL79034A"):
            return _openlibrary_author_response("Frank Herbert")
        if url == metadata._OPENLIBRARY_RESOURCE_URL.format(key="/works/OL893414W"):
            return _openlibrary_work_response()
        if url.startswith(metadata._OPENLIBRARY_COVER_URL.format(isbn=isbn)):
            return _no_cover_response()
        if url in (metadata._DNB_SRU_URL, metadata._BNF_SRU_URL):
            return _empty_sru_response()
        if url == metadata._OPAC_SBN_URL:
            return _empty_opacsbn_response()
        if url.startswith("https://isbnsearch.org/isbn/"):
            return _empty_isbnsearch_response()
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    record = metadata.fetch_by_isbn(isbn)

    assert record is not None
    assert record.source == "openlibrary"


def test_fetch_by_isbn_openlibrary_error_does_not_kill_googlebooks_data(monkeypatch):
    isbn = "9780441172719"

    def fake_get(url, **kwargs):
        if url == metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn):
            raise requests.exceptions.Timeout("slow")
        if url.startswith(metadata._OPENLIBRARY_COVER_URL.format(isbn=isbn)):
            raise requests.exceptions.Timeout("slow")
        if url.startswith(metadata._GOOGLE_BOOKS_URL):
            return _googlebooks_response(thumbnail="")
        if url in (metadata._DNB_SRU_URL, metadata._BNF_SRU_URL):
            return _empty_sru_response()
        if url == metadata._OPAC_SBN_URL:
            return _empty_opacsbn_response()
        if url.startswith("https://isbnsearch.org/isbn/"):
            return _empty_isbnsearch_response()
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    record = metadata.fetch_by_isbn(isbn)

    assert record is not None
    assert record.source == "googlebooks"
    assert record.title == "Dune"


def test_fetch_by_isbn_strips_non_digits_from_raw_barcode(monkeypatch):
    raw_barcode = "978-0-441-17271-9"
    clean = "9780441172719"

    def fake_get(url, **kwargs):
        if url == metadata._OPENLIBRARY_EDITION_URL.format(isbn=clean):
            return _openlibrary_edition_response()
        if url == metadata._OPENLIBRARY_RESOURCE_URL.format(key="/authors/OL79034A"):
            return _openlibrary_author_response("Frank Herbert")
        if url == metadata._OPENLIBRARY_RESOURCE_URL.format(key="/works/OL893414W"):
            return _openlibrary_work_response()
        if url.startswith(metadata._OPENLIBRARY_COVER_URL.format(isbn=clean)):
            return _no_cover_response()
        if url.startswith(metadata._GOOGLE_BOOKS_URL):
            assert kwargs["params"]["q"] == f"isbn:{clean}"
            return FakeResponse(status_code=200, json_data={"items": []})
        if url == metadata._DNB_SRU_URL:
            assert kwargs["params"]["query"] == f"isbn={clean}"
            return _empty_sru_response()
        if url == metadata._BNF_SRU_URL:
            assert clean in kwargs["params"]["query"]
            return _empty_sru_response()
        if url == metadata._OPAC_SBN_URL:
            return _empty_opacsbn_response()
        if url.startswith("https://isbnsearch.org/isbn/"):
            return _empty_isbnsearch_response()
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    record = metadata.fetch_by_isbn(raw_barcode)

    assert record is not None
    assert record.isbn13 == clean


def test_fetch_by_isbn_empty_input_returns_none():
    assert metadata.fetch_by_isbn("") is None
    assert metadata.fetch_by_isbn("   ") is None


def test_fetch_by_isbn_uses_env_api_key(monkeypatch):
    isbn = "9780441172719"
    monkeypatch.setenv("GOOGLE_BOOKS_API_KEY", "secret-key")

    def fake_get(url, **kwargs):
        if url == metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn):
            return FakeResponse(status_code=404)
        if url.startswith(metadata._GOOGLE_BOOKS_URL):
            assert kwargs["params"]["key"] == "secret-key"
            return _googlebooks_response(thumbnail="")
        if url in (metadata._DNB_SRU_URL, metadata._BNF_SRU_URL):
            return _empty_sru_response()
        if url == metadata._OPAC_SBN_URL:
            return _empty_opacsbn_response()
        if url.startswith("https://isbnsearch.org/isbn/"):
            return _empty_isbnsearch_response()
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    record = metadata.fetch_by_isbn(isbn)
    assert record is not None


def test_fetch_by_isbn_include_description_false_still_merges_other_fields(monkeypatch):
    """The default (include_description=False) must not reduce coverage of any other field --
    only the synopsis text is withheld."""
    isbn = "9780441172719"
    mapping = {
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): FakeResponse(status_code=404),
        metadata._GOOGLE_BOOKS_URL: _googlebooks_response(thumbnail=""),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping))
    monkeypatch.setattr(requests, "post", _dispatch(mapping))

    record = metadata.fetch_by_isbn(isbn, include_description=False)

    assert record is not None
    assert record.title == "Dune"
    assert record.authors == ["Frank Herbert"]
    assert record.publisher == "Ace Books"
    assert record.description == ""


def test_fetch_by_isbn_dnb_contributes_when_others_empty(monkeypatch):
    isbn = "9783827319333"
    mapping = {
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): FakeResponse(status_code=404),
        metadata._GOOGLE_BOOKS_URL: FakeResponse(status_code=200, json_data={"items": []}),
        metadata._DNB_SRU_URL: _sru_dc_response(
            title="Effektiv Java programmieren",
            creator="Bloch, Joshua [Verfasser]",
            publisher="Addison-Wesley",
            date="2002",
        ),
        metadata._BNF_SRU_URL: _empty_sru_response(),
        metadata._OPAC_SBN_URL: _empty_opacsbn_response(),
        "https://isbnsearch.org/isbn/": _empty_isbnsearch_response(),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping, default_empty_sru=False))
    monkeypatch.setattr(requests, "post", _dispatch(mapping, default_empty_sru=False))

    record = metadata.fetch_by_isbn(isbn)

    assert record is not None
    assert record.source == "dnb"
    assert record.title == "Effektiv Java programmieren"
    assert record.authors == ["Joshua Bloch"]
    assert record.publisher == "Addison-Wesley"
    assert record.published_date == "2002"


def test_fetch_by_isbn_bnf_contributes_and_cleans_creator_name(monkeypatch):
    isbn = "9782070360024"
    mapping = {
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): FakeResponse(status_code=404),
        metadata._GOOGLE_BOOKS_URL: FakeResponse(status_code=200, json_data={"items": []}),
        metadata._DNB_SRU_URL: _empty_sru_response(),
        metadata._BNF_SRU_URL: _sru_dc_response(
            title="L'Etranger",
            creator="Camus, Albert (1913-1960). Auteur du texte",
            publisher="Gallimard",
            date="1971",
        ),
        metadata._OPAC_SBN_URL: _empty_opacsbn_response(),
        "https://isbnsearch.org/isbn/": _empty_isbnsearch_response(),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping, default_empty_sru=False))
    monkeypatch.setattr(requests, "post", _dispatch(mapping, default_empty_sru=False))

    record = metadata.fetch_by_isbn(isbn)

    assert record is not None
    assert record.source == "bnf"
    assert record.authors == ["Albert Camus"]


def test_fetch_by_isbn_dnb_and_bnf_error_does_not_kill_other_sources(monkeypatch):
    isbn = "9780441172719"

    def fake_get(url, **kwargs):
        if url in (metadata._DNB_SRU_URL, metadata._BNF_SRU_URL):
            raise requests.exceptions.ConnectionError("boom")
        if url == metadata._OPAC_SBN_URL:
            return _empty_opacsbn_response()
        if url.startswith("https://isbnsearch.org/isbn/"):
            return _empty_isbnsearch_response()
        if url == metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn):
            return _openlibrary_edition_response()
        if url == metadata._OPENLIBRARY_RESOURCE_URL.format(key="/authors/OL79034A"):
            return _openlibrary_author_response("Frank Herbert")
        if url == metadata._OPENLIBRARY_RESOURCE_URL.format(key="/works/OL893414W"):
            return _openlibrary_work_response()
        if url.startswith(metadata._OPENLIBRARY_COVER_URL.format(isbn=isbn)):
            return _no_cover_response()
        if url.startswith(metadata._GOOGLE_BOOKS_URL):
            return FakeResponse(status_code=200, json_data={"items": []})
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    record = metadata.fetch_by_isbn(isbn)

    assert record is not None
    assert record.source == "openlibrary"


def test_fetch_openlibrary_caps_author_lookups(monkeypatch):
    """An edition listing more than `_MAX_AUTHOR_LOOKUPS` authors (e.g. a big edited anthology)
    should only trigger that many follow-up /authors/{key}.json GETs, not one per author -- this
    caps how many sequential round trips one ISBN lookup can cost."""
    isbn = "9780441172719"
    author_keys = [f"/authors/OL{i}A" for i in range(8)]
    calls = {"authors": 0}

    def fake_get(url, **kwargs):
        if url == metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn):
            return _openlibrary_edition_response(author_keys=author_keys, work_keys=())
        if url.startswith("https://openlibrary.org/authors/"):
            calls["authors"] += 1
            return _openlibrary_author_response(f"Author {url}")
        if url.startswith(metadata._OPENLIBRARY_COVER_URL.format(isbn=isbn)):
            return _no_cover_response()
        if url.startswith(metadata._GOOGLE_BOOKS_URL):
            return FakeResponse(status_code=200, json_data={"items": []})
        if url in (metadata._DNB_SRU_URL, metadata._BNF_SRU_URL):
            return _empty_sru_response()
        if url == metadata._OPAC_SBN_URL:
            return _empty_opacsbn_response()
        if url.startswith("https://isbnsearch.org/isbn/"):
            return _empty_isbnsearch_response()
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    record = metadata.fetch_by_isbn(isbn)

    assert record is not None
    assert calls["authors"] == metadata._MAX_AUTHOR_LOOKUPS
    assert len(record.authors) == metadata._MAX_AUTHOR_LOOKUPS


def test_fetch_openlibrary_work_lookup_failure_does_not_lose_other_fields(monkeypatch):
    """If the follow-up /works/{key}.json call (used only for best-effort subjects/categories)
    fails, the rest of the Open Library edition data must still come through."""
    isbn = "9780441172719"

    def fake_get(url, **kwargs):
        if url == metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn):
            return _openlibrary_edition_response()
        if url == metadata._OPENLIBRARY_RESOURCE_URL.format(key="/authors/OL79034A"):
            return _openlibrary_author_response("Frank Herbert")
        if url == metadata._OPENLIBRARY_RESOURCE_URL.format(key="/works/OL893414W"):
            raise requests.exceptions.Timeout("slow")
        if url.startswith(metadata._OPENLIBRARY_COVER_URL.format(isbn=isbn)):
            return _no_cover_response()
        if url.startswith(metadata._GOOGLE_BOOKS_URL):
            return FakeResponse(status_code=200, json_data={"items": []})
        if url in (metadata._DNB_SRU_URL, metadata._BNF_SRU_URL):
            return _empty_sru_response()
        if url == metadata._OPAC_SBN_URL:
            return _empty_opacsbn_response()
        if url.startswith("https://isbnsearch.org/isbn/"):
            return _empty_isbnsearch_response()
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    record = metadata.fetch_by_isbn(isbn)

    assert record is not None
    assert record.title == "Dune"
    assert record.authors == ["Frank Herbert"]
    assert record.categories == []


def test_fetch_sru_dc_malformed_xml_returns_empty(monkeypatch):
    def fake_get(url, **kwargs):
        return FakeResponse(status_code=200, content=b"not xml at all <<<")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    assert metadata._fetch_dnb("9780441172719") == {}


def test_clean_creator_name_reorders_last_first():
    assert metadata._clean_creator_name("Bloch, Joshua [Verfasser]") == "Joshua Bloch"
    assert metadata._clean_creator_name("Camus, Albert (1913-1960). Auteur du texte") == "Albert Camus"
    assert metadata._clean_creator_name("Frank Herbert") == "Frank Herbert"


# ---------------------------------------------------------------------------
# _fetch_opacsbn
# ---------------------------------------------------------------------------


def test_fetch_opacsbn_parses_title_creator_publisher_year(monkeypatch):
    def fake_post(url, **kwargs):
        assert url == metadata._OPAC_SBN_URL
        assert kwargs["data"]["fieldvalue[0]"] == "9788807881114"
        return _opacsbn_response(
            title="In ogni caso nessun rimorso",
            creator="Cacucci, Pino",
            place_publisher_year="Milano : Feltrinelli, 2013",
        )

    monkeypatch.setattr(requests, "post", fake_post)

    result = metadata._fetch_opacsbn("9788807881114")

    assert result["title"] == "In ogni caso nessun rimorso"
    assert result["authors"] == ["Pino Cacucci"]
    assert result["publisher"] == "Feltrinelli"
    assert result["published_date"] == "2013"


def test_fetch_opacsbn_no_hit_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: _empty_opacsbn_response())
    monkeypatch.setattr(requests, "post", lambda url, **kwargs: _empty_opacsbn_response())

    assert metadata._fetch_opacsbn("0000000000000") == {}


def test_fetch_opacsbn_connection_error_returns_empty_dict(monkeypatch):
    def fake_get(url, **kwargs):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    assert metadata._fetch_opacsbn("9788807881114") == {}


def test_fetch_opacsbn_malformed_json_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda url, **kwargs: FakeResponse(status_code=200, content=b"not json at all")
    )

    assert metadata._fetch_opacsbn("9788807881114") == {}


# ---------------------------------------------------------------------------
# _fetch_isbnsearch
# ---------------------------------------------------------------------------


def test_fetch_isbnsearch_parses_title_author_publisher_published(monkeypatch):
    def fake_get(url, **kwargs):
        assert url == metadata._ISBNSEARCH_URL.format(isbn="9788807881114")
        assert kwargs["headers"]["User-Agent"] == metadata._BROWSER_USER_AGENT
        return _isbnsearch_response(
            title="In ogni caso nessun rimorso",
            author="Pino Cacucci",
            publisher="Feltrinelli",
            published_date="2013",
        )

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    result = metadata._fetch_isbnsearch("9788807881114")

    assert result["title"] == "In ogni caso nessun rimorso"
    assert result["authors"] == ["Pino Cacucci"]
    assert result["publisher"] == "Feltrinelli"
    assert result["published_date"] == "2013"


def test_fetch_isbnsearch_404_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: FakeResponse(status_code=404))
    monkeypatch.setattr(requests, "post", lambda url, **kwargs: FakeResponse(status_code=404))

    assert metadata._fetch_isbnsearch("0000000000000") == {}


def test_fetch_isbnsearch_connection_error_returns_empty_dict(monkeypatch):
    def fake_get(url, **kwargs):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    assert metadata._fetch_isbnsearch("9788807881114") == {}


def test_fetch_isbnsearch_botcheck_page_returns_empty_dict(monkeypatch):
    """isbnsearch.org intermittently serves a reCAPTCHA bot-check page (still HTTP 200) instead
    of the real book page -- must be treated as a miss, not parsed as a real title."""
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: _isbnsearch_botcheck_response())
    monkeypatch.setattr(requests, "post", lambda url, **kwargs: _isbnsearch_botcheck_response())

    assert metadata._fetch_isbnsearch("9788807881114") == {}


# ---------------------------------------------------------------------------
# fetch_description
# ---------------------------------------------------------------------------


def test_fetch_description_prefers_googlebooks_when_available(monkeypatch):
    isbn = "9780441172719"

    def fake_get(url, **kwargs):
        if url.startswith(metadata._GOOGLE_BOOKS_URL):
            return _googlebooks_response(description="A desert planet epic.", thumbnail="")
        raise AssertionError(f"unexpected URL requested: {url}")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    assert metadata.fetch_description(isbn) == "A desert planet epic."


def test_fetch_description_falls_back_to_openlibrary_when_googlebooks_misses(monkeypatch):
    """Google Books' free API shares one keyless daily quota across every TechaQ install --
    exhausting it (or any other Google Books outage) must not silently kill synopsis fetching
    entirely, since Open Library's work-level description covers most books too."""
    isbn = "9780441172719"
    mapping = {
        metadata._GOOGLE_BOOKS_URL: FakeResponse(status_code=200, json_data={"items": []}),
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): _openlibrary_edition_response(),
        metadata._OPENLIBRARY_RESOURCE_URL.format(key="/works/OL893414W"): _openlibrary_work_response(
            description="Set on the desert planet Arrakis..."
        ),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping))
    monkeypatch.setattr(requests, "post", _dispatch(mapping))

    assert metadata.fetch_description(isbn) == "Set on the desert planet Arrakis..."


def test_fetch_description_falls_back_to_openlibrary_on_googlebooks_rate_limit(monkeypatch):
    isbn = "9780441172719"
    mapping = {
        metadata._GOOGLE_BOOKS_URL: FakeResponse(status_code=429, json_data={"error": "rate limited"}),
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): _openlibrary_edition_response(),
        metadata._OPENLIBRARY_RESOURCE_URL.format(key="/works/OL893414W"): _openlibrary_work_response(
            description="Set on the desert planet Arrakis..."
        ),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping))
    monkeypatch.setattr(requests, "post", _dispatch(mapping))

    assert metadata.fetch_description(isbn) == "Set on the desert planet Arrakis..."


def test_fetch_description_openlibrary_description_as_dict_value(monkeypatch):
    """Open Library sometimes returns description as {"type": "/type/text", "value": "..."}
    rather than a plain string -- both shapes appear across editions/works."""
    isbn = "9780441172719"
    mapping = {
        metadata._GOOGLE_BOOKS_URL: FakeResponse(status_code=200, json_data={"items": []}),
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): _openlibrary_edition_response(),
        metadata._OPENLIBRARY_RESOURCE_URL.format(key="/works/OL893414W"): _openlibrary_work_response(
            description={"type": "/type/text", "value": "Set on the desert planet Arrakis..."}
        ),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping))
    monkeypatch.setattr(requests, "post", _dispatch(mapping))

    assert metadata.fetch_description(isbn) == "Set on the desert planet Arrakis..."


def test_fetch_description_no_hit_returns_empty_string(monkeypatch):
    def fake_get(url, **kwargs):
        return FakeResponse(status_code=200, json_data={"items": []})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    assert metadata.fetch_description("9780441172719") == ""


def test_fetch_description_empty_input_returns_empty_string():
    assert metadata.fetch_description("") == ""


def _wikipedia_search_response(titles):
    return FakeResponse(json_data={"query": {"search": [{"title": t} for t in titles]}})


def _wikipedia_summary_response(extract):
    return FakeResponse(json_data={"extract": extract})


def test_fetch_description_rejects_implausible_openlibrary_description(monkeypatch):
    """Open Library's crowd-sourced description field is sometimes a single stray word
    (observed live: "Excellent" for a Celine novel) -- that must not win over a real
    Wikipedia summary just because it's technically non-empty."""
    isbn = "9788879720175"
    mapping = {
        metadata._GOOGLE_BOOKS_URL: FakeResponse(status_code=200, json_data={"items": []}),
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): _openlibrary_edition_response(),
        metadata._OPENLIBRARY_RESOURCE_URL.format(key="/works/OL893414W"): _openlibrary_work_response(
            description="Excellent"
        ),
        metadata._WIKIPEDIA_SEARCH_URL.format(lang="it"): _wikipedia_search_response(
            ["Viaggio al termine della notte"]
        ),
        metadata._WIKIPEDIA_SUMMARY_URL.format(
            lang="it", title="Viaggio_al_termine_della_notte"
        ): _wikipedia_summary_response(
            "Viaggio al termine della notte è il primo romanzo di Louis-Ferdinand Celine."
        ),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping))
    monkeypatch.setattr(requests, "post", _dispatch(mapping))

    assert (
        metadata.fetch_description(isbn, "Viaggio al termine della notte", "Louis-Ferdinand Celine")
        == "Viaggio al termine della notte è il primo romanzo di Louis-Ferdinand Celine."
    )


def test_fetch_description_falls_back_to_wikipedia_when_others_miss(monkeypatch):
    isbn = "9780441172719"
    mapping = {
        metadata._GOOGLE_BOOKS_URL: FakeResponse(status_code=200, json_data={"items": []}),
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): _openlibrary_edition_response(),
        metadata._OPENLIBRARY_RESOURCE_URL.format(key="/works/OL893414W"): _openlibrary_work_response(
            description=""
        ),
        metadata._WIKIPEDIA_SEARCH_URL.format(lang="it"): _wikipedia_search_response(["Dune"]),
        metadata._WIKIPEDIA_SUMMARY_URL.format(lang="it", title="Dune"): _wikipedia_summary_response(
            "Set on the desert planet Arrakis..."
        ),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping))
    monkeypatch.setattr(requests, "post", _dispatch(mapping))

    assert metadata.fetch_description(isbn, "Dune", "Frank Herbert") == "Set on the desert planet Arrakis..."


def test_fetch_description_no_title_skips_wikipedia(monkeypatch):
    """fetch_description's default title="" must not attempt a Wikipedia search at all --
    callers without a title (e.g. fetch_by_isbn before a title is known) should just get ""
    rather than an assertion error from an unexpected Wikipedia request."""
    isbn = "9780441172719"
    mapping = {
        metadata._GOOGLE_BOOKS_URL: FakeResponse(status_code=200, json_data={"items": []}),
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): _openlibrary_edition_response(),
        metadata._OPENLIBRARY_RESOURCE_URL.format(key="/works/OL893414W"): _openlibrary_work_response(
            description=""
        ),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping))
    monkeypatch.setattr(requests, "post", _dispatch(mapping))

    assert metadata.fetch_description(isbn) == ""


def test_fetch_description_wikipedia_rejects_mismatched_title(monkeypatch):
    """Regression guard for the live-observed false positive: searching "Il treno di
    mezzanotte" returned only the unrelated "Segretissimo" as a result -- without
    _title_matches verifying the candidate title, this would have been trusted."""
    isbn = "9788833579931"
    mapping = {
        metadata._GOOGLE_BOOKS_URL: FakeResponse(status_code=200, json_data={"items": []}),
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): FakeResponse(status_code=404),
        metadata._WIKIPEDIA_SEARCH_URL.format(lang="it"): _wikipedia_search_response(["Segretissimo"]),
        metadata._WIKIPEDIA_SEARCH_URL.format(lang="en"): _wikipedia_search_response([]),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping))
    monkeypatch.setattr(requests, "post", _dispatch(mapping))

    assert metadata.fetch_description(isbn, "Il treno di mezzanotte") == ""


def test_fetch_description_wikipedia_falls_back_to_english(monkeypatch):
    isbn = "9781933372358"
    mapping = {
        metadata._GOOGLE_BOOKS_URL: FakeResponse(status_code=200, json_data={"items": []}),
        metadata._OPENLIBRARY_EDITION_URL.format(isbn=isbn): FakeResponse(status_code=404),
        metadata._WIKIPEDIA_SEARCH_URL.format(lang="it"): _wikipedia_search_response([]),
        metadata._WIKIPEDIA_SEARCH_URL.format(lang="en"): _wikipedia_search_response(["The Lost Sailors"]),
        metadata._WIKIPEDIA_SUMMARY_URL.format(
            lang="en", title="The_Lost_Sailors"
        ): _wikipedia_summary_response("A novel about sailors stranded in a Caribbean port."),
    }
    monkeypatch.setattr(requests, "get", _dispatch(mapping))
    monkeypatch.setattr(requests, "post", _dispatch(mapping))

    assert (
        metadata.fetch_description(isbn, "The Lost Sailors")
        == "A novel about sailors stranded in a Caribbean port."
    )


@pytest.mark.parametrize(
    "query_title, candidate_title, expected",
    [
        ("Marinai perduti", "Marinai perduti", True),
        ("Il treno di mezzanotte", "Segretissimo", False),
        ("The Lost Sailors", "The Lost Sailors (novel)", True),
        ("Dune", "Dune Messiah", True),
        ("", "Dune", False),
        ("Il treno di mezzanotte", "", False),
    ],
)
def test_title_matches(query_title, candidate_title, expected):
    assert metadata._title_matches(query_title, candidate_title) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Excellent", False),
        ("Set on the desert planet Arrakis...", True),
        ("", False),
        ("A", False),
    ],
)
def test_is_plausible_description(text, expected):
    assert metadata._is_plausible_description(text) is expected


def test_fetch_wikipedia_summary_never_raises_on_network_error(monkeypatch):
    def fake_get(url, **kwargs):
        raise requests.exceptions.ConnectionError("no network")

    monkeypatch.setattr(requests, "get", fake_get)

    assert metadata._fetch_wikipedia_summary("Dune", "Frank Herbert") == ""


def test_fetch_wikipedia_summary_empty_title_returns_empty_string():
    assert metadata._fetch_wikipedia_summary("") == ""


# ---------------------------------------------------------------------------
# search_by_title_author
# ---------------------------------------------------------------------------


def test_search_by_title_author_parses_docs(monkeypatch):
    def fake_get(url, **kwargs):
        if url.startswith(metadata._OPENLIBRARY_SEARCH_URL):
            assert "Dune" in kwargs["params"]["q"]
            return FakeResponse(
                json_data={
                    "docs": [
                        {
                            "title": "Dune",
                            "author_name": ["Frank Herbert"],
                            "first_publish_year": 1965,
                            "isbn": ["9780441172719", "0441172717"],
                            "cover_i": 12345,
                        }
                    ]
                }
            )
        if url.startswith(metadata._OPENLIBRARY_COVER_BY_ID_URL.format(cover_id=12345)):
            return _cover_response(size=3000)
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    results = metadata.search_by_title_author("Dune", "Frank Herbert")

    assert len(results) == 1
    book = results[0]
    assert book.title == "Dune"
    assert book.authors == ["Frank Herbert"]
    assert book.published_date == "1965"
    assert book.isbn13 == "9780441172719"
    assert book.cover_image == b"x" * 3000
    assert book.source == "openlibrary"


def test_search_by_title_author_no_cover_id_skips_download(monkeypatch):
    def fake_get(url, **kwargs):
        if url.startswith(metadata._OPENLIBRARY_SEARCH_URL):
            return FakeResponse(json_data={"docs": [{"title": "Foundation", "author_name": []}]})
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    results = metadata.search_by_title_author("Foundation")

    assert len(results) == 1
    assert results[0].cover_image is None


def test_search_by_title_author_returns_empty_list_on_error(monkeypatch):
    def fake_get(url, **kwargs):
        raise requests.exceptions.ConnectionError("network down")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    assert metadata.search_by_title_author("Dune") == []


def test_search_by_title_author_returns_empty_list_on_bad_json(monkeypatch):
    def fake_get(url, **kwargs):
        return FakeResponse(status_code=200, json_data=None)  # .json() raises ValueError

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    assert metadata.search_by_title_author("Dune") == []


def test_search_by_title_author_empty_query_returns_empty_list():
    assert metadata.search_by_title_author("", "") == []


def test_search_by_title_author_http_error_status_returns_empty_list(monkeypatch):
    def fake_get(url, **kwargs):
        return FakeResponse(status_code=500, json_data={})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_get)

    assert metadata.search_by_title_author("Dune") == []
