FROM python:3.12-slim
WORKDIR /app
COPY services/rag-service /app
RUN pip install --no-cache-dir ".[minio]"
ENV STORE=qdrant EMBEDDER=gateway DB_URL=/data/rag.db
VOLUME /data
EXPOSE 8400
CMD ["uvicorn", "rag_service.main:app", "--host", "0.0.0.0", "--port", "8400"]
