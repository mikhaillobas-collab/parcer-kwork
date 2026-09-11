import sys
sys.stdout.reconfigure(encoding='utf-8')
from playwright.sync_api import sync_playwright
import config

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(user_data_dir=str(config.USER_DATA_DIR), headless=True)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto('https://kwork.ru/projects', wait_until='domcontentloaded')
    page.wait_for_timeout(3000)

    # Исключаем кнопку "Оставить отзыв" (.want-card__open-review)
    selector = ".want-card .kw-button--green:has-text('Предложить услугу'), .want-card span:has-text('Предложить услугу'):not(.want-card__open-review)"
    btns = page.locator(selector).all()
    print("Clean offer buttons found:", len(btns))
    for i, b in enumerate(btns[:5]):
        text = b.inner_text().strip()
        cls = b.get_attribute("class")
        print(f" [{i}] text: '{text}', class: '{cls}'")

    ctx.close()
