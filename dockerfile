# Use a slim Python image
FROM python:3.11-slim

# Prevents Python from writing .pyc files and buffers logs less
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install Chromium and necessary deps for Selenium in headless mode
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libgtk-3-0 \
    libnss3 \
    libgbm1 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    xdg-utils \
    wget \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# Make sure Selenium can find Chromium
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMIUM_FLAGS="--no-sandbox --disable-dev-shm-usage --disable-gpu --headless=new"

# Workdir and copy app
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/

# Expose port for Render
EXPOSE 10000

# Start the FastAPI app with uvicorn
# CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "10000"]
# for render otherwise use above
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]