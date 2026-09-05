FROM python:3.11-slim

WORKDIR /app

# System deps for psycopg2-binary wheels and general build hygiene.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# --workers 1 is deliberate, not a default left in place: the checkpoint
# registry (backend/core/registry.py) holds live Solari runtime handles
# in process memory. A second worker would not see sessions created by
# the first, silently breaking actions/timeline/rollback for half of
# all requests. Scale this service vertically, not by worker count,
# until the registry is moved out of process memory.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
