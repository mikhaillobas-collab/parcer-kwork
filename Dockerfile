# Базовый образ: официальный Playwright Python от Microsoft (с предустановленным Chromium и всеми Linux-библиотеками)
FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

# Установка рабочей директории
WORKDIR /app

# Отключаем буферизацию вывода Python для живых логов в docker compose logs
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

# Копируем список зависимостей
COPY requirements.txt .

# Устанавливаем зависимости Python
RUN pip install --no-cache-dir -r requirements.txt

# Устанавливаем браузер Chromium со всеми системными библиотеками
RUN python -m playwright install --with-deps chromium


# Копируем исходный код приложения
COPY . .

# Создаем необходимые директории для данных
RUN mkdir -p /app/browser_profile /app/screenshots /app/data

# Точка входа: запуск бота
CMD ["python", "main.py"]
