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
FROM python:3.14.5-alpine3.22 AS base

WORKDIR /app

# Pinned, not `:latest`: a floating tag changes on any registry push and silently alters
# the build. Dependabot cannot track a COPY --from reference, so this is bumped by hand.
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /usr/local/bin/uv

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

# CI-only target: runs the test suite against the real runtime libc. It adds the dev group
# plus the files the runtime image deliberately omits (tests, scripts, cli). Dev deps are
# installed at build time so the suite needs no writable cache at run time and can execute
# as a non-root uid — necessary because the chmod-based permission tests are vacuous under
# root, which ignores mode 0000.
FROM base AS test
COPY tests/ tests/
COPY scripts/ scripts/
COPY cli.py .
RUN uv sync --frozen --dev

# Runtime is intentionally the LAST stage: deploy.yml and deploy.sh both build without
# --target, so the default must resolve here and never to `test`. Do not append a stage
# after this one. ci.yml asserts the built runtime image has no pytest to catch a slip.
FROM base AS runtime
CMD ["uv", "run", "python", "main.py"]
