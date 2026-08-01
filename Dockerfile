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

# Run unprivileged. Nothing here needs root: the HTTP token endpoint binds 8080 (above
# the privileged range), and the only writes go to DATA_DIR.
#
# The uid/gid is pinned to 1001 to match the `ubuntu` account on the deployment host,
# because DATA_DIR is bind-mounted out of that user's home. The match is not cosmetic:
# claude_client writes session_token.json with mode 0o600, and config._load_persisted_token
# fails closed if it cannot be read, so a mismatched uid would crash the bot at startup
# rather than degrade. Existing state files are root-owned from when this ran as root, so
# deploy.sh and the CI deploy chown DATA_DIR before starting the container.
RUN groupadd --gid 1001 appuser \
    && useradd --uid 1001 --gid 1001 --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

# Invoke the venv interpreter directly instead of going through `uv run`: uv requires a
# writable cache (/.cache/uv) and aborts with a permission error for a non-root user.
ENV PATH="/app/.venv/bin:$PATH"

ARG GIT_SHA=
ARG GIT_COMMIT_DATE=
ENV GIT_SHA=${GIT_SHA}
ENV GIT_COMMIT_DATE=${GIT_COMMIT_DATE}

USER appuser

CMD ["python", "main.py"]
