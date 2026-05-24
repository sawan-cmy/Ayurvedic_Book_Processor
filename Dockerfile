FROM python:3.11-slim

# Install system dependencies: poppler for PDF processing, fonts for slide deck rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    fonts-indic \
    fonts-deva \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

# Set production environment variables
ENV HOST=0.0.0.0
ENV PORT=7860
ENV DISABLE_INLINE_WORKERS=false

# Expose port
EXPOSE 7860

USER appuser

# Command to run Waitress server with 50 threads for 25 concurrent users
CMD ["waitress-serve", "--host=0.0.0.0", "--port=7860", "--threads=50", "app:app"]
