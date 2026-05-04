# --- Stage 1: Build stage (includes compilation tools) ---
FROM python:3.11-slim AS builder

WORKDIR /app

# Install minimal system packages for building
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Rust
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Install dependencies
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt


# --- Stage 2: Runtime stage (final image) ---
FROM python:3.11-slim

WORKDIR /app

# Copy only the python packages installed in stage 1
COPY --from=builder /root/.local /root/.local
# Copy source code
COPY . .

# Environment variables
ENV PATH=/root/.local/bin:$PATH
ENV DATA_DIR=/data

# Create data directory
RUN mkdir -p /data && chmod 777 /data

# Define volume for data persistence
VOLUME ["/data"]

# Port and execution command
ENV PORT=8373
EXPOSE $PORT
CMD uvicorn web.app:app --host 0.0.0.0 --port $PORT
