# --- Stage 1: Build React Frontend ---
FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Stage 2: Build Python Backend ---
FROM python:3.11-slim AS backend-builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# --- Stage 3: Runtime ---
FROM python:3.11-slim
WORKDIR /app

# Copy python packages
COPY --from=backend-builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Environment variables
ENV DATA_DIR=/data
ENV PORT=8373

# Create data directory
RUN mkdir -p /data && chmod 777 /data

# Install tini
RUN apt-get update && apt-get install -y --no-install-recommends tini && rm -rf /var/lib/apt/lists/*

# Copy backend source
COPY backend/ ./backend/

# Copy built frontend to backend/static
COPY --from=frontend-builder /app/frontend/dist ./backend/static

# Define volume for data persistence (e.g. Tuya wizard files)
VOLUME ["/data"]

EXPOSE $PORT
STOPSIGNAL SIGINT
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-u", "backend/app.py"]
