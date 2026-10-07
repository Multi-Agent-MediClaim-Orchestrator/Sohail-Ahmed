FROM python:3.12-slim
WORKDIR /app
COPY contract/python /app/contract/python
COPY insurer/calc_engine /app/insurer/calc_engine
COPY insurer/crew /app/insurer/crew
RUN pip install --no-cache-dir /app/contract/python /app/insurer/calc_engine /app/insurer/crew && useradd -m crew
USER crew
ENV CREWAI_TELEMETRY_OPT_OUT=true OTEL_SDK_DISABLED=true
EXPOSE 8110
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen(\"http://localhost:8110/v1/health\")"
CMD ["uvicorn", "insurer_crew.main:app", "--host", "0.0.0.0", "--port", "8110"]
