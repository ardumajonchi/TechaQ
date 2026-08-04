# ocr_runtime Brick

A custom Brick that runs headless Tesseract OCR as a small internal HTTP service, used by
`python/engine/ocr.py` (in the main app container) for shelf-spine text extraction and
photo-to-ISBN scanning. There's no official Tesseract Brick, so this one is built and maintained
in this repo, following the same "custom runtime Brick" shape as `scummvm-q`'s emulator Brick.

## Why a separate container

The main app container runs as a non-root, non-apt-privileged user on a shared
`python-apps-base` image — it cannot `apt-get install` anything, and `tesseract-ocr` isn't part of
that base image. `brick_compose.yaml`'s `build:` directive instead builds this Brick's own image
locally from `Dockerfile`, which *can* run as root at build time. That's the only place in this
app `apt-get install` ever runs. Once built, the resulting container drops to a fixed non-root
user (`1000:1000`) before starting the actual service — root is only ever used to install
packages, never to run them.

## Architecture

- **`Dockerfile`** — `debian:trixie-slim` base; installs `tesseract-ocr` + the `eng` language
  pack, `python3`/`python3-venv`, and `curl` (needed for the compose healthcheck below); creates
  a `python3 -m venv /opt/venv` so `pip install`s land outside the system Python; creates a
  fixed `uid 1000` user and `chown`s `/app`, its `$HOME`, and a scratch `/tmp/ocr` dir to it before
  switching `USER 1000:1000`.
- **`requirements.txt`** — `fastapi` + `uvicorn` only. No App Lab Python SDK dependency at all —
  this container doesn't use `arduino.app_bricks.*`, it's a bare HTTP microservice.
- **`brick_config.yaml`** — declares `id: ocr_runtime`, `category: image`,
  `supported_boards: ["unoq"]`, `requires_container: true`, and the single internal port `6098`
  that `brick_compose.yaml` and `server.py` both agree on.
- **`brick_compose.yaml`** — builds the image from this directory's `Dockerfile` (rather than
  pulling a pre-built image from a registry, since `tesseract-ocr` needs a local `apt-get`) and
  wires up a `curl -f http://localhost:6098/healthz` healthcheck so App Lab won't route traffic to
  the container (or consider the app started) until Tesseract is actually ready to serve.
- **`server.py`** — the whole service: a ~75-line FastAPI app with two routes, run directly via
  `uvicorn.run(...)` in `__main__` (no ASGI server config beyond binding `0.0.0.0:6098`).

## Behavior — the HTTP contract

### `GET /healthz`

Returns `{"status": "ok"}` unconditionally. Exists purely for the Docker Compose healthcheck
above; it says nothing about whether `tesseract` itself works, only that the process is up.

### `POST /ocr`

- **Request body is the raw image bytes** — *not* `multipart/form-data`. Whatever content-type
  header the caller sends is ignored; the body is written to a temp file exactly as received, and
  `tesseract` sniffs the actual image format itself from the file contents.
- The body is written to `/tmp/ocr/<uuid4>.img`, then run through:
  ```
  tesseract <tmp_path> stdout -l eng
  ```
  with a 30-second subprocess timeout (`TESSERACT_TIMEOUT_S`).
- **On success (exit 0):** `{"text": "<raw tesseract stdout>"}`, HTTP 200. The text is completely
  unprocessed — no cleanup, no confidence filtering; that's left to the caller (`engine/ocr.py`
  handles the noisy-text-to-structured-guess step on the other side).
- **On failure** — non-zero exit, a timeout, or any other exception (bad/corrupt image bytes, an
  empty request body, `tesseract` missing, etc.) — the response is `{"error": "..."}` with HTTP
  500 (or 400 for an empty body). The temp file is always removed in a `finally` block regardless
  of outcome.
- The server process itself never crashes on a bad request: every failure mode above is caught
  and turned into a JSON error response, not an unhandled exception.

## How the main app uses it

`python/engine/ocr.py`'s `call_ocr_service()` is the only caller, POSTing to
`http://ocr_runtime:6098/ocr` (the Docker Compose service name resolves within the app's internal
network — no port-forwarding or `app.yaml` port entry is needed for this internal-only Brick).
Two flows build on top of it:

- **Shelf-photo OCR** (`process_shelf_photo`) — preprocesses the photo into 4 rotation variants
  (0/90/180/270°, grayscale + autocontrast), OCRs each one, keeps whichever variant returned the
  *most* text (spines are usually vertical, so the "right" rotation reads the most real text), and
  hands that text to the local LLM to extract `{title, author}` guesses.
- **Photo-to-ISBN scanning** (`process_isbn_photo`) — OCRs all 4 rotation variants too, but merges
  plausible ISBN-13/ISBN-10 digit sequences found across *every* variant instead of picking one
  "best" variant — a barcode's digits can sit in a small caption that loses to a longer, digit-free
  stretch of cover-blurb prose in the "most text" heuristic used for shelf photos.

## Degradation

If this container is missing, still starting, or the request times out/errors for any reason,
`call_ocr_service()` returns `""` (never raises), and `process_shelf_photo`/`process_isbn_photo`
in turn return an empty list. `engine/library.py` treats that the same as "OCR unavailable" —
shelf-photo review and photo-to-ISBN scanning quietly disable themselves, and the rest of the app
(manual entry, direct ISBN lookup, barcode scanning) is completely unaffected.
