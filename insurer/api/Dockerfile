FROM python:3.12-slim
WORKDIR /app
COPY contract/python /app/contract/python
COPY insurer/calc_engine /app/insurer/calc_engine
COPY insurer/api /app/insurer/api
RUN pip install --no-cache-dir /app/contract/python /app/insurer/calc_engine /app/insurer/api psycopg2-binary && useradd -m api
WORKDIR /app/insurer/api
USER api
EXPOSE 8100
CMD ["uvicorn", "insurer_app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8100"]
