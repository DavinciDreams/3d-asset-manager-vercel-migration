FROM python:3.12-slim

# Avoid .pyc files and buffer issues in containers
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Coolify will route to this port; the app listens on $PORT (default 8000)
EXPOSE 8000

# gunicorn imports `app` from wsgi.py
# - 2 workers x 4 threads is a sane default for a small Flask app
# - timeout raised to 120s for larger model uploads to GridFS
CMD ["sh", "-c", "gunicorn wsgi:app --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 4 --timeout 120"]
