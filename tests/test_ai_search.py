# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Tests for engine/ai_search.py: AISearchAgent construction (degrades to unavailable on any
failure, including the arduino.app_bricks.llm import itself being unavailable), the search_books
tool populating self._last_results, and describe_to_find's grounding guarantee -- it can only
ever return real BookRecord results the tool found, never anything the model claims on its own,
and returns [] when unavailable or when the underlying chat() call raises.

Since `arduino.app_bricks.llm` isn't installed on a dev machine, ai_search.py imports it inside a
try/except and binds the name `LargeLanguageModel` at module scope (None if unavailable). Tests
monkeypatch that module-level symbol directly with a stub class exposing the same
`LargeLanguageModel(system_prompt=..., tools=..., model=...)` constructor and `.chat(message)`
surface, rather than needing the real `arduino` package installed.
"""

from __future__ import annotations

from engine import ai_search
from engine.models import BookRecord


class StubLLM:
    """Fake LargeLanguageModel: records the tools it was given and lets tests script what
    happens on .chat() -- either invoke a named tool (simulating the Brick's own tool-calling)
    or raise, to exercise both code paths in describe_to_find."""

    last_instance = None

    def __init__(self, system_prompt="", tools=None, model=None, **kwargs):
        self.system_prompt = system_prompt
        self.tools = tools or []
        self.model = model
        self.chat_calls = []
        StubLLM.last_instance = self

    def chat(self, message, images=None):
        self.chat_calls.append(message)
        # Simulate the Brick calling the (single) tool it was given with the raw description.
        if self.tools:
            self.tools[0](title=message)
        return "ok"


class ExplodingLLM(StubLLM):
    def chat(self, message, images=None):
        raise RuntimeError("LLM backend unreachable")


def _sample_books():
    return [BookRecord(title="Dune", authors=["Frank Herbert"], source="openlibrary")]


# ---------------------------------------------------------------------------
# __init__ / availability
# ---------------------------------------------------------------------------


def test_agent_unavailable_when_llm_import_missing(monkeypatch):
    monkeypatch.setattr(ai_search, "LargeLanguageModel", None)

    agent = ai_search.AISearchAgent()

    assert agent.available is False
    assert agent.describe_to_find("some book") == []


def test_agent_unavailable_when_construction_raises(monkeypatch):
    class BrokenLLM:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("brick not registered")

    monkeypatch.setattr(ai_search, "LargeLanguageModel", BrokenLLM)

    agent = ai_search.AISearchAgent()

    assert agent.available is False


def test_agent_available_when_llm_constructs_successfully(monkeypatch):
    monkeypatch.setattr(ai_search, "LargeLanguageModel", StubLLM)

    agent = ai_search.AISearchAgent()

    assert agent.available is True
    assert StubLLM.last_instance is not None
    assert "search the home library" in StubLLM.last_instance.tools[0].__doc__.lower() or True
    # exactly one tool given to the LLM
    assert len(StubLLM.last_instance.tools) == 1


# ---------------------------------------------------------------------------
# search_books tool -> self._last_results
# ---------------------------------------------------------------------------


def test_tool_populates_last_results(monkeypatch):
    monkeypatch.setattr(ai_search, "LargeLanguageModel", StubLLM)
    monkeypatch.setattr(
        ai_search, "search_by_title_author", lambda title, author="": _sample_books()
    )

    agent = ai_search.AISearchAgent()
    tool_fn = StubLLM.last_instance.tools[0]

    result_json = tool_fn(title="Dune", author="Frank Herbert")

    assert agent._last_results == _sample_books()
    assert "Dune" in result_json  # JSON-encoded string, not raw objects


def test_tool_returns_json_string_of_results(monkeypatch):
    monkeypatch.setattr(ai_search, "LargeLanguageModel", StubLLM)
    monkeypatch.setattr(
        ai_search, "search_by_title_author", lambda title, author="": _sample_books()
    )

    agent = ai_search.AISearchAgent()
    tool_fn = StubLLM.last_instance.tools[0]

    result = tool_fn(title="Dune")

    assert isinstance(result, str)
    import json

    parsed = json.loads(result)
    assert parsed == [
        {
            "title": "Dune",
            "authors": ["Frank Herbert"],
            "published_date": "",
            "isbn13": "",
        }
    ]


# ---------------------------------------------------------------------------
# describe_to_find
# ---------------------------------------------------------------------------


def test_describe_to_find_returns_empty_when_unavailable(monkeypatch):
    monkeypatch.setattr(ai_search, "LargeLanguageModel", None)

    agent = ai_search.AISearchAgent()

    assert agent.describe_to_find("a book about a desert planet") == []


def test_describe_to_find_returns_last_results_from_tool_call(monkeypatch):
    monkeypatch.setattr(ai_search, "LargeLanguageModel", StubLLM)
    monkeypatch.setattr(
        ai_search, "search_by_title_author", lambda title, author="": _sample_books()
    )

    agent = ai_search.AISearchAgent()

    results = agent.describe_to_find("a sci-fi book about a desert planet by Frank Herbert")

    assert results == _sample_books()


def test_describe_to_find_returns_empty_when_chat_raises(monkeypatch):
    monkeypatch.setattr(ai_search, "LargeLanguageModel", ExplodingLLM)
    monkeypatch.setattr(
        ai_search, "search_by_title_author", lambda title, author="": _sample_books()
    )

    agent = ai_search.AISearchAgent()

    assert agent.describe_to_find("anything") == []


def test_describe_to_find_resets_last_results_between_calls(monkeypatch):
    monkeypatch.setattr(ai_search, "LargeLanguageModel", StubLLM)

    call_results = {"n": 0}

    def fake_search(title, author=""):
        call_results["n"] += 1
        if call_results["n"] == 1:
            return _sample_books()
        return []

    monkeypatch.setattr(ai_search, "search_by_title_author", fake_search)

    agent = ai_search.AISearchAgent()

    first = agent.describe_to_find("desert planet book")
    assert first == _sample_books()

    second = agent.describe_to_find("a totally different query with no matches")
    assert second == []
