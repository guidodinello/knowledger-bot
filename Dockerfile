# Debian slim, not Alpine, deliberately. On musl there are no manylinux wheels, so every
# dependency compiled from source — hence the gcc/musl-dev/libffi-dev install this replaces
# — and, more importantly, musl exports a different set of libc symbols than the glibc the
# test suite runs on. That divergence shipped a crash loop: code resolving renameat2 via
# ctypes passed every test and died on startup in production (see #59). Matching the libc
# CI tests against removes the whole class, and every dependency here has a prebuilt
# manylinux wheel, so dropping Alpine also drops the compiler from the image.
# Pinned to the same patch version as .python-version so local, CI, and prod agree.
FROM python:3.14.5-slim-trixie

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./
COPY knowledger/ knowledger/
COPY main.py .
COPY channels.json .

# Persistent state (poller_state.json, petition_queue.json) lives here, bind-mounted to a
# host dir at runtime (see deploy.sh). Set as an image ENV rather than in the secrets
# env-file: it's a property of the deployment, not a secret. Local runs leave DATA_DIR
# unset and default to ".".
RUN uv sync --frozen --no-dev && mkdir -p /app/data
ENV DATA_DIR=/app/data

ARG GIT_SHA=
ARG GIT_COMMIT_DATE=
ENV GIT_SHA=${GIT_SHA}
ENV GIT_COMMIT_DATE=${GIT_COMMIT_DATE}

CMD ["uv", "run", "python", "main.py"]
