FROM python:3.12-slim AS gltfpack-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

ARG GLTFPACK_VERSION=v1.2
ARG GLTFPACK_SHA256=ebc236f5f6c08c7e5c5750476a187d24805d44d8c680449c4b7369c333f817b1
# Use the official release binary instead of compiling moving upstream repos
# during deploy; Coolify logs then fail at checksum/download/install, not a
# bundled clone/cmake/build command.
RUN curl -fSL \
        "https://github.com/zeux/meshoptimizer/releases/download/${GLTFPACK_VERSION}/gltfpack-ubuntu.zip" \
        -o /tmp/gltfpack.zip \
    && echo "${GLTFPACK_SHA256}  /tmp/gltfpack.zip" | sha256sum -c - \
    && python -c "import os, zipfile; z=zipfile.ZipFile('/tmp/gltfpack.zip'); names=[n for n in z.namelist() if n.rstrip('/').split('/')[-1]=='gltfpack']; assert names, 'gltfpack binary missing from release zip'; z.extract(names[0], '/tmp'); os.replace('/tmp/' + names[0], '/usr/local/bin/gltfpack'); os.chmod('/usr/local/bin/gltfpack', 0o755)" \
    && /usr/local/bin/gltfpack -v

FROM python:3.12-slim

# Avoid .pyc files and buffer issues in containers
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System tools for mesh conversion and FBX animation extraction, plus the
# OSMesa software-OpenGL stack so pyrender can rasterize GLB thumbnails
# offscreen (no GPU, no X server) in this headless container.
RUN apt-get update && apt-get install -y --no-install-recommends \
        assimp-utils \
        bash \
        curl \
        ca-certificates \
        gnupg \
        libosmesa6 \
        libosmesa6-dev \
        libgl1 \
        libglib2.0-0 \
        freeglut3-dev \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Tell PyOpenGL/pyrender to use the software OSMesa backend at import time.
ENV PYOPENGL_PLATFORM=osmesa

COPY --from=gltfpack-builder /usr/local/bin/gltfpack /usr/local/bin/gltfpack

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
