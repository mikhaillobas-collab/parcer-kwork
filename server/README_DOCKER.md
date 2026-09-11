# 🐳 Инструкция по запуску Kwork Bot на сервере через Docker

## 1. Подготовка на локальном компьютере (Windows)

1. Убедитесь, что вы авторизованы в Kwork (проверьте, что в папке `browser_profile` есть данные сессии).
2. Запустите файл `server/export_profile.bat` — он создаст архив `session_kwork.tar.gz`.
3. Отправьте код и архив на ваш сервер через git или SFTP/SCP:
   ```bash
   scp session_kwork.tar.gz root@YOUR_SERVER_IP:/root/kwork-bot/
   ```

---

## 2. Установка на сервере (Ubuntu / Debian)

1. Установите Docker и Docker Compose (если еще не установлены):
   ```bash
   curl -fsSL https://get.docker.com -o get-docker.sh
   sh get-docker.sh
   ```

2. Перейдите в папку проекта:
   ```bash
   cd /root/kwork-bot
   ```

3. Распакуйте архив с вашей сессией:
   ```bash
   tar -xzf session_kwork.tar.gz
   ```

4. Создайте файл настроек окружения `.env.docker`:
   ```bash
   cp server/.env.docker.example server/.env.docker
   nano server/.env.docker
   ```
   Вставьте:
   - `GEMINI_API_KEY` (ваш ключ Gemini)
   - `TELEGRAM_BOT_TOKEN` (токен вашего бота)
   - `TELEGRAM_CHAT_ID` (ваш числовой ID)
   - Установите `DRY_RUN=true` для проверки или `DRY_RUN=false` для реальной работы.

---

## 3. Запуск контейнера

1. Сборка и фоновый запуск:
   ```bash
   cd server
   docker compose up -d --build
   ```

2. Просмотр логов в реальном времени:
   ```bash
   docker compose logs -f
   ```

3. Остановка бота:
   ```bash
   docker compose down
   ```

4. Перезапуск бота:
   ```bash
   docker compose restart
   ```
