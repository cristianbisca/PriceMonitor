# ============================================
# Price Monitor - Docker Image
# ============================================

FROM python:3.12-slim

WORKDIR /app

# Install system dependencies needed for matplotlib and building packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libfreetype6 \
    libpng16-16 \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY backend/ ./

# Create data directory for SQLite database
RUN mkdir -p /app/data

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# Run the application
CMD ["python", "main.py"]