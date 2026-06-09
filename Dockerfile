FROM python:3.12-slim

# Avoid .pyc files and buffer issues in containers
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System tools for mesh conversion and FBX animation extraction.
RUN apt-get update && apt-get install -y --no-install-recommends \
        assimp-utils \
        gltfpack \
        nodejs \
        npm \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fSL -o /usr/local/bin/FBX2glTF \
        https://github.com/facebookincubator/FBX2glTF/releases/download/v0.9.7/FBX2glTF-linux-x64 \
    && chmod +x /usr/local/bin/FBX2glTF \
    && test -s /usr/local/bin/FBX2glTF

COPY tools/package.json /app/tools/package.json
RUN cd /app/tools && npm install --omit=dev

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Coolify will route to this port; the app listens on $PORT (default 8000)
EXPOSE 8000

# gunicorn imports `app` from wsgi.py
# - 2 workers x 4 threads is a sane default for a small Flask app
# - timeout raised to 300s for larger uploads and first-time export transcodes
CMD ["sh", "-c", "gunicorn wsgi:app --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 4 --timeout 300"]
