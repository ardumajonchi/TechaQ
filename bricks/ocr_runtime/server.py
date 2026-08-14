# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Bare sidecar process for the ocr_runtime Brick -- no App Lab Python SDK involved here, this
container's whole job is: accept a raw image over HTTP, shell out to the `tesseract` CLI, and hand
the raw OCR text back, over a small internal HTTP API that engine/ocr.py (in the main app
container) calls.

Endpoints:
  GET  /healthz -- liveness check for the brick's compose healthcheck
  POST /ocr     -- body is the RAW IMAGE BYTES (Content-Type is whatever the caller sends, e.g.
                   image/jpeg or image/png -- it is ignored; the body is written to a temp file
                   as-is and handed straight to `tesseract`, which sniffs the format itself).
                   NOT multipart/form-data -- the request body IS the image, nothing else.
                   Response: {"text": "<raw tesseract stdout>", "confidence": <0-100 or null>}
                   on success (200), or {"error": "..."} on failure (500). Never crashes the
                   server process.

`confidence` is Tesseract's own average per-word confidence (from its TSV output, not the plain
`stdout` text mode) -- it exists so a caller trying several rotations of the same photo can tell
which orientation Tesseract actually read correctly, rather than guessing from raw text length
(a garbled 90-degree-rotated read of vertical spine text can easily produce *more* characters
than the correctly-oriented read, since gibberish still gets transcribed as *something*).
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI(title="ocr_runtime")

TESSERACT_TIMEOUT_S = 30
TMP_DIR = "/tmp/ocr"


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


def _parse_tsv(tsv_text: str) -> tuple[str, float | None]:
    """Reconstruct plain text (one line per Tesseract source line, blank lines between blocks)
    and compute the average word confidence from Tesseract's TSV output. Returns (text, None) if
    no words were recognized at all (a blank/unreadable image) -- never raises on malformed TSV,
    since the caller must not 500 just because parsing this side-channel failed."""
    lines: list[str] = []
    current_line_key = None
    current_words: list[str] = []
    confidences: list[float] = []

    try:
        reader = csv.DictReader(io.StringIO(tsv_text), delimiter="\t")
        for row in reader:
            if row.get("level") != "5":  # level 5 = word
                continue
            line_key = (row.get("block_num"), row.get("par_num"), row.get("line_num"))
            if line_key != current_line_key:
                if current_words:
                    lines.append(" ".join(current_words))
                current_words = []
                current_line_key = line_key
            current_words.append(row.get("text", ""))
            try:
                conf = float(row.get("conf", "-1"))
                if conf >= 0:
                    confidences.append(conf)
            except ValueError:
                pass
        if current_words:
            lines.append(" ".join(current_words))
    except csv.Error:
        return "", None

    text = "\n".join(lines)
    avg_confidence = sum(confidences) / len(confidences) if confidences else None
    return text, avg_confidence


@app.post("/ocr")
async def ocr(request: Request):
    body = await request.body()
    if not body:
        return JSONResponse({"error": "empty request body"}, status_code=400)

    os.makedirs(TMP_DIR, exist_ok=True)
    tmp_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}.img")
    try:
        with open(tmp_path, "wb") as f:
            f.write(body)

        result = subprocess.run(
            ["tesseract", tmp_path, "stdout", "-l", "eng", "tsv"],
            capture_output=True,
            text=True,
            timeout=TESSERACT_TIMEOUT_S,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or "tesseract failed with no stderr output"
            return JSONResponse({"error": err}, status_code=500)

        text, confidence = _parse_tsv(result.stdout)
        return {"text": text, "confidence": confidence}
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "tesseract timed out"}, status_code=500)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=6098)
