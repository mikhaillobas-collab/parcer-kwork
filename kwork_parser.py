import asyncio
import json
import base64
import html
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

# Состояние формы отклика на kwork.ru/new_offer так, как его видит Kwork.
# values — значения из Vue-компонента поля: проверяемое Kwork перед отправкой и отправляемое (v-model).
OFFER_FORM_STATE_JS = r"""() => {
    const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
    const clean = s => (s || '').replace(/\s+/g, ' ').trim();
    const editorField = name => {
        const textarea = document.querySelector(`textarea[name="${name}"]`);
        const box = textarea ? textarea.closest('.trumbowyg-box') : null;
        const editor = box ? box.querySelector('.trumbowyg-editor') : null;
        let values = null, counter = null, counterError = false, textError = '';
        for (let el = textarea, i = 0; el && i < 12; el = el.parentElement, i++) {
            const vm = el.__vue__;
            if (vm && 'textPreSubmitMessage' in vm) {
                values = [String(vm.textPreSubmitMessage || '')];
                if (typeof vm.value === 'string') values.push(vm.value);
                // Счётчик символов Kwork: при ошибке счётчика Kwork не даёт отправить форму
                counter = typeof vm.lengthValue === 'number' ? vm.lengthValue : null;
                counterError = !!vm.errorTextCounter;
                textError = clean(vm.textError);
                break;
            }
        }
        return {visible: visible(editor), text: editor ? editor.innerText.trim() : '', values, counter, counterError, textError};
    };
    const price = document.querySelector('#offer-custom-price');
    const payment = document.querySelector('.offer-payment-type__items');
    const duration = document.querySelector('.duration-select');
    // Выбранный срок Kwork показывает значением input внутри .vs__selected
    const selected = duration ? duration.querySelector('.vs__selected') : null;
    const selectedInput = selected ? selected.querySelector('input') : null;
    return {
        description: editorField('description'),
        name: editorField('name'),
        priceVisible: visible(price),
        price: price ? price.value : '',
        paymentVisible: visible(payment),
        paymentChosen: !!(payment && payment.querySelector('.offer-payment-type__item.active')),
        durationVisible: visible(duration),
        duration: selectedInput ? clean(selectedInput.value) : clean(selected ? selected.textContent : ''),
        errors: [...document.querySelectorAll('.form-item__error, .offer-individual__error, .offer-individual__total-price-error')]
            .filter(visible).map(el => clean(el.textContent)).filter(Boolean),
    };
}"""

VISIBLE_POPUPS_JS = r"""() => [...document.querySelectorAll(".modal, .kw-modal, .vm--modal, [role='dialog']")]
    .filter(el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden')
    .map(el => el.innerText.replace(/\s+/g, ' ').trim().slice(0, 300))
    .filter(Boolean)"""

# Данные, которые Kwork встраивает в страницу биржи (window.stateData):
# favourites — любимые рубрики аккаунта {id рубрики: название}, wantRubrics — рубрики заказов на странице {id заказа: id рубрики}
EXCHANGE_RUBRICS_JS = r"""() => {
    const st = window.stateData || {};
    const fav = st.favouriteCategories;
    const favourites = {};
    for (const c of Array.isArray(fav) ? fav : Object.values(fav || {})) {
        if (c && c.category_id) favourites[String(c.category_id)] = String(c.name || c.category_id);
    }
    const wantRubrics = {};
    for (const w of ((st.pagination || {}).data || [])) {
        if (w && w.id) wantRubrics[String(w.id)] = String(w.category_id);
    }
    return {favourites, wantRubrics};
}"""

class FavouriteRubricsNotFound(Exception):
    """На бирже не видно любимых рубрик: слетел вход в Kwork или в аккаунте не отмечено ни одной любимой рубрики."""

