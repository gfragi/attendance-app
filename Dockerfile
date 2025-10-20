# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# OS deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Python deps (lock αν έχεις requirements.txt/poetry)
COPY requirements.txt .
RUN pip install -r requirements.txt

# App
COPY app/ ./app/
# αν το main είναι app/Home.py
ENV STREAMLIT_CONFIG_DIR=/app/.streamlit
RUN mkdir -p $STREAMLIT_CONFIG_DIR

# Streamlit default config μέσω env (override με .env)
ENV STREAMLIT_SERVER_ENABLECORS=false \
    STREAMLIT_SERVER_ENABLEXSRSFPROTECTION=false \
    STREAMLIT_SERVER_HEADLESS=true

EXPOSE 8501

# Healthcheck: app root να απαντά
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

CMD ["bash", "-lc", "streamlit run app/Home.py --server.port=${STREAMLIT_SERVER_PORT:-8501} --server.address=0.0.0.0"]
