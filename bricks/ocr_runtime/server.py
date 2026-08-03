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
                   Response: {"text": "<raw tesseract stdout>"} on success (200), or
                   {"error": "..."} on failure (500). Never crashes the server process.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
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
            ["tesseract", tmp_path, "stdout", "-l", "eng"],
            capture_output=True,
            text=True,
            timeout=TESSERACT_TIMEOUT_S,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or "tesseract failed with no stderr output"
            return JSONResponse({"error": err}, status_code=500)

        return {"text": result.stdout}
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
