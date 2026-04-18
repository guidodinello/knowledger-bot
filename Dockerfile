FROM python:3.13-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md .
COPY knowledger/ knowledger/
COPY main.py .

RUN uv sync --frozen --no-dev

CMD ["uv", "run", "python", "main.py"]
