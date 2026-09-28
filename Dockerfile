# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- builder
# Resolves dependencies into a self-contained virtualenv; no build tooling
# reaches the runtime image.
FROM python:${PYTHON_VERSION}-slim AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH"
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN pip install --require-virtualenv -r /tmp/requirements.txt

# ---------------------------------------------------------------- test-deps
FROM builder AS test-deps
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --require-virtualenv -r /tmp/requirements-dev.txt

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd --system --gid 10001 matchlens \
 && useradd --system --uid 10001 --gid matchlens --home-dir /app --no-create-home --shell /usr/sbin/nologin matchlens

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
# Code stays root-owned and read-only to the service user.
COPY sql ./sql
COPY loader ./loader
COPY app ./app

USER matchlens
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers"]

# ---------------------------------------------------------------- test
FROM runtime AS test
COPY --from=test-deps /opt/venv /opt/venv
COPY pytest.ini ./
COPY tests ./tests
ENV PYTHONDONTWRITEBYTECODE=1
HEALTHCHECK NONE
CMD ["pytest", "-p", "no:cacheprovider"]
