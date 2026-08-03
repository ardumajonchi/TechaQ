# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Shelf-spine OCR pipeline: preprocess a shelf photo, send it to the ocr_runtime Brick's
Tesseract HTTP service, and turn the noisy raw OCR text into a list of {"title","author"}
candidate guesses for the user to confirm.

Every stage here is defensive -- a missing/unreachable ocr_runtime container, a malformed image,
or an LLM that returns garbage must degrade gracefully (empty text / heuristic fallback / empty
candidate list) rather than ever raising out of this module and taking the main app down.

HTTP contract with the ocr_runtime Brick (see bricks/ocr_runtime/server.py):
  POST http://{host}:{port}/ocr
    Body: raw image bytes (NOT multipart/form-data -- the request body IS the image).
    Response: {"text": "<raw tesseract stdout>"} on 200, {"error": "..."} on non-200.
"""

from __future__ import annotations

import io
import json
import logging

import requests
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

DEFAULT_OCR_HOST = "ocr_runtime"
DEFAULT_OCR_PORT = 6098
OCR_REQUEST_TIMEOUT_S = 15

# Rotations tried in addition to the original orientation -- book spines on a shelf are usually
# vertical text, so rotating the whole shelf photo makes spine text horizontal for OCR.
_ROTATIONS = (0, 90, 270)


def preprocess_image(image_bytes: bytes) -> list[bytes]:
    """Grayscale + autocontrast the image, then return re-encoded JPEG byte variants for the
    original orientation plus 90 and 270 degree rotations (3 variants total).

    Returns an empty list if the input isn't a decodable image, rather than raising.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception as exc:
        logger.error("preprocess_image: failed to decode input image: %r", exc)
        return []

    variants: list[bytes] = []
    for angle in _ROTATIONS:
        try:
            frame = img.convert("L")  # grayscale
            frame = ImageOps.autocontrast(frame)
            if angle:
                frame = frame.rotate(angle, expand=True)
            buf = io.BytesIO()
            frame.save(buf, format="JPEG")
            variants.append(buf.getvalue())
        except Exception as exc:
            logger.error("preprocess_image: failed to build %d-degree variant: %r", angle, exc)

    return variants


def call_ocr_service(
    image_bytes: bytes,
    host: str = DEFAULT_OCR_HOST,
    port: int = DEFAULT_OCR_PORT,
) -> str:
    """POST raw image bytes to the ocr_runtime Brick's /ocr endpoint and return the recognized
    text. Returns "" (and logs) on any failure -- connection refused, timeout, non-200, malformed
    response -- never raises.
    """
    url = f"http://{host}:{port}/ocr"
    try:
        resp = requests.post(url, data=image_bytes, timeout=OCR_REQUEST_TIMEOUT_S)
    except requests.exceptions.RequestException as exc:
        logger.error("call_ocr_service: request to %s failed: %r", url, exc)
        return ""

    if resp.status_code != 200:
        logger.error("call_ocr_service: %s returned status %d: %s", url, resp.status_code, resp.text[:200])
        return ""

    try:
        data = resp.json()
    except ValueError as exc:
        logger.error("call_ocr_service: %s returned non-JSON body: %r", url, exc)
        return ""

    return data.get("text", "") or ""


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


def _heuristic_candidates(raw_text: str) -> list[dict]:
    """Best-effort fallback when no LLM is available: treat each non-trivial line of raw OCR
    text as a naive title guess. Clearly worse than an LLM pass, but keeps the feature usable.
    """
    candidates = []
    for line in raw_text.splitlines():
        line = line.strip()
        if len(line) > 4:
            candidates.append({"title": line, "author": ""})
    return candidates


_LLM_PROMPT_TEMPLATE = """The following is raw, noisy OCR text extracted from a photo of a \
bookshelf (multiple book spines' text may be interleaved, fragmented, or partially unreadable).

Extract a JSON list of book guesses in this exact shape:
[{{"title": "...", "author": "..."}}, ...]

Rules:
- Only include a fragment if you can confidently identify at least a plausible book title from it.
- Omit "author" (empty string) if you can't confidently identify one -- never invent one.
- Skip fragments entirely if they don't look like a real book title/author at all.
- Never invent a full title from nothing -- only use text that actually appears in the OCR output.
- Respond with ONLY the JSON list, no commentary, no markdown code fences.

OCR text:
{raw_text}
"""


def extract_candidates(raw_text: str, llm=None) -> list[dict]:
    """Turn raw OCR text into a list of {"title","author"} candidate guesses.

    If `llm` is provided (an arduino.app_bricks.llm.LargeLanguageModel-like object exposing
    .chat(message: str) -> str), ask it to extract structured guesses and parse its JSON response
    defensively. If `llm` is None, or anything about the LLM call/parse fails, fall back to (or
    degrade to) a naive per-line heuristic -- never raises.
    """
    if not raw_text or not raw_text.strip():
        return []

    if llm is None:
        return _heuristic_candidates(raw_text)

    try:
        prompt = _LLM_PROMPT_TEMPLATE.format(raw_text=raw_text)
        response = llm.chat(prompt)
    except Exception as exc:
        logger.error("extract_candidates: LLM call failed, falling back to heuristic: %r", exc)
        return _heuristic_candidates(raw_text)

    try:
        cleaned = _strip_code_fences(response)
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.error("extract_candidates: failed to parse LLM JSON response: %r", exc)
        return []

    if not isinstance(parsed, list):
        logger.error("extract_candidates: LLM JSON response was not a list: %r", type(parsed))
        return []

    candidates = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        author = str(item.get("author", "") or "").strip()
        candidates.append({"title": title, "author": author})

    return candidates


def process_shelf_photo(image_bytes: bytes, llm=None) -> list[dict]:
    """Orchestrates the full pipeline: preprocess -> OCR each rotation variant -> keep the variant
    with the most text -> extract structured candidates.

    Always returns a list (possibly empty) -- never raises, even if the ocr_runtime Brick is
    unreachable or the image is malformed.
    """
    variants = preprocess_image(image_bytes)
    if not variants:
        return []

    best_text = ""
    for variant in variants:
        text = call_ocr_service(variant)
        if len(text) > len(best_text):
            best_text = text

    if not best_text.strip():
        return []

    return extract_candidates(best_text, llm=llm)
