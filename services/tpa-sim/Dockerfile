FROM python:3.12-slim
WORKDIR /app
COPY contract/python /app/contract/python
COPY services/tpa-sim /app/services/tpa-sim
RUN pip install --no-cache-dir /app/contract/python /app/services/tpa-sim
ENV TPA_SIM_DB_URL=sqlite+aiosqlite:////data/tpa_sim.db
VOLUME /data
EXPOSE 8500
CMD ["uvicorn", "tpa_sim.main:app", "--host", "0.0.0.0", "--port", "8500"]
