import os
from pathlib import Path
from dotenv import load_dotenv

# Загружаем переменные из .env
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Gemini API
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")

# URL страницы биржи с настроенными фильтрами
# Пользователь может скопировать полный URL из адресной строки браузера со своими фильтрами
KWORK_URL = os.getenv("KWORK_URL", "https://kwork.ru/projects")

# Режим работы
# ВНИМАНИЕ: Если DRY_RUN = True, бот формирует отклик, заполняет форму (или делает скриншот),
# но НЕ нажимает финальную кнопку "Предложить" для экономии коннектов
DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() in ("true", "1", "yes")

# Запуск браузера: False — окно видно (рекомендуется для избежания капч и первого входа)
HEADLESS = os.getenv("HEADLESS", "false").strip().lower() in ("true", "1", "yes")

# Интервалы между проверками биржи (в секундах)
CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "60"))
CHECK_INTERVAL_MAX = int(os.getenv("CHECK_INTERVAL_MAX", "120"))

# Пути к данным
USER_DATA_DIR = BASE_DIR / "browser_profile"

# Если на сервере (например, Bothost / Docker) есть постоянный том /app/data, сохраняем БД туда
if os.path.exists("/app/data"):
    DEFAULT_DB_PATH = Path("/app/data/kwork_bot.db")
else:
    DEFAULT_DB_PATH = BASE_DIR / "kwork_bot.db"

DB_PATH = Path(os.getenv("DB_PATH", str(DEFAULT_DB_PATH)))

# Настройки ценообразования и фильтров
MIN_ACCEPTABLE_PRICE = int(os.getenv("MIN_ACCEPTABLE_PRICE", "3000"))
MAX_EXISTING_OFFERS = int(os.getenv("MAX_EXISTING_OFFERS", "10"))
PRICING_RULES_PATH = BASE_DIR / "pricing_rules.json"
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# Telegram-уведомления
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Авторизационные куки для облачного хостинга (Bothost, Railway и др.)
KWORK_COOKIES = os.getenv("KWORK_COOKIES", "").strip()

import urllib.parse
import re

def normalize_proxy(raw: str) -> str:
    """Приводит прокси любого формата (включая IP:PORT:USER:PASS) к валидному URL для httpx."""
    if not raw:
        return ""
    raw = raw.strip()
    if raw.startswith(("http://", "https://", "socks5://", "socks4://")) and "@" in raw:
        return raw
    
    clean = re.sub(r"^(https?|socks5|socks4)://", "", raw)
    parts = clean.split(":")
    if len(parts) == 4:
        ip, port, user, pwd = parts
        user_enc = urllib.parse.quote(user)
        pwd_enc = urllib.parse.quote(pwd)
        scheme = "socks5://" if "socks5" in raw else "http://"
        return f"{scheme}{user_enc}:{pwd_enc}@{ip}:{port}"
    
    if "@" in clean:
        scheme = "socks5://" if "socks5" in raw else "http://"
        return f"{scheme}{clean}"
        
    if not raw.startswith(("http://", "https://", "socks5://")):
        return "http://" + raw
    return raw

# Прокси для обхода геоблокировки Google Gemini (User location is not supported)
# Поддерживает любые форматы: http://user:pass@ip:port или ip:port:user:pass
_raw_proxy = os.getenv("GEMINI_PROXY", "").strip() or os.getenv("HTTPS_PROXY", "").strip()
GEMINI_PROXY = normalize_proxy(_raw_proxy)
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "").strip()







