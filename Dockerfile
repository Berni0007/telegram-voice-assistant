FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BOT_MEMORY_DB=/app/data/bot_memory.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY src ./src

# Bothost keeps /app/data between Git deploys/rebuilds.
RUN mkdir -p /app/data

CMD ["python", "bot.py"]
