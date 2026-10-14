FROM python:3.13-slim
WORKDIR /app
COPY server.py store.py parsers.py ./
COPY public/ ./public/
RUN useradd -m app && mkdir -p /app/data && chown -R app:app /app
USER app
ENV HOST=0.0.0.0 \
    PORT=8765 \
    VENNER_DB=/app/data/venner.db \
    VENNER_SECRET_FILE=/app/data/.venner_secret \
    ADMIN_USER=admin
# ADMIN_PASSWORD intentionally unset: first run prints a generated one (see logs).
EXPOSE 8765
VOLUME /app/data
CMD ["python3", "server.py"]
