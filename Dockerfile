# Phase 0 POC image for the Stock Report feasibility proof-of-concept.
#
# Built and run with Apple Container (`container build` / `container run`).
# All Python dependencies are installed INSIDE this image; nothing is installed
# on the macOS host. See docs/phase-0/implementation.md for exact commands.

FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# WeasyPrint needs pango/cairo at runtime; fonts-noto-cjk provides the
# Traditional Chinese glyphs used by the A4 report templates.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz0b \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libffi8 \
        shared-mime-info \
        fontconfig \
        fonts-noto-cjk \
        fonts-noto-cjk-extra \
        ca-certificates \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

WORKDIR /work

# Dependency layer first so source edits do not re-trigger downloads.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml ./
COPY src ./src
RUN pip install -e . --no-deps

# The rest of the repo (fixtures, config, tests, docs) is normally bind-mounted
# at /work so that generated reports land on the host. Copy it anyway so the
# image is self-contained for a one-shot `container run`.
COPY . .

CMD ["python", "-m", "pytest", "-q"]
