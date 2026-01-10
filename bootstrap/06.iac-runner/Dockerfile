FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    openssh-client \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy webhook server
COPY webhook_server.py .
COPY sync_runner.py .

# Create workspace directory
RUN mkdir -p /workspace

EXPOSE 8080

CMD ["python", "webhook_server.py"]
