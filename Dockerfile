FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DOUNAI_DATA_DIR=/app/data \
    DOUNAI_HOST=0.0.0.0 \
    DOUNAI_PORT=8000

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

ARG INSTALL_LOCAL_ASR=0
RUN pip install . \
    && if [ "$INSTALL_LOCAL_ASR" = "1" ]; then pip install ".[local-asr]"; fi \
    && addgroup --gid 10001 dounai \
    && adduser --uid 10001 --gid 10001 --disabled-password --gecos "" \
         --no-create-home --home /app dounai \
    && mkdir -p /app/data \
    && chown -R 10001:10001 /app/data

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3); assert json.load(r)['status']=='ok'"

CMD ["dounai-input", "serve", "--host", "0.0.0.0"]
