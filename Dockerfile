# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml README.md ./
COPY bot/ ./bot/
COPY CalendarClient/ ./CalendarClient/
COPY db/ ./db/
# Use published Linux wheels: no compiler or Rust toolchain in the router image.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --only-binary=:all: .

FROM ${PYTHON_IMAGE} AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL="sqlite+aiosqlite:////data/calendar_bot.db"
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY bot/ ./bot/
COPY CalendarClient/ ./CalendarClient/
COPY db/ ./db/
COPY alembic.ini ./
COPY migrations/ ./migrations/
RUN groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin bot \
    && mkdir /data \
    && chown 10001:10001 /data
USER 10001:10001
VOLUME ["/data"]
STOPSIGNAL SIGTERM
CMD ["python", "-m", "bot"]
