FROM python:3.13-slim

# openssl CLI for the self-signed TLS listener cert
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir fastapi uvicorn

COPY src/ ./src/

ENV PYTHONPATH=/app/src

EXPOSE 20780 20781 20782

# PORT is honored for PaaS platforms (Railway/Heroku-style port injection).
# WEB_CONCURRENCY > 1 requires TLS_ENABLED=false and CHAOS_ENABLED=false —
# the extra listeners are spawned per worker and would collide on their ports.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-20780} --workers ${WEB_CONCURRENCY:-1}"]
