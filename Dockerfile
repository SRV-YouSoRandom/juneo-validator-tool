# Multi-stage build for smaller production image
FROM python:3.11-slim as builder

# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --user -r requirements.txt

# Production stage
FROM python:3.11-slim

# Create non-root user for security
RUN groupadd -r botuser && useradd -r -g botuser botuser

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy Python packages from builder stage
COPY --from=builder /root/.local /home/botuser/.local

# Copy application code
COPY bot.py .
COPY .env.example .

# Create data directory for persistent storage with correct permissions
RUN mkdir -p /app/data && \
    mkdir -p /app/logs && \
    chown -R botuser:botuser /app/data && \
    chown -R botuser:botuser /app/logs && \
    chown -R botuser:botuser /app

# Switch to non-root user
USER botuser

# Add local python packages to PATH
ENV PATH=/home/botuser/.local/bin:$PATH

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Set the database path explicitly to the mounted volume
ENV DATABASE_PATH=/app/data/bot_data.db

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8000/metrics', timeout=5)" || exit 1

# Expose metrics port
EXPOSE 8000

# Run the bot
CMD ["python", "bot.py"]