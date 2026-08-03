# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
#
# Built locally via brick_compose.yaml's `build:` directive rather than pulled from a registry --
# tesseract-ocr isn't present in the shared python-apps-base image, and the main app container
# runs as a non-root, non-apt-privileged user, so this is the only place in the app where
# `apt-get install` can actually run (as root, at image-build time).
FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        python3 \
        python3-venv \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py /app/server.py

RUN mkdir -p /tmp/ocr \
    && useradd -u 1000 -m -d /home/ocr ocr \
    && chown -R 1000:1000 /home/ocr /app /tmp/ocr

USER 1000:1000
ENV HOME=/home/ocr

EXPOSE 6098
ENTRYPOINT ["python3", "/app/server.py"]
