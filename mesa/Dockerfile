FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY tests ./tests
ENV MESA_DB=/data/mesa.db PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Alive: the web answers and the background thread keeps its heartbeat (app/vivo.py). A 503 from
# /health (Yahoo behind) is not a reason to restart.
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s CMD ["python", "-m", "app.vivo"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
