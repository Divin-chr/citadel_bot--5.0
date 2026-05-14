# Lightweight single-stage build using pre-built wheels
FROM python:3.12-slim

WORKDIR /app

# Copy requirements
COPY requirements.txt .

# Install runtime dependencies (no build tools needed)
# Use --default-timeout for slow networks, --retries for resilience
RUN pip install --upgrade pip && \
    pip install --default-timeout=1000 --retries 5 --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user for security
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Expose ports
EXPOSE 8080 8501 8765

# Environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

# Run the application
CMD ["python", "citadel_bot/main.py"]
