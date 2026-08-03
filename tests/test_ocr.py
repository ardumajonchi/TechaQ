# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Tests for engine/ocr.py: extract_candidates (LLM JSON parsing, code-fence stripping, heuristic
fallback), preprocess_image (rotation variant count + validity), and call_ocr_service (graceful
degradation to "" on connection errors/timeouts, mocking requests.post).
"""

from __future__ import annotations

import io
import json

import pytest
import requests
from PIL import Image

from engine import ocr


class StubLLM:
    """Fake LLM exposing the same .chat(message: str) -> str surface as
    arduino.app_bricks.llm.LargeLanguageModel, returning a canned response."""

    def __init__(self, response: str):
        self._response = response

    def chat(self, message: str) -> str:
        return self._response


def _sample_image_bytes(size=(40, 20), color=(255, 255, 255)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# extract_candidates
# ---------------------------------------------------------------------------


def test_extract_candidates_parses_valid_llm_json():
    canned = json.dumps([
        {"title": "Dune", "author": "Frank Herbert"},
        {"title": "Foundation", "author": ""},
    ])
    llm = StubLLM(canned)
    result = ocr.extract_candidates("dune frnk herb fondat", llm=llm)
    assert result == [
        {"title": "Dune", "author": "Frank Herbert"},
        {"title": "Foundation", "author": ""},
    ]


def test_extract_candidates_strips_markdown_code_fences():
    canned = "```json\n" + json.dumps([{"title": "Neuromancer", "author": "William Gibson"}]) + "\n```"
    llm = StubLLM(canned)
    result = ocr.extract_candidates("some ocr noise", llm=llm)
    assert result == [{"title": "Neuromancer", "author": "William Gibson"}]


def test_extract_candidates_bare_code_fence_no_language_tag():
    canned = "```\n" + json.dumps([{"title": "Snow Crash", "author": ""}]) + "\n```"
    llm = StubLLM(canned)
    result = ocr.extract_candidates("noise", llm=llm)
    assert result == [{"title": "Snow Crash", "author": ""}]


def test_extract_candidates_malformed_json_returns_empty_list():
    llm = StubLLM("this is not json at all {[")
    result = ocr.extract_candidates("noise", llm=llm)
    assert result == []


def test_extract_candidates_non_list_json_returns_empty_list():
    llm = StubLLM(json.dumps({"title": "not a list"}))
    result = ocr.extract_candidates("noise", llm=llm)
    assert result == []


def test_extract_candidates_skips_items_without_title():
    canned = json.dumps([
        {"author": "no title here"},
        {"title": "Has Title", "author": "Someone"},
        "not even a dict",
    ])
    llm = StubLLM(canned)
    result = ocr.extract_candidates("noise", llm=llm)
    assert result == [{"title": "Has Title", "author": "Someone"}]


def test_extract_candidates_llm_chat_raises_falls_back_to_heuristic():
    class ExplodingLLM:
        def chat(self, message: str) -> str:
            raise RuntimeError("LLM unavailable")

    raw_text = "Dune II\nFoundation\nabc\n"
    result = ocr.extract_candidates(raw_text, llm=ExplodingLLM())
    # falls back to heuristic: lines over ~4 chars become naive title guesses
    assert {"title": "Dune II", "author": ""} in result
    assert {"title": "Foundation", "author": ""} in result
    assert not any(c["title"] == "abc" for c in result)


def test_extract_candidates_no_llm_uses_heuristic_fallback():
    raw_text = "The Hobbit\nab\nFoundation and Empire\n\n   \nOK"
    result = ocr.extract_candidates(raw_text, llm=None)
    titles = [c["title"] for c in result]
    assert "The Hobbit" in titles
    assert "Foundation and Empire" in titles
    # short/blank lines (<=4 chars, or empty/whitespace-only) are skipped
    assert "ab" not in titles
    assert "OK" not in titles
    assert all(c["author"] == "" for c in result)


def test_extract_candidates_empty_raw_text_returns_empty_list():
    assert ocr.extract_candidates("", llm=StubLLM("[]")) == []
    assert ocr.extract_candidates("   \n  ", llm=None) == []


# ---------------------------------------------------------------------------
# preprocess_image
# ---------------------------------------------------------------------------


def test_preprocess_image_returns_three_rotation_variants():
    variants = ocr.preprocess_image(_sample_image_bytes())
    assert len(variants) == 3


def test_preprocess_image_variants_are_valid_images():
    variants = ocr.preprocess_image(_sample_image_bytes())
    for variant_bytes in variants:
        img = Image.open(io.BytesIO(variant_bytes))
        img.load()
        assert img.size[0] > 0 and img.size[1] > 0


def test_preprocess_image_rotated_variants_swap_dimensions():
    width, height = 40, 20
    variants = ocr.preprocess_image(_sample_image_bytes(size=(width, height)))
    sizes = [Image.open(io.BytesIO(v)).size for v in variants]
    # first variant (0 degrees) keeps original orientation
    assert sizes[0] == (width, height)
    # 90/270-degree variants should have swapped width/height (expand=True)
    assert sizes[1] == (height, width)
    assert sizes[2] == (height, width)


def test_preprocess_image_invalid_input_returns_empty_list():
    assert ocr.preprocess_image(b"not an image, just garbage bytes") == []


# ---------------------------------------------------------------------------
# call_ocr_service
# ---------------------------------------------------------------------------


def test_call_ocr_service_success(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"text": "Dune Foundation"}

    def fake_post(url, data=None, timeout=None):
        assert url == "http://ocr_runtime:6098/ocr"
        assert data == b"imgbytes"
        return FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)
    result = ocr.call_ocr_service(b"imgbytes")
    assert result == "Dune Foundation"


def test_call_ocr_service_connection_error_returns_empty_string(monkeypatch):
    def fake_post(url, data=None, timeout=None):
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr(requests, "post", fake_post)
    result = ocr.call_ocr_service(b"imgbytes")
    assert result == ""


def test_call_ocr_service_timeout_returns_empty_string(monkeypatch):
    def fake_post(url, data=None, timeout=None):
        raise requests.exceptions.Timeout("timed out")

    monkeypatch.setattr(requests, "post", fake_post)
    result = ocr.call_ocr_service(b"imgbytes")
    assert result == ""


def test_call_ocr_service_non_200_returns_empty_string(monkeypatch):
    class FakeResponse:
        status_code = 500
        text = "internal error"

        def json(self):
            return {"error": "tesseract failed"}

    def fake_post(url, data=None, timeout=None):
        return FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)
    result = ocr.call_ocr_service(b"imgbytes")
    assert result == ""


def test_call_ocr_service_malformed_json_returns_empty_string(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            raise ValueError("not json")

    def fake_post(url, data=None, timeout=None):
        return FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)
    result = ocr.call_ocr_service(b"imgbytes")
    assert result == ""


# ---------------------------------------------------------------------------
# process_shelf_photo (light integration of the pieces above)
# ---------------------------------------------------------------------------


def test_process_shelf_photo_picks_longest_variant_text_and_extracts(monkeypatch):
    call_count = {"n": 0}

    def fake_call_ocr_service(image_bytes, host=ocr.DEFAULT_OCR_HOST, port=ocr.DEFAULT_OCR_PORT):
        call_count["n"] += 1
        # second variant "wins" with the longest text
        return "short" if call_count["n"] != 2 else "Dune II\nFoundation"

    monkeypatch.setattr(ocr, "call_ocr_service", fake_call_ocr_service)

    result = ocr.process_shelf_photo(_sample_image_bytes(), llm=None)
    assert call_count["n"] == 3
    titles = [c["title"] for c in result]
    assert "Dune II" in titles
    assert "Foundation" in titles


def test_process_shelf_photo_unreachable_service_returns_empty_list(monkeypatch):
    monkeypatch.setattr(ocr, "call_ocr_service", lambda *a, **k: "")
    result = ocr.process_shelf_photo(_sample_image_bytes(), llm=None)
    assert result == []


def test_process_shelf_photo_invalid_image_returns_empty_list():
    result = ocr.process_shelf_photo(b"garbage, not an image", llm=None)
    assert result == []


# ---------------------------------------------------------------------------
# extract_isbn_candidates / process_isbn_photo
# ---------------------------------------------------------------------------


def test_extract_isbn_candidates_finds_13_digit_sequence():
    result = ocr.extract_isbn_candidates("some noise 9780134685991 more noise")
    assert result == ["9780134685991"]


def test_extract_isbn_candidates_finds_10_digit_sequence():
    result = ocr.extract_isbn_candidates("blah 0134685997 blah")
    assert result == ["0134685997"]


def test_extract_isbn_candidates_normalizes_hyphens_and_spaces():
    result = ocr.extract_isbn_candidates("ISBN 978-0-13-468599-1")
    assert result == ["9780134685991"]


def test_extract_isbn_candidates_no_digits_returns_empty_list():
    assert ocr.extract_isbn_candidates("no digits here at all") == []


def test_extract_isbn_candidates_empty_string_returns_empty_list():
    assert ocr.extract_isbn_candidates("") == []


def test_extract_isbn_candidates_dedupes_and_prefers_978_979_prefix():
    text = "0134685997\n9780134685991\n9780134685991\n9791234567896"
    result = ocr.extract_isbn_candidates(text)
    assert result[0] in ("9780134685991", "9791234567896")
    assert set(result) == {"0134685997", "9780134685991", "9791234567896"}
    assert result.count("9780134685991") == 1


def test_process_isbn_photo_picks_longest_variant_and_extracts(monkeypatch):
    call_count = {"n": 0}

    def fake_call_ocr_service(image_bytes, host=ocr.DEFAULT_OCR_HOST, port=ocr.DEFAULT_OCR_PORT):
        call_count["n"] += 1
        return "5" if call_count["n"] != 2 else "9780134685991"

    monkeypatch.setattr(ocr, "call_ocr_service", fake_call_ocr_service)

    result = ocr.process_isbn_photo(_sample_image_bytes())
    assert call_count["n"] == 3
    assert result == ["9780134685991"]


def test_process_isbn_photo_unreachable_service_returns_empty_list(monkeypatch):
    monkeypatch.setattr(ocr, "call_ocr_service", lambda *a, **k: "")
    result = ocr.process_isbn_photo(_sample_image_bytes())
    assert result == []


def test_process_isbn_photo_invalid_image_returns_empty_list():
    result = ocr.process_isbn_photo(b"garbage, not an image")
    assert result == []
