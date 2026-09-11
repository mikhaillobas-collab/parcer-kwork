import logging
import re
import sys
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

import config
from database import is_order_processed, save_order, update_order_status
from gemini_analyzer import analyze_kwork_order

logger = logging.getLogger("kwork_bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

class KworkBot:
    def __init__(self):
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    def start_browser(self) -> None:
        """Запускает браузер с постоянным профилем сессии."""
        logger.info(f"Запуск браузера (профиль: {config.USER_DATA_DIR}, headless={config.HEADLESS})...")
        self.playwright = sync_playwright().start()
        
        # Настройки запуска для стабильности и обхода антифрод-систем (включая Linux Docker)
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--disable-gpu"
        ]
        
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(config.USER_DATA_DIR),
            headless=config.HEADLESS,
            args=launch_args,
            viewport={"width": 1440, "height": 900},
            locale="ru-RU",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        )
        # Скрываем автоматизацию от проверок JS
        self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(20000)

    def close_browser(self) -> None:
        """Безопасное закрытие браузера."""
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.context = None
        self.playwright = None
        self.page = None

    def ensure_browser_alive(self) -> None:
        """Проверяет, жив ли контекст браузера, и восстанавливает при разрыве соединения."""
        needs_restart = False
        try:
            if not self.context or not self.page or self.page.is_closed():
                needs_restart = True
            else:
                # Быстрый ping
                _ = self.page.title()
        except Exception:
            needs_restart = True

        if needs_restart:
            logger.warning("Соединение с браузером прервано. Восстановление сессии...")
            self.close_browser()
            self.start_browser()


    def check_authorization(self) -> bool:
        """
        Проверяет, авторизован ли пользователь.
        Если нет, ожидает ручного входа в открывшемся окне браузера.
        """
        logger.info("Проверка статуса авторизации на kwork.ru...")
        self.page.goto("https://kwork.ru/", wait_until="domcontentloaded")
        time.sleep(3)

        # Признаки авторизованного пользователя: наличие аватара, профиля или баланса
        logged_in_selectors = [
            ".user-avatar",
            ".header__user",
            "a[href*='/user/']",
            ".balance-item",
            ".profile-menu"
        ]

        def is_logged_in() -> bool:
            for sel in logged_in_selectors:
                if self.page.locator(sel).first.is_visible():
                    return True
            return False

        if is_logged_in():
            logger.info("✅ Вы уже успешно авторизованы в Kwork!")
            return True

        logger.warning(
            "⚠️ Аккаунт Kwork не авторизован!\n"
            "Пожалуйста, выполните вход в аккаунт в открывшемся окне браузера.\n"
            "Скрипт ждет завершения входа..."
        )

        # Ожидаем входа пользователя до 5 минут
        start_wait = time.time()
        while time.time() - start_wait < 300:
            time.sleep(3)
            if is_logged_in():
                logger.info("🎉 Авторизация успешно выполнена! Сессия сохранена в browser_profile.")
                return True

        logger.error("❌ Время ожидания входа истекло (5 минут).")
        return False

    def fetch_orders_from_exchange(self) -> List[Dict[str, Any]]:
        """
        Загружает страницу биржи по настроенным фильтрам
        и извлекает список новых карточек заказов.
        """
        self.ensure_browser_alive()
        url = config.KWORK_URL
        logger.info(f"Переход на биржу: {url}")
        self.page.goto(url, wait_until="domcontentloaded")
        time.sleep(4)


        # Селекторы карточек заказов на бирже Kwork
        # Обычно это .want-card или div с ссылкой /projects/{id}/view
        cards = self.page.locator(".want-card, div.project-card, div[data-id]").all()
        if not cards:
            # Запасной селектор для поиска блоков проектов
            cards = self.page.locator("div:has(a[href*='/projects/'][href*='/view'])").all()

        logger.info(f"Найдено карточек на странице: {len(cards)}")
        extracted_orders = []

        for idx, card in enumerate(cards):
            try:
                # Извлекаем ссылку на проект и ID
                link_elem = card.locator("a[href*='/projects/']").first
                if not link_elem.is_visible():
                    continue

                href = link_elem.get_attribute("href") or ""
                match_id = re.search(r"/projects/(\d+)", href)
                if not match_id:
                    continue
                order_id = match_id.group(1)

                # Проверяем, не обрабатывали ли уже этот заказ ранее
                if is_order_processed(order_id):
                    continue

                # Заголовок проекта
                title = link_elem.inner_text().strip()

                # Раскрываем полный текст (на Kwork это span.kw-link-dashed или текст 'Показать полностью')
                expand_btn = card.locator("span.kw-link-dashed:has-text('Показать полностью'), :text('Показать полностью')").first
                if expand_btn.is_visible():
                    try:
                        expand_btn.click(timeout=2000)
                        time.sleep(0.5)
                    except Exception:
                        pass

                # Извлекаем текст описания: ищем блок с полным текстом (контейнер перед ссылкой 'Скрыть')
                description = ""
                expanded_block = card.locator(".wants-card__description-text div:has(span:has-text('Скрыть')) .d-inline, .want-card__description-text div:has(span:has-text('Скрыть')) .d-inline").first
                if expanded_block.count() > 0:
                    description = expanded_block.text_content().strip()
                if not description:
                    desc_elem = card.locator(".wants-card__description-text, .want-card__description-text, .project-card__description, .w-break-word").first
                    if desc_elem.is_visible():
                        description = desc_elem.inner_text().strip()
                    else:
                        description = card.inner_text().strip()

                # Извлекаем блок бюджета
                budget_info = ""
                budget_elem = card.locator(".want-card__budget, .project-card__price, .w-budget, div:has-text('Желаемый бюджет')").first
                if budget_elem.is_visible():
                    budget_info = budget_elem.inner_text().strip()
                # Извлекаем количество уже поданных предложений (например: "Предложений: 5")
                card_full_text = card.inner_text()
                offers_count = 0
                offers_match = re.search(r"Предложений:\s*(\d+)", card_full_text, re.IGNORECASE)
                if not offers_match:
                    offers_match = re.search(r"Откликов:\s*(\d+)", card_full_text, re.IGNORECASE)
                if offers_match:
                    offers_count = int(offers_match.group(1))

                extracted_orders.append({
                    "id": order_id,
                    "title": title,
                    "description": description,
                    "budget_info": budget_info,
                    "offers_count": offers_count,
                    "card_locator": card
                })


            except Exception as e:
                logger.debug(f"Ошибка при парсинге карточки #{idx}: {e}")
                continue

        return extracted_orders

    def fill_and_submit_offer(self, order: Dict[str, Any], analysis: Any) -> bool:
        """
        Открывает форму отклика на заказ, заполняет поля
        и отправляет (или делает скриншот в режиме DRY_RUN).
        """
        card = order["card_locator"]
        order_id = order["id"]

        try:
            logger.info(f"Нажатие кнопки отклика на заказ #{order_id}...")
            # Кнопка отклика: СТРОГО зеленая кнопка "Предложить услугу", исключая "Оставить отзыв"
            offer_btn = card.locator(".kw-button--green:has-text('Предложить услугу'), span:has-text('Предложить услугу'):not(.want-card__open-review)").first
            
            if not offer_btn.is_visible():
                # Если на карточке списка кнопка не видна (например, статус ПРОСМОТРЕНО),
                # переходим на страницу проекта, где кнопка доступна всегда
                logger.info(f"Кнопка отклика не видна на карточке. Открываем https://kwork.ru/projects/{order_id}/view...")
                self.page.goto(f"https://kwork.ru/projects/{order_id}/view", wait_until="domcontentloaded")
                time.sleep(2)
                offer_btn = self.page.locator(".kw-button--green:has-text('Предложить услугу'), span:has-text('Предложить услугу'):not(.want-card__open-review)").first

            if not offer_btn.is_visible():
                logger.warning(f"Кнопка 'Предложить услугу' не найдена для заказа #{order_id} (возможно, уже откликнулись или проект закрыт).")
                return False

            offer_btn.scroll_into_view_if_needed()
            offer_btn.click(force=True)
            time.sleep(2)




            modal = self.page.locator(".modal-dialog, .modal-content, .b-modal, .popup, div:has(button:has-text('Предложить'))").first

            # 1. Поле "Описание" (textarea с подсказкой "Напишите, как вы будете решать задачу клиента")
            if modal.is_visible() and modal.locator("textarea").count() > 0:
                desc_textarea = modal.locator("textarea").first
            else:
                desc_textarea = self.page.locator("textarea[placeholder*='Напишите, как вы будете решать'], textarea[name='description'], textarea").first

            desc_textarea.wait_for(state="visible", timeout=10000)
            desc_textarea.scroll_into_view_if_needed()
            desc_textarea.click()
            desc_textarea.fill(analysis.proposal_text)
            try:
                # Программный вызов событий для 100% обновления счетчика символов Kwork
                self.page.evaluate("""(el) => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""", desc_textarea.element_handle())
            except Exception:
                pass
            logger.info(f"Заполнено описание ({len(analysis.proposal_text)} симв.)")

            # 2. Поле "Стоимость"
            price_input = self.page.locator("input[placeholder*=' - '], input[name='price'], div:has-text('Стоимость') + div input, div:has-text('Стоимость') input").first
            
            # Определяем верхний допустимый потолок Kwork
            kwork_max_limit = getattr(analysis, "max_allowed_budget", None)
            if price_input.is_visible():
                placeholder = price_input.get_attribute("placeholder") or ""
                ph_clean = placeholder.replace(" ", "").replace("\xa0", "")
                range_match = re.findall(r"\d+", ph_clean)
                if len(range_match) >= 2:
                    kwork_max_limit = int(range_match[-1])
                elif len(range_match) == 1 and int(range_match[0]) > 500 and not kwork_max_limit:
                    kwork_max_limit = int(range_match[0])

            # Целевая цена: берем kwork_form_price или final_offer_price
            target_price = (
                getattr(analysis, "kwork_form_price", None) or
                getattr(analysis, "final_offer_price", None) or
                getattr(analysis, "desired_budget", None) or
                1000
            )

            # ЗАЩИТА ВИЛКИ: если цена превышает лимит площадки, ставим ровно лимит!
            if kwork_max_limit and target_price > kwork_max_limit:
                logger.info(
                    f"⚠️ Желаемая цена ({target_price} ₽) превышает потолок Kwork ({kwork_max_limit} ₽). "
                    f"В инпут формы устанавливается допустимый предел: {kwork_max_limit} ₽ (предложение цены будет в тексте)."
                )
                target_price = kwork_max_limit

            if price_input.is_visible() and target_price:
                price_input.fill(str(target_price))
                logger.info(f"Заполнена стоимость: {target_price} ₽ (макс. потолок Kwork: {kwork_max_limit})")

            # 3. Поле "Название заказа" (input с подсказкой "Введите название заказа")
            title_input = self.page.locator("input[placeholder*='Введите название заказа'], input[name='title']").first
            if title_input.is_visible():
                title_text = analysis.proposal_title[:70]
                title_input.fill(title_text)
                logger.info(f"Заполнено название заказа: {title_text}")

            # 4. Поле "Срок выполнения" (выпадающий список, 2 или 3 дня)
            duration = analysis.duration_days
            logger.info(f"Выбор срока выполнения: {duration} дня...")

            # Проверяем нативный <select>
            duration_select = self.page.locator("select[name*='term'], select[name*='duration'], select").first
            if duration_select.is_visible():
                try:
                    duration_select.select_option(label=re.compile(f"{duration}"))
                except Exception:
                    duration_select.select_option(value=str(duration))
            else:
                dropdown_trigger = self.page.locator("div:has-text('Срок выполнения'), .duration-select, .select-styled").last
                if dropdown_trigger.is_visible():
                    dropdown_trigger.click()
                    time.sleep(1)
                    option_item = self.page.locator(f"li:has-text('{duration} дн'), div:has-text('{duration} дн'), span:has-text('{duration} дн')").first
                    if option_item.is_visible():
                        option_item.click()
                        time.sleep(0.5)

            # 5. Создание скриншота (ТЕКСТ ОТКЛИКА В ПРИОРИТЕТЕ)
            screenshot_path = config.SCREENSHOTS_DIR / f"dry_run_{order_id}.png"
            # Скроллим модальное окно наверх к тексту отклика
            try:
                desc_textarea.scroll_into_view_if_needed()
                time.sleep(0.5)
            except Exception:
                pass

            if config.DRY_RUN:
                # Пытаемся сделать скриншот самого модального окна (где видно всё: текст, цена, заголовок)
                try:
                    if modal.is_visible():
                        modal.screenshot(path=str(screenshot_path))
                    else:
                        self.page.screenshot(path=str(screenshot_path), full_page=False)
                except Exception:
                    self.page.screenshot(path=str(screenshot_path), full_page=False)

                logger.info(f"🛡️ [DRY_RUN] Предложение для заказа #{order_id} сформировано! Скриншот: {screenshot_path}")
                
                # Закрываем модальное окно (кнопка закрытия или Esc)
                close_btn = self.page.locator("button.close, .modal-close, span:has-text('✕'), .popup-close").first
                if close_btn.is_visible():
                    close_btn.click()
                else:
                    self.page.keyboard.press("Escape")
                if "/projects/" in self.page.url and "/view" in self.page.url:
                    self.page.goto(config.KWORK_URL, wait_until="domcontentloaded")
                return True
            else:
                # В боевом режиме кликаем кнопку "Предложить"
                submit_btn = self.page.locator("button:has-text('Предложить'), input[type='submit'][value='Предложить']").first
                logger.info(f"🚀 [PRODUCTION] Отправка отклика на заказ #{order_id}...")
                submit_btn.click()
                time.sleep(3)
                logger.info(f"✅ Отклик на заказ #{order_id} успешно отправлен!")
                if "/projects/" in self.page.url and "/view" in self.page.url:
                    self.page.goto(config.KWORK_URL, wait_until="domcontentloaded")
                return True


        except Exception as e:
            logger.error(f"❌ Ошибка при заполнении формы отклика #{order_id}: {e}")
            # Скриншот ошибки
            err_shot = config.SCREENSHOTS_DIR / f"error_{order_id}.png"
            try:
                self.page.screenshot(path=str(err_shot))
            except Exception:
                pass
            return False
