FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY backend/src ./backend/src
COPY data/*.sql data/seed.json ./data/
COPY frontend ./frontend
# Keep the editable src layout: shared resource paths resolve relative to /app.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev && \
    groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app && \
    mkdir -p /app/data/runtime && chown 10001:10001 /app/data/runtime

USER 10001:10001
CMD ["uvicorn", "helpdesk.api:from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
