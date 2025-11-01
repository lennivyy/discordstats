# Используем тонкий базовый образ Python
FROM python:3.12-slim

# Без буферов и *.pyc
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Невредные системные обновления (по желанию можно убрать)
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Создаём непривилегированного пользователя
RUN useradd -m appuser
WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходники (без секретов — см. .dockerignore)
COPY . .

# Переходим на непривилегированного пользователя
USER appuser

# Запускаем бота
CMD ["python", "main.py"]
