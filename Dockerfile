FROM python:3.14.5-alpine3.22

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN apk add --no-cache gcc=14.2.0-r6 musl-dev=1.2.5-r12 libffi-dev=3.4.8-r0

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
