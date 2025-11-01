FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Копирование requirements и установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода
COPY . .

# Создание пользователя для безопасности
RUN groupadd -r discordbot && useradd -r -g discordbot discordbot
RUN chown -R discordbot:discordbot /app
USER discordbot

# Переменные окружения
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]