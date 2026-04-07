FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create data directory for persistent storage
RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python", "bot.py"]
