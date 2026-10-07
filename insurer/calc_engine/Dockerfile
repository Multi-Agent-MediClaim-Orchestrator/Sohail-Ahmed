FROM python:3.12-slim
WORKDIR /app
COPY contract/python /app/contract/python
COPY insurer/calc_engine /app/insurer/calc_engine
RUN pip install --no-cache-dir /app/contract/python /app/insurer/calc_engine && useradd -m calc
USER calc
EXPOSE 8120
CMD ["uvicorn", "calc_engine.api:app", "--host", "0.0.0.0", "--port", "8120"]
