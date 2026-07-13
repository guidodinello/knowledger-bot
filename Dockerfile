FROM python:3.14.5-alpine3.22

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN apk add --no-cache gcc musl-dev libffi-dev

COPY pyproject.toml uv.lock README.md ./
COPY knowledger/ knowledger/
COPY main.py .
COPY channels.json .

RUN uv sync --frozen --no-dev

# Mount point for persistent state (poller_state.json, petition_queue.json).
# Bind-mounted to a host dir at runtime; see deploy.sh + DATA_DIR.
RUN mkdir -p /app/data

CMD ["uv", "run", "python", "main.py"]
