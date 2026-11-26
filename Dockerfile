# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    MS_RUNTIME_DIR=/app/runtime \
    MS_DATA_DIR=/app/data \
    MS_LOG_DIR=/app/logs \
    MS_AUDIT_LOG_DIR=/app/audit_logs \
    MS_BACKUP_DIR=/app/backups

WORKDIR /app

RUN groupadd --gid "${APP_GID}" market-signals \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" \
        --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin \
        market-signals

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip==26.1.2 \
    && python -m pip install --requirement /app/requirements.txt

COPY . /app

RUN mkdir -p \
        /app/runtime \
        /app/data \
        /app/logs \
        /app/backups \
        /app/output \
        /app/reports \
        /app/audit_logs \
    && chown -R "${APP_UID}:${APP_GID}" \
        /app/runtime \
        /app/data \
        /app/logs \
        /app/backups \
        /app/output \
        /app/reports \
        /app/audit_logs \
    && chmod -R a-w /app/api /app/core /app/agents /app/frontend /app/tools \
    && chmod 0555 /app/run_server.py

USER market-signals:market-signals

EXPOSE 8003
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8003/api/health', timeout=3).read()"]

ENTRYPOINT ["python", "/app/tools/container_entrypoint.py"]
