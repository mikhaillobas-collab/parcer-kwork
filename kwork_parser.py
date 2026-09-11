import asyncio
import json
import base64
import logging
import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from playwright.async_api import async_playwright, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

import config
from database import is_order_processed, save_order, update_order_status
from gemini_analyzer import analyze_kwork_order

logger = logging.getLogger("kwork_bot")

class KworkBot:
    def __init__(self):
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def start_browser(self) -> None:
        """Запускает браузер с постоянным профилем сессии через асинхронный Playwright."""
        logger.info(f"Запуск браузера (профиль: {config.USER_DATA_DIR}, headless={config.HEADLESS})...")
        self.playwright = await async_playwright().start()
        
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--disable-gpu"
        ]
        
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(config.USER_DATA_DIR),
                headless=config.HEADLESS,
                args=launch_args,
                viewport={"width": 1440, "height": 900},
                locale="ru-RU",
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
            )
        except Exception as e:
            if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
                logger.info("📦 Бинарники Chromium не найдены на хостинге. Загрузка...")
                import subprocess
                subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
                self.context = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir=str(config.USER_DATA_DIR),
                    headless=config.HEADLESS,
                    args=launch_args,
                    viewport={"width": 1440, "height": 900},
                    locale="ru-RU",
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
                )
            else:
                raise e

        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        # Внедрение куков Kwork
        if config.KWORK_COOKIES:
            try:
                c_str = config.KWORK_COOKIES
                if c_str.startswith("base64:"):
                    c_str = base64.b64decode(c_str[7:]).decode("utf-8")
                cookies_list = json.loads(c_str)
                if isinstance(cookies_list, list):
                    await self.context.add_cookies(cookies_list)
                    logger.info(f"🔑 Успешно внедрено {len(cookies_list)} куков авторизации из KWORK_COOKIES!")
            except Exception as e:
                logger.warning(f"Ошибка при импорте KWORK_COOKIES: {e}")

        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.set_default_timeout(20000)

    async def close_browser(self) -> None:
        """Безопасное закрытие браузера."""
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass
        self.context = None
        self.playwright = None
        self.page = None

    async def ensure_browser_alive(self) -> None:
        """Проверяет жизнеспособность браузера и перезапускает при сбое."""
        needs_restart = False
        try:
            if not self.context or not self.page or self.page.is_closed():
                needs_restart = True
            else:
                _ = await self.page.title()
        except Exception:
            needs_restart = True

        if needs_restart:
            logger.warning("Соединение с браузером прервано. Восстановление сессии...")
            await self.close_browser()
            await self.start_browser()

    async def check_authorization(self) -> bool:
        """Проверяет авторизацию пользователя."""
        logger.info("Проверка статуса авторизации на kwork.ru...")
        await self.page.goto("https://kwork.ru/", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        logged_in_selectors = [
            ".user-avatar",
            ".header__user",
            "a[href*='/user/']",
            ".balance-item",
            ".profile-menu"
        ]

        async def is_logged_in() -> bool:
            for sel in logged_in_selectors:
                loc = self.page.locator(sel).first
                if await loc.is_visible():
                    return True
            return False

        if await is_logged_in():
            logger.info("✅ Вы уже успешно авторизованы в Kwork!")
            return True

        logger.warning(
            "⚠️ Аккаунт Kwork не авторизован!\n"
            "Пожалуйста, выполните вход в открывшемся браузере (или передайте KWORK_COOKIES)."
        )

        for _ in range(60):
            await asyncio.sleep(5)
            if await is_logged_in():
                logger.info("🎉 Авторизация успешно выполнена! Сессия сохранена.")
                return True

        logger.error("❌ Время ожидания входа истекло.")
        return False

    async def fetch_orders_from_exchange(self) -> List[Dict[str, Any]]:
        """Загружает страницу биржи и собирает новые карточки."""
        await self.ensure_browser_alive()
        url = config.KWORK_URL
        logger.info(f"Переход на биржу: {url}")
        await self.page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(4)

        cards = await self.page.locator(".want-card, div.project-card, div[data-id]").all()
        if not cards:
            cards = await self.page.locator("div:has(a[href*='/projects/'][href*='/view'])").all()

        logger.info(f"Найдено карточек на странице: {len(cards)}")
        extracted_orders = []

        for idx, card in enumerate(cards):
            try:
                link_elem = card.locator("a[href*='/projects/']").first
                if not await link_elem.is_visible():
                    continue

                href = await link_elem.get_attribute("href") or ""
                match_id = re.search(r"/projects/(\d+)", href)
                if not match_id:
                    continue
                order_id = match_id.group(1)

                if is_order_processed(order_id):
                    continue

                title = (await link_elem.inner_text()).strip()

                expand_btn = card.locator("span.kw-link-dashed:has-text('Показать полностью'), :text('Показать полностью')").first
                if await expand_btn.is_visible():
                    try:
                        await expand_btn.click(timeout=2000)
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass

                description = ""
                expanded_block = card.locator(".wants-card__description-text div:has(span:has-text('Скрыть')) .d-inline, .want-card__description-text div:has(span:has-text('Скрыть')) .d-inline").first
                if await expanded_block.count() > 0:
                    description = (await expanded_block.text_content()).strip()
                if not description:
                    desc_elem = card.locator(".wants-card__description-text, .want-card__description-text, .project-card__description, .w-break-word").first
                    if await desc_elem.is_visible():
                        description = (await desc_elem.inner_text()).strip()
                    else:
                        description = (await card.inner_text()).strip()

                budget_info = ""
                budget_elem = card.locator(".want-card__budget, .project-card__price, .w-budget, div:has-text('Желаемый бюджет')").first
                if await budget_elem.is_visible():
                    budget_info = (await budget_elem.inner_text()).strip()

                card_full_text = await card.inner_text()
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
                    "offers_count": offers_count
                })

            except Exception as e:
                logger.debug(f"Ошибка при парсинге карточки #{idx}: {e}")
                continue

        return extracted_orders

    async def submit_proposal_by_id(
        self,
        order_id: str,
        proposal_title: str,
        proposal_text: str,
        price: int,
        duration_days: int = 3
    ) -> Tuple[bool, Optional[Path], str]:
        """
        Открывает страницу заказа, заполняет форму и отправляет её на Kwork.
        Возвращает: (успех, путь_к_скриншоту, текст_ошибки).
        """
        await self.ensure_browser_alive()
        try:
            url = f"https://kwork.ru/projects/{order_id}/view"
            logger.info(f"Открытие страницы проекта: {url}...")
            await self.page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(2)

            # Ищем кнопку "Предложить услугу"
            offer_btn = self.page.locator(
                ".kw-button--green:has-text('Предложить услугу'), "
                "button:has-text('Предложить услугу'), "
                "a:has-text('Предложить услугу'), "
                "span:has-text('Предложить услугу'):not(.want-card__open-review)"
            ).first

            if not await offer_btn.is_visible():
                # Проверяем, возможно форма уже открыта на странице
                if await self.page.locator("textarea[placeholder*='Напишите, как вы будете решать'], textarea[name='description']").count() == 0:
                    return False, None, "Кнопка 'Предложить услугу' недоступна (проект закрыт или отклик уже подан)"

            if await offer_btn.is_visible():
                await offer_btn.scroll_into_view_if_needed()
                await offer_btn.click(force=True)
                await asyncio.sleep(2)

            # Ожидаем появления поля textarea (в модальном окне или на странице)
            desc_textarea = None
            selectors = [
                ".modal-dialog textarea",
                ".modal-content textarea",
                ".b-modal textarea",
                ".popup textarea",
                "textarea[placeholder*='Напишите, как вы будете решать']",
                "textarea[name='description']",
                "textarea"
            ]

            for attempt in range(3):
                for sel in selectors:
                    loc = self.page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        desc_textarea = loc
                        break
                if desc_textarea:
                    break
                await asyncio.sleep(1)

            if not desc_textarea:
                # Делаем скриншот страницы, чтобы точно увидеть состояние экрана
                err_shot = config.SCREENSHOTS_DIR / f"notextarea_{order_id}.png"
                await self.page.screenshot(path=str(err_shot), full_page=True)
                return False, err_shot, "Поле ввода отклика (textarea) не появилось после клика на кнопку предложения"

            await desc_textarea.scroll_into_view_if_needed()
            await desc_textarea.click()
            await desc_textarea.fill(proposal_text)
            try:
                handle = await desc_textarea.element_handle()
                await self.page.evaluate("""(el) => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""", handle)
            except Exception:
                pass

            # 2. Поле Стоимость
            price_input = self.page.locator("input[placeholder*=' - '], input[name='price'], div:has-text('Стоимость') + div input, div:has-text('Стоимость') input").first
            if await price_input.is_visible() and price:
                placeholder = await price_input.get_attribute("placeholder") or ""
                ph_clean = placeholder.replace(" ", "").replace("\xa0", "")
                range_match = re.findall(r"\d+", ph_clean)
                target_price = price
                if len(range_match) >= 2:
                    max_limit = int(range_match[-1])
                    if target_price > max_limit:
                        target_price = max_limit
                await price_input.fill(str(target_price))

            # 3. Поле Название
            title_input = self.page.locator("input[placeholder*='Введите название заказа'], input[name='title']").first
            if await title_input.is_visible():
                await title_input.fill(proposal_title[:70])

            # 4. Срок
            duration = duration_days if duration_days in (2, 3) else 3
            duration_select = self.page.locator("select[name*='term'], select[name*='duration'], select").first
            if await duration_select.is_visible():
                try:
                    await duration_select.select_option(label=re.compile(f"{duration}"))
                except Exception:
                    await duration_select.select_option(value=str(duration))
            else:
                dropdown_trigger = self.page.locator("div:has-text('Срок выполнения'), .duration-select, .select-styled").last
                if await dropdown_trigger.is_visible():
                    await dropdown_trigger.click()
                    await asyncio.sleep(1)
                    option_item = self.page.locator(f"li:has-text('{duration} дн'), div:has-text('{duration} дн'), span:has-text('{duration} дн')").first
                    if await option_item.is_visible():
                        await option_item.click()
                        await asyncio.sleep(0.5)

            # Скриншот
            shot_file = config.SCREENSHOTS_DIR / f"offer_{order_id}.png"
            try:
                if await modal.is_visible():
                    await modal.screenshot(path=str(shot_file))
                else:
                    await self.page.screenshot(path=str(shot_file), full_page=False)
            except Exception:
                await self.page.screenshot(path=str(shot_file), full_page=False)

            if config.DRY_RUN:
                logger.info(f"🛡️ [DRY_RUN] Отклик на #{order_id} подтвержден, моделируем отправку.")
                close_btn = self.page.locator("button.close, .modal-close, span:has-text('✕'), .popup-close").first
                if await close_btn.is_visible():
                    await close_btn.click()
                else:
                    await self.page.keyboard.press("Escape")
                return True, shot_file, ""

            # Реальная отправка
            submit_btn = self.page.locator("button:has-text('Предложить'), input[type='submit'][value='Предложить']").first
            await submit_btn.click()
            await asyncio.sleep(3)
            logger.info(f"✅ Отклик на заказ #{order_id} успешно отправлен на Kwork!")

            confirm_shot = config.SCREENSHOTS_DIR / f"sent_{order_id}.png"
            try:
                await self.page.screenshot(path=str(confirm_shot), full_page=False)
                return True, confirm_shot, ""
            except Exception:
                return True, shot_file, ""

        except Exception as e:
            logger.error(f"❌ Ошибка отправки отклика #{order_id}: {e}")
            err_shot = config.SCREENSHOTS_DIR / f"err_{order_id}.png"
            try:
                await self.page.screenshot(path=str(err_shot))
            except Exception:
                pass
            return False, err_shot, str(e)
