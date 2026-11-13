FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cene ./cene
COPY services ./services
COPY harness ./harness
COPY agent ./agent
# CENE_SERVICE selects the role: control | gateway | tools | all
ENV CENE_SERVICE=all CENE_CONTRACTS_DIR=harness/contracts
CMD exec uvicorn services.app:app --host 0.0.0.0 --port ${PORT:-8080}
