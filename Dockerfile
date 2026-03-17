FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml requirements.txt ./
RUN pip install --no-cache-dir -e .

# Copy source
COPY nexus/ nexus/

# Default: paper trading
ENTRYPOINT ["python", "-m", "nexus"]
CMD ["run", "--paper", "--no-dashboard"]
