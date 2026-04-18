FROM python:3.15-rc-alpine3.22

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN apk add --no-cache gcc musl-dev libffi-dev

COPY pyproject.toml uv.lock README.md ./
COPY knowledger/ knowledger/
COPY main.py .

RUN uv sync --frozen --no-dev

CMD ["uv", "run", "python", "main.py"]
