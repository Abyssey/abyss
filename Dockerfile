# ==============================================================================
# Modern Coding Agent Architecture — Internal Pod / Container Runtime Image
# ==============================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOST=0.0.0.0 \
    PORT=8000 \
    PIP_NO_CACHE_DIR=1 \
    GIT_TERMINAL_PROMPT=0 \
    GCM_INTERACTIVE=never

# Git is required for isolated worktrees, internal GitLab clone/push, and diff review.
# ca-certificates allows mounting corporate root CAs via INTERNAL_GIT_CA_BUNDLE.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
        openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user for Kubernetes / internal Pod security compliance
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash appuser

WORKDIR /app

# Install Python package dependencies first for better layer caching
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --upgrade pip && pip install . \
    && mkdir -p /app/demo_workspace /app/demo_workspace/task_runs \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT}/healthz || exit 1

CMD ["sh", "-c", "exec uvicorn app.main:app --host \"${HOST}\" --port \"${PORT}\""]
