import logging
import sys
import time
from playwright.sync_api import sync_playwright

import config

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("kwork_login")

def login():
    logger.info("=" * 60)
    logger.info("🔑 АВТОРИЗАЦИЯ В KWORK")
    logger.info("=" * 60)
    logger.info("Сейчас откроется окно браузера.")
    logger.info("Войдите в свой аккаунт Kwork (логин, пароль, 2FA если есть).")
    logger.info("Как только вы войдете, сессия автоматически сохранится навсегда.")
    logger.info("=" * 60)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(config.USER_DATA_DIR),
            headless=False,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1440, "height": 900},
            locale="ru-RU"
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://kwork.ru/", wait_until="domcontentloaded")

        logger.info("Ожидание входа в аккаунт (до 5 минут)...")
        start = time.time()
        logged_in = False
        while time.time() - start < 300:
            time.sleep(3)
            # Проверяем появление элементов авторизованного профиля
            if page.locator(".user-avatar, .header__user, a[href*='/user/'], .balance-item").first.is_visible():
                logged_in = True
                logger.info("🎉 Поздравляем! Авторизация успешно выполнена.")
                logger.info("Профиль и куки сохранены в папке browser_profile.")
                
                # Делаем проверочный скриншот
                shot_path = config.SCREENSHOTS_DIR / "logged_in_success.png"
                page.screenshot(path=str(shot_path))
                logger.info(f"Скриншот сохранен: {shot_path}")
                break

        if not logged_in:
            logger.warning("Время ожидания истекло. Пожалуйста, запустите скрипт снова, когда будете готовы войти.")

        time.sleep(2)
        ctx.close()

if __name__ == "__main__":
    login()