class KworkBot:
    def __init__(self):
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        # Заказы, по которым сейчас идёт отправка отклика (защита от двойного нажатия в Telegram)
        self._submitting: set = set()
        # Любимые рубрики с прошлой проверки биржи — чтобы писать список в лог только при изменении
        self._favourites: Dict[str, str] = {}

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
        """Загружает биржу во вкладке «Любимые» и собирает новые карточки из любимых рубрик."""
        await self.ensure_browser_alive()
        url = config.KWORK_FAVOURITES_URL
        logger.info(f"Переход на биржу: {url}")
        await self.page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(4)

        # Без входа в Kwork вкладка «Любимые» показывает все рубрики подряд — поэтому сверяем рубрику каждой карточки
        rubrics = await self.page.evaluate(EXCHANGE_RUBRICS_JS)
        favourites: Dict[str, str] = rubrics["favourites"]
        want_rubrics: Dict[str, str] = rubrics["wantRubrics"]
        if not favourites:
            self._favourites = {}
            raise FavouriteRubricsNotFound(
                "На бирже не видно любимых рубрик: слетел вход в Kwork или в аккаунте не отмечено ни одной любимой рубрики"
            )
        if favourites != self._favourites:
            logger.info(f"⭐ Любимые рубрики на Kwork: {', '.join(favourites.values())}")
            self._favourites = favourites

        cards = await self.page.locator(".want-card, div.project-card, div[data-id]").all()
        if not cards:
            cards = await self.page.locator("div:has(a[href*='/projects/'][href*='/view'])").all()

        logger.info(f"Найдено карточек на странице: {len(cards)}")
        extracted_orders = []
        other_rubric_ids = set()

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

                rubric = favourites.get(want_rubrics.get(order_id, ""))
                if not rubric:
                    other_rubric_ids.add(order_id)
                    continue

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

                # Бюджет: "Желаемый бюджет: до X ₽ Допустимый: до Y ₽" или "Цена до: X ₽" — отдельный блок справа в карточке
                budget_info = ""
                budget_elem = card.locator(".wants-card__right, .want-card__right, .want-card__budget, .project-card__price, .w-budget").first
                if await budget_elem.count() > 0 and await budget_elem.is_visible():
                    budget_info = " ".join((await budget_elem.inner_text()).split())

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
                    "offers_count": offers_count,
                    "rubric": rubric
                })

            except Exception as e:
                logger.debug(f"Ошибка при парсинге карточки #{idx}: {e}")
                continue

        if other_rubric_ids:
            logger.warning(f"Пропущено карточек не из любимых рубрик: {len(other_rubric_ids)}")
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
        Открывает форму отклика (kwork.ru/new_offer?project=ID), заполняет её и отправляет на Kwork.
        Работает в отдельной вкладке, чтобы не мешать циклу мониторинга биржи в self.page.
        Возвращает: (успех, путь_к_скриншоту, текст_ошибки).
        """
        order_id = str(order_id)
        if order_id in self._submitting:
            return False, None, "Отклик на этот заказ уже отправляется"
        self._submitting.add(order_id)

        page: Optional[Page] = None
        try:
            await self.ensure_browser_alive()
            page = await self.context.new_page()
            page.set_default_timeout(20000)
            return await self._fill_and_send_offer(
                page, order_id, proposal_title, proposal_text, price, duration_days
            )
        except Exception as e:
            logger.error(f"❌ Ошибка отправки отклика #{order_id}: {e}")
            err_text = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            err_shot = await self._offer_screenshot(page, f"err_{order_id}.png")
            return False, err_shot, err_text
        finally:
            self._submitting.discard(order_id)
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

    async def _fill_and_send_offer(
        self,
        page: Page,
        order_id: str,
        proposal_title: str,
        proposal_text: str,
        price: int,
        duration_days: int
    ) -> Tuple[bool, Optional[Path], str]:
        url = f"https://kwork.ru/projects/{order_id}/view"
        logger.info(f"Открытие страницы проекта: {url}...")
        await page.goto(url, wait_until="domcontentloaded")

        # 1. Кнопка "Предложить услугу" ведёт на отдельную страницу формы /new_offer?project=ID
        offer_btn = page.locator(
            ".projects-offer-btn, .kw-button--green:not(.want-card__open-review), button, a"
        ).filter(has_text="Предложить услугу").first
        if not await self._wait_visible(offer_btn, 10000):
            shot = await self._offer_screenshot(page, f"err_{order_id}.png")
            return False, shot, "Кнопка 'Предложить услугу' недоступна (проект закрыт или отклик уже подан)"

        popups_before = await self._visible_popup_texts(page)
        await offer_btn.scroll_into_view_if_needed()
        try:
            await offer_btn.click(timeout=10000)
        except PlaywrightTimeoutError:
            await offer_btn.click(force=True)

        try:
            await page.wait_for_url(re.compile(r"/new_offer"), wait_until="domcontentloaded", timeout=20000)
        except PlaywrightTimeoutError:
            # Вместо формы Kwork показал окно (закончились коннекты, нужно портфолио и т.п.)
            popup = await self._new_popup_text(page, popups_before)
            shot = await self._offer_screenshot(page, f"err_{order_id}.png")
            return False, shot, "Kwork не открыл форму отклика" + (f": {popup}" if popup else "")

        # 2. Описание. На Kwork это визуальный редактор Trumbowyg: видимый div[contenteditable]
        # и скрытая textarea под ним. Kwork берёт текст из редактора, поэтому вводим именно туда.
        if not await self._wait_visible(self._editor_locator(page, "description"), 20000):
            shot = await self._offer_screenshot(page, f"err_{order_id}.png")
            return False, shot, "Форма отклика не загрузилась: нет поля 'Описание'"

        # Цена должна входить в диапазон Kwork (подсказка поля «Стоимость»: "500 - 1 000"). Молча подгонять её нельзя:
        # сумма в тексте отклика разойдётся с формой — пусть пользователь сам изменит цену в Telegram
        price = int(price or 0)
        price_input = page.locator("#offer-custom-price").first
        price_visible = await price_input.is_visible()
        if price_visible:
            min_price, max_price = self._parse_price_range(await price_input.get_attribute("placeholder") or "")
            if (min_price and price < min_price) or (max_price and price > max_price):
                shot = await self._offer_screenshot(page, f"err_{order_id}.png")
                return False, shot, (
                    f"Цена {price} ₽ вне допустимого диапазона Kwork для этого заказа: от {min_price} до {max_price} ₽. "
                    f"Нажмите «Изменить цену» и отправьте снова"
                )
        await self._type_into_editor(page, "description", proposal_text)

        # 3. Стоимость: цена по правилам бота или заданная вручную в Telegram
        if price_visible:
            await price_input.fill(str(price))
            await price_input.evaluate("el => el.blur()")

        # 4. Порядок оплаты (появляется при цене от ~4 000 ₽): "Целиком, когда заказ выполнен".
        # Вариант "По мере выполнения задач" требует расписывать этапы.
        payment_all = page.locator(".offer-payment-type__item").filter(has_text="Целиком").first
        if await self._wait_visible(payment_all, 1500):
            await payment_all.click()

        # 5. Название заказа — тоже редактор Trumbowyg, появляется после ввода цены
        if await self._wait_visible(self._editor_locator(page, "name"), 3000):
            await self._type_into_editor(page, "name", " ".join((proposal_title or "").split())[:70])

        # 6. Срок выполнения — выпадающий список vue-select
        await self._select_duration(page, duration_days if duration_days in (2, 3) else 3)

        # 7. Проверяем, что Kwork действительно принял значения полей
        await asyncio.sleep(0.5)
        state = await self._read_offer_form(page)
        problems = self._offer_form_problems(state, proposal_text)
        form_shot = await self._offer_screenshot(page, f"offer_{order_id}.png")
        if problems:
            return False, form_shot, "Форма отклика заполнена с ошибками: " + "; ".join(problems)
        logger.info(
            f"Форма отклика #{order_id} заполнена: описание {state['description']['counter']} симв. (по счётчику Kwork), "
            f"цена {state['price']} ₽, срок '{state['duration']}'"
        )

        if config.DRY_RUN:
            logger.info(f"🛡️ [DRY_RUN] Отклик на #{order_id} заполнен, кнопка 'Предложить' не нажимается.")
            return True, form_shot, ""

        # 8. Отправка и проверка ответа сервера Kwork
        submit_btn = page.locator(
            ".modal-individual-offer__buttons button, .offer-buttons button"
        ).filter(has_text="Предложить").first
        if not await self._wait_visible(submit_btn, 5000):
            return False, form_shot, "В форме не найдена кнопка 'Предложить'"

        offer_results: List[Tuple[int, Any]] = []

        async def collect_offer_response(response) -> None:
            if "/api/offer/createoffer" not in response.url and "/api/offer/editoffer" not in response.url:
                return
            try:
                data = await response.json()
            except Exception:
                data = None
            offer_results.append((response.status, data))

        page.on("response", collect_offer_response)
        popups_before = await self._visible_popup_texts(page)
        logger.info(f"🚀 Отправка отклика на заказ #{order_id}...")
        await submit_btn.click()

        for tick in range(60):  # до 30 секунд
            await asyncio.sleep(0.5)
            if offer_results:
                break
            if tick < 3:
                continue
            # Запрос на создание отклика не ушёл: ошибка проверки формы или окно-предупреждение Kwork
            try:
                popup = await self._new_popup_text(page, popups_before)
                errors = (await self._read_offer_form(page))["errors"]
            except Exception:
                continue  # страница перезагружается
            if popup or errors:
                shot = await self._offer_screenshot(page, f"err_{order_id}.png")
                return False, shot, "Kwork не принял отклик: " + (popup or "; ".join(errors))

        if not offer_results:
            shot = await self._offer_screenshot(page, f"err_{order_id}.png")
            return False, shot, "Kwork не ответил на отправку отклика за 30 секунд"

        status, data = offer_results[0]
        if isinstance(data, dict):
            accepted = status < 400 and data.get("status") != "error" and data.get("success") is not False
        else:
            # Тело ответа не прочиталось: при успехе Kwork уводит со страницы формы
            await asyncio.sleep(2)
            accepted = status < 400 and "/new_offer" not in page.url

        if accepted:
            logger.info(f"✅ Kwork принял отклик на заказ #{order_id}: {json.dumps(data, ensure_ascii=False)[:300]}")
            return True, form_shot, ""

        reason = self._offer_error_text(data) or f"HTTP {status}"
        logger.error(f"❌ Kwork отклонил отклик на заказ #{order_id}: {reason}")
        await asyncio.sleep(1)
        shot = await self._offer_screenshot(page, f"err_{order_id}.png")
        return False, shot, f"Kwork отклонил отклик: {reason}"

    @staticmethod
    def _editor_locator(page: Page, field_name: str):
        """Видимый редактор Trumbowyg для поля формы (textarea[name=...] под ним скрыта)."""
        return page.locator(f".trumbowyg-box:has(textarea[name='{field_name}']) .trumbowyg-editor").first

    async def _type_into_editor(self, page: Page, field_name: str, text: str) -> None:
        """
        Ввод текста в редактор Kwork так, как это делает пользователь: фокус, очистка черновика, текст построчно.
        Счётчик символов Kwork пересчитывается по событию input, но берёт текст из скрытой textarea,
        которая синхронизируется с редактором только по keyup. Поэтому в конце: End (keyup, текст не меняет)
        и событие input — иначе счётчик остаётся устаревшим (0 или без последней строки) и Kwork не примет форму.
        """
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        editor = self._editor_locator(page, field_name)
        await editor.scroll_into_view_if_needed()
        await editor.click()
        await page.keyboard.press("ControlOrMeta+A")
        await page.keyboard.press("Delete")
        for i, line in enumerate(text.split("\n")):
            if i:
                await page.keyboard.press("Enter")
            if line:
                await page.keyboard.insert_text(line)

        for _ in range(3):
            await page.keyboard.press("End")
            await editor.dispatch_event("input")
            await asyncio.sleep(0.3)
            field = (await self._read_offer_form(page))[field_name]
            if self._field_synced(field, text):
                break
            await editor.click()
        await editor.evaluate("el => el.blur()")

    async def _select_duration(self, page: Page, days: int) -> None:
        """Выбор срока в выпадающем списке Kwork: точное совпадение или ближайший больший срок."""
        select = page.locator(".duration-select").first
        if not await self._wait_visible(select, 3000):
            return
        await select.scroll_into_view_if_needed()
        await select.locator(".vs__dropdown-toggle").click()
        options = page.locator(".vs__dropdown-menu .vs__dropdown-option")
        if await self._wait_visible(options.first, 3000):
            available = []
            for idx, label in enumerate(await options.all_inner_texts()):
                match = re.match(r"\s*(\d+)", label)
                if match:
                    available.append((int(match.group(1)), idx))
            if available:
                exact = [idx for value, idx in available if value == days]
                longer = sorted((value, idx) for value, idx in available if value > days)
                pick = exact[0] if exact else (longer[0][1] if longer else max(available)[1])
                await options.nth(pick).click()
                return
        # Запасной вариант: поле поиска списка принимает число дней
        await select.locator("input.vs__search").fill(str(days))
        await page.keyboard.press("Enter")

    async def _read_offer_form(self, page: Page) -> Dict[str, Any]:
        """Текущее состояние формы отклика так, как его видит Kwork."""
        return await page.evaluate(OFFER_FORM_STATE_JS)

    @classmethod
    def _offer_form_problems(cls, state: Dict[str, Any], proposal_text: str) -> List[str]:
        """Незаполненные поля и ошибки формы отклика."""
        problems = []
        description = state["description"]
        expected_chars = len(re.sub(r"\s", "", proposal_text or ""))
        got_chars = cls._field_chars(description)
        if got_chars == 0:
            problems.append("описание не заполнено")
        elif got_chars < expected_chars * 0.98:
            problems.append(f"в описание попало {got_chars} из {expected_chars} знаков")
        if description["counterError"]:
            problems.append(f"счётчик символов Kwork показывает ошибку в описании ({description['counter']} симв.)")

        name = state["name"]
        if name["visible"] and cls._field_chars(name) == 0:
            problems.append("не заполнено название заказа")
        if name["visible"] and name["counterError"]:
            problems.append(f"счётчик символов Kwork показывает ошибку в названии ({name['counter']} симв.)")
        problems.extend(field["textError"] for field in (description, name) if field["textError"])
        if state["priceVisible"] and not re.sub(r"\D", "", state["price"] or ""):
            problems.append("не указана стоимость")
        if state["paymentVisible"] and not state["paymentChosen"]:
            problems.append("не выбран порядок оплаты")
        if state["durationVisible"] and not state["duration"]:
            problems.append("не выбран срок выполнения")
        problems.extend(state["errors"])
        return problems

    @staticmethod
    def _parse_price_range(placeholder: str) -> Tuple[Optional[int], Optional[int]]:
        """'500 - 1 000' -> (500, 1000). Разделитель тысяч может быть любым пробельным символом."""
        numbers = [re.sub(r"\D", "", part) for part in re.split(r"[-–—]", placeholder)]
        numbers = [int(n) for n in numbers if n]
        if len(numbers) >= 2:
            return numbers[0], numbers[-1]
        return None, None

    @staticmethod
    def _plain_text(value: Optional[str]) -> str:
        """HTML -> обычный текст."""
        return html.unescape(re.sub(r"<[^>]+>", " ", value or ""))

    @classmethod
    def _field_chars(cls, field: Dict[str, Any]) -> int:
        """Сколько непробельных знаков текста реально лежит в поле Kwork (минимум по всем его значениям)."""
        values = field["values"] if field["values"] is not None else [field["text"]]
        return min(len(re.sub(r"\s", "", cls._plain_text(value))) for value in values)

    @classmethod
    def _field_synced(cls, field: Dict[str, Any], text: str) -> bool:
        """Текст целиком дошёл до Kwork, и счётчик символов Kwork его учёл."""
        if field["values"] is None:  # внутреннее состояние Kwork недоступно — проверить нечем
            return True
        if cls._field_chars(field) < len(re.sub(r"\s", "", text)) * 0.98:
            return False
        # Kwork считает длину, схлопывая повторяющиеся пробелы и переводы строк
        kwork_length = len(re.sub(r"[\n ]{2,}", "\n", re.sub(r" {2,}", " ", text)).strip())
        return field["counter"] is None or field["counter"] >= kwork_length * 0.95

    @classmethod
    def _offer_error_text(cls, data: Any) -> str:
        """Текст ошибки из ответа Kwork на создание отклика."""
        if not isinstance(data, dict):
            return ""
        parts = [data[key] for key in ("response", "error", "message") if isinstance(data.get(key), str)]
        errors = data.get("errors")
        items = errors.values() if isinstance(errors, dict) else errors if isinstance(errors, list) else []
        for item in items:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, list) and item:
                parts.append(str(item[0]))
            elif item:
                parts.append(str(item))
        return " ".join(cls._plain_text(" ".join(parts)).split())[:400]

    async def _visible_popup_texts(self, page: Page) -> List[str]:
        """Тексты видимых всплывающих окон Kwork."""
        try:
            return await page.evaluate(VISIBLE_POPUPS_JS)
        except Exception:
            return []

    async def _new_popup_text(self, page: Page, before: List[str]) -> str:
        """Текст всплывающего окна, которого не было до действия."""
        for text in await self._visible_popup_texts(page):
            if text not in before:
                return text
        return ""

    async def _offer_screenshot(self, page: Optional[Page], filename: str) -> Optional[Path]:
        """Скриншот блока формы отклика целиком (если он на странице), иначе видимой части страницы."""
        if page is None or page.is_closed():
            return None
        path = config.SCREENSHOTS_DIR / filename
        try:
            form = page.locator(".modal-individual-offer").filter(has=page.locator(".trumbowyg-box")).first
            if await form.is_visible():
                await form.screenshot(path=str(path))
            else:
                await page.screenshot(path=str(path))
            return path
        except Exception as e:
            logger.warning(f"Не удалось сделать скриншот {filename}: {e}")
            return None

    @staticmethod
    async def _wait_visible(locator, timeout: int) -> bool:
        try:
            await locator.wait_for(state="visible", timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            return False
