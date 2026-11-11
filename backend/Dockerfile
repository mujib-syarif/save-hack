FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DOWNLOAD_DIR=/tmp/downloads

# ffmpeg (includes ffprobe) is required for merging video+audio and for MP3 conversion.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY main.py .
COPY savehack ./savehack

# Job files live in ephemeral storage and are swept automatically (see JOB_TTL_SECONDS / MAX_STORAGE_MB).
RUN mkdir -p /tmp/downloads \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /tmp/downloads /app
USER appuser

# Railway injects PORT at runtime; 8000 is only the local default.
EXPOSE 8000
# Single process on purpose: job state, rate limits and the egress guard are in-memory.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-graceful-shutdown 10"]
