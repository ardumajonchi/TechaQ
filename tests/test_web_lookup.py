# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Tests for engine/web_lookup.py: _search_ddg_lite (HTML result-scraping, graceful degradation
to "" on bot-check/error responses), and WebMetadataFallback (LLM construction degrading to
unavailable exactly like AISearchAgent, lookup() returning a clean {"title","author"} guess or {}
on any failure/no-signal step).

Since `arduino.app_bricks.llm` isn't installed on a dev machine, web_lookup.py imports it inside a
try/except and binds the name `LargeLanguageModel` at module scope (None if unavailable). Tests
monkeypatch that module-level symbol directly with a stub class exposing the same
`LargeLanguageModel(system_prompt=..., model=...)` constructor and `.chat(message)` surface,
mirroring tests/test_ai_search.py's StubLLM.
"""

from __future__ import annotations

import json

import requests

from engine import web_lookup


class StubLLM:
    last_instance = None

    def __init__(self, system_prompt="", model=None, **kwargs):
        self.system_prompt = system_prompt
        self.model = model
        self.chat_calls = []
        self._response = kwargs.get("response", "")
        StubLLM.last_instance = self

    def chat(self, message):
        self.chat_calls.append(message)
        return self._response


def make_stub_llm(response: str):
    def factory(system_prompt="", model=None, **kwargs):
        return StubLLM(system_prompt=system_prompt, model=model, response=response)

    return factory


class ExplodingLLM:
    def __init__(self, *args, **kwargs):
        pass

    def chat(self, message):
        raise RuntimeError("LLM backend unreachable")


_REAL_RESULTS_HTML = """
<html><body>
<a class="result-link" href="https://example.com/1">Dune by Frank Herbert - Goodreads</a>
<td class="result-snippet">Dune is a 1965 science fiction novel by Frank Herbert.</td>
<a class="result-link" href="https://example.com/2">Dune (novel) - Wikipedia</a>
<td class="result-snippet">Dune tells the story of Paul Atreides.</td>
</body></html>
"""

_BOT_CHECK_HTML = """
<html><body><h1>Anomaly detected</h1><p>Please verify you are human.</p></body></html>
"""


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


# ---------------------------------------------------------------------------
# _search_ddg_lite
# ---------------------------------------------------------------------------


def test_search_ddg_lite_parses_titles_and_snippets(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        assert url == web_lookup._DDG_LITE_URL
        assert params == {"q": "isbn 9780441172719 book"}
        return FakeResponse(status_code=200, text=_REAL_RESULTS_HTML)

    monkeypatch.setattr(requests, "get", fake_get)
    result = web_lookup._search_ddg_lite("isbn 9780441172719 book")
    assert "Dune by Frank Herbert - Goodreads" in result
    assert "Dune is a 1965 science fiction novel by Frank Herbert." in result
    assert "Paul Atreides" in result


def test_search_ddg_lite_bot_check_page_returns_empty_string(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(status_code=200, text=_BOT_CHECK_HTML))
    assert web_lookup._search_ddg_lite("anything") == ""


def test_search_ddg_lite_non_200_returns_empty_string(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(status_code=202, text=_BOT_CHECK_HTML))
    assert web_lookup._search_ddg_lite("anything") == ""


def test_search_ddg_lite_connection_error_returns_empty_string(monkeypatch):
    def fake_get(*a, **k):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(requests, "get", fake_get)
    assert web_lookup._search_ddg_lite("anything") == ""


def test_search_ddg_lite_timeout_returns_empty_string(monkeypatch):
    def fake_get(*a, **k):
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(requests, "get", fake_get)
    assert web_lookup._search_ddg_lite("anything") == ""


def test_search_ddg_lite_uses_browser_user_agent(monkeypatch):
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["headers"] = headers
        return FakeResponse(status_code=200, text="")

    monkeypatch.setattr(requests, "get", fake_get)
    web_lookup._search_ddg_lite("anything")
    assert "Mozilla" in seen["headers"]["User-Agent"]


# ---------------------------------------------------------------------------
# WebMetadataFallback construction / availability
# ---------------------------------------------------------------------------


def test_fallback_unavailable_when_llm_import_missing(monkeypatch):
    monkeypatch.setattr(web_lookup, "LargeLanguageModel", None)
    fallback = web_lookup.WebMetadataFallback()
    assert fallback.available is False
    assert fallback.lookup("9780441172719") == {}


def test_fallback_unavailable_when_construction_raises(monkeypatch):
    class BrokenLLM:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("brick not registered")

    monkeypatch.setattr(web_lookup, "LargeLanguageModel", BrokenLLM)
    fallback = web_lookup.WebMetadataFallback()
    assert fallback.available is False


def test_fallback_available_when_llm_constructs_successfully(monkeypatch):
    monkeypatch.setattr(web_lookup, "LargeLanguageModel", StubLLM)
    fallback = web_lookup.WebMetadataFallback()
    assert fallback.available is True


# ---------------------------------------------------------------------------
# WebMetadataFallback.lookup
# ---------------------------------------------------------------------------


def test_lookup_returns_clean_guess_from_llm_json(monkeypatch):
    monkeypatch.setattr(
        web_lookup, "LargeLanguageModel", make_stub_llm(json.dumps({"title": "Dune", "author": "Frank Herbert"}))
    )
    monkeypatch.setattr(web_lookup, "_search_ddg_lite", lambda query: "some real snippet text about Dune")

    fallback = web_lookup.WebMetadataFallback()
    assert fallback.lookup("9780441172719") == {"title": "Dune", "author": "Frank Herbert"}


def test_lookup_strips_markdown_code_fences(monkeypatch):
    canned = "```json\n" + json.dumps({"title": "Neuromancer", "author": "William Gibson"}) + "\n```"
    monkeypatch.setattr(web_lookup, "LargeLanguageModel", make_stub_llm(canned))
    monkeypatch.setattr(web_lookup, "_search_ddg_lite", lambda query: "snippet text")

    fallback = web_lookup.WebMetadataFallback()
    assert fallback.lookup("9780441172719") == {"title": "Neuromancer", "author": "William Gibson"}


def test_lookup_no_snippets_returns_empty_dict_without_calling_llm(monkeypatch):
    monkeypatch.setattr(web_lookup, "LargeLanguageModel", StubLLM)
    monkeypatch.setattr(web_lookup, "_search_ddg_lite", lambda query: "")

    fallback = web_lookup.WebMetadataFallback()
    assert fallback.lookup("9780441172719") == {}
    assert StubLLM.last_instance.chat_calls == []


def test_lookup_llm_says_unidentifiable_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(web_lookup, "LargeLanguageModel", make_stub_llm(json.dumps({"title": "", "author": ""})))
    monkeypatch.setattr(web_lookup, "_search_ddg_lite", lambda query: "irrelevant noise")

    fallback = web_lookup.WebMetadataFallback()
    assert fallback.lookup("9780441172719") == {}


def test_lookup_malformed_json_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(web_lookup, "LargeLanguageModel", make_stub_llm("not json at all {["))
    monkeypatch.setattr(web_lookup, "_search_ddg_lite", lambda query: "snippet text")

    fallback = web_lookup.WebMetadataFallback()
    assert fallback.lookup("9780441172719") == {}


def test_lookup_non_dict_json_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(web_lookup, "LargeLanguageModel", make_stub_llm(json.dumps(["not", "a", "dict"])))
    monkeypatch.setattr(web_lookup, "_search_ddg_lite", lambda query: "snippet text")

    fallback = web_lookup.WebMetadataFallback()
    assert fallback.lookup("9780441172719") == {}


def test_lookup_llm_chat_raises_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(web_lookup, "LargeLanguageModel", ExplodingLLM)
    monkeypatch.setattr(web_lookup, "_search_ddg_lite", lambda query: "snippet text")

    fallback = web_lookup.WebMetadataFallback()
    assert fallback.lookup("9780441172719") == {}


def test_lookup_unavailable_returns_empty_dict_without_scraping(monkeypatch):
    monkeypatch.setattr(web_lookup, "LargeLanguageModel", None)
    calls = {"n": 0}

    def fake_search(query):
        calls["n"] += 1
        return "snippet text"

    monkeypatch.setattr(web_lookup, "_search_ddg_lite", fake_search)

    fallback = web_lookup.WebMetadataFallback()
    assert fallback.lookup("9780441172719") == {}
    assert calls["n"] == 0
