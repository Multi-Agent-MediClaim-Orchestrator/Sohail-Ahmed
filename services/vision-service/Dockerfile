# Build from the repo root:  docker build -f services/vision-service/Dockerfile -t vision-service .
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --package vision-service
RUN useradd -m app && chown -R app /app
USER app
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8300
HEALTHCHECK --interval=15s --timeout=3s --retries=5 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8300/v1/health').status < 500 else 1)"
WORKDIR /app/services/vision-service
CMD ["uvicorn","vision.main:app_factory","--factory","--host","0.0.0.0","--port","8300"]
