# Alpine, deliberately. aa1e198 ("fix: Dockerfile to reduce vulnerabilities", co-authored
# by snyk-bot) moved off python:3.13-slim precisely to clear Debian 13 CVEs, and that still
# holds: a trivy scan of the two candidate bases reports 26 HIGH/CRITICAL OS advisories for
# python:3.14.5-slim-trixie against 2 for this one. Going back to Debian to unify libc would
# have traded a 13x vulnerability surface for it.
#
# The glibc-vs-musl divergence that caused #59 is closed instead by the `test` stage below,
# which runs the suite against this exact base in CI — detecting libc-specific failures
# rather than eliminating them by importing Debian's CVEs.
#
# The gcc/musl-dev/libffi-dev install this used to need is gone: every dependency now
# publishes a musllinux wheel, so nothing compiles from source and no compiler ships in the
# image. That also retires three hand-pinned apk revisions that broke on every Alpine bump.
FROM python:3.15.0b2-alpine3.22 AS base

WORKDIR /app

# Pinned, not `:latest`: a floating tag changes on any registry push and silently alters
# the build. Dependabot cannot track a COPY --from reference, so this is bumped by hand.
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./
COPY knowledger/ knowledger/
COPY main.py .

# Persistent state (poller_state.json, petition_queue.json, channels.json) lives here,
# bind-mounted to a host dir at runtime (see deploy.sh). Set as an image ENV rather than in
# the secrets env-file: it's a property of the deployment, not a secret. Local runs leave
# DATA_DIR unset and default to ".".
RUN uv sync --frozen --no-dev && mkdir -p /app/data
ENV DATA_DIR=/app/data

# The account the runtime stage drops to. Nothing here needs root: the HTTP token endpoint
# binds 8080, above the privileged range.
#
# Writes are not confined to DATA_DIR: logs/knowledger_<date>.log and its rotations
# (config.py) are CWD-relative and land in /app itself. That is why the chown below covers
# all of /app rather than DATA_DIR alone.
#
# The uid/gid is pinned to 1001 to match the `ubuntu` account on the deployment host,
# because DATA_DIR is bind-mounted out of that user's home. The match is not cosmetic:
# claude_client writes session_token.json with mode 0o600, and config._load_persisted_token
# fails closed if it cannot be read, so a mismatched uid would crash the bot at startup
# rather than degrade. Existing state files are root-owned from when this ran as root, so
# deploy.sh and the CI deploy chown DATA_DIR before starting the container.
#
# busybox addgroup/adduser rather than groupadd/useradd — this is Alpine, not Debian.
RUN addgroup -g 1001 appuser \
    && adduser -D -u 1001 -G appuser -s /sbin/nologin appuser \
    && chown -R appuser:appuser /app

# Invoke the venv interpreter directly rather than through `uv run`, following docker.md's
# direct-entrypoint guidance: it keeps uv out of the runtime path and skips its environment
# resolution on every start.
#
# To be clear about what this is *not* working around: `uv run` would function fine here.
# appuser has a real home, so its cache at ~/.cache/uv is writable. An earlier version of
# this comment claimed uv aborts for any non-root user, which was wrong — that error came
# from testing with `--user 1001:1001` against an image with no matching passwd entry, which
# left HOME as / and made /.cache/uv unwritable.
ENV PATH="/app/.venv/bin:$PATH"

ARG GIT_SHA=
ARG GIT_COMMIT_DATE=
ENV GIT_SHA=${GIT_SHA}
ENV GIT_COMMIT_DATE=${GIT_COMMIT_DATE}

# CI-only target: runs the test suite against the real runtime libc. It adds the dev group
# plus the files the runtime image deliberately omits (tests, scripts, cli). Dev deps are
# installed at build time so the suite needs no writable cache at run time and can execute
# as a non-root uid — necessary because the chmod-based permission tests are vacuous under
# root, which ignores mode 0000. This stage deliberately does NOT set USER: the dev sync
# needs root at build time, and ci.yml supplies a non-root uid when it runs the suite.
FROM base AS test
COPY tests/ tests/
COPY scripts/ scripts/
COPY cli.py .
RUN uv sync --frozen --dev

# Runtime is intentionally the LAST stage: deploy.yml and deploy.sh both build without
# --target, so the default must resolve here and never to `test`. Do not append a stage
# after this one. ci.yml asserts the built runtime image has no pytest to catch a slip.
FROM base AS runtime
USER appuser
CMD ["python", "main.py"]
