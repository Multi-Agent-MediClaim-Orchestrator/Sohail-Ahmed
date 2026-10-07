FROM ghcr.io/berriai/litellm:main-stable
WORKDIR /app
COPY config.yaml config.local.yaml /app/
COPY callbacks /app/callbacks
RUN pip install --no-cache-dir redis
ENV PYTHONPATH=/app
ENTRYPOINT []
CMD ["litellm", "--config", "/app/config.yaml", "--port", "4000"]
