# Build from the repo root:  docker build -f hospital/crew/Dockerfile -t hospital-crew .
FROM python:3.12-slim

RUN pip install --no-cache-dir uv
WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --package hospital-crew
RUN useradd -m app && chown -R app /app
USER app
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8010
HEALTHCHECK --interval=15s --timeout=3s --retries=5 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8010/v1/health').status < 500 else 1)"
WORKDIR /app/hospital/crew
CMD ["uvicorn","crew.main:app_factory","--factory","--host","0.0.0.0","--port","8010"]
