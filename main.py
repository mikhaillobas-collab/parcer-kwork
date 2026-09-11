import asyncio
import logging
import random
import re
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import config
from database import init_db, is_order_processed, save_order, get_stats, get_order_by_id
from gemini_analyzer import analyze_kwork_order, parse_budget_details
from kwork_parser import KworkBot
from tg_bot import (
    start_telegram_polling,
    send_order_card_for_approval,
    set_approve_handler,
    get_bot
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("kwork_bot")

async def run_parser_loop(bot: KworkBot):
    """Фоновый цикл периодического скрапинга и анализа биржи Kwork."""
    logger.info("🚀 Запуск фонового цикла мониторинга биржи Kwork...")
    iteration = 0

    while True:
        iteration += 1
        logger.info(f"\n--- [Итерация #{iteration}] Проверка биржи Kwork ---")

        try:
            orders = await bot.fetch_orders_from_exchange()
            new_orders = [o for o in orders if not is_order_processed(o["id"])]
            logger.info(f"Найдено карточек: {len(orders)} | Новых необработанных: {len(new_orders)}")

            for order in new_orders:
                order_id = order["id"]
                title = order["title"]
                description = order["description"]
                budget_info = order["budget_info"]
                offers_count = order.get("offers_count", 0)

                # 1. Фильтр конкуренции
                if offers_count > config.MAX_EXISTING_OFFERS:
                    logger.info(f"⏭️ Пропуск #{order_id} ('{title}'): уже {offers_count} предложений (лимит: {config.MAX_EXISTING_OFFERS}).")
                    save_order(
                        kwork_id=order_id,
                        title=title,
                        description=description,
                        budget_info=budget_info,
                        is_feasible=False,
                        reasoning=f"Превышен лимит предложений ({offers_count} > {config.MAX_EXISTING_OFFERS})",
                        status="SKIPPED_TOO_MANY_OFFERS"
                    )
                    continue

                # 2. Фильтр минимальной планки
                desired, max_allowed = parse_budget_details(budget_info)
                if max_allowed and max_allowed < config.MIN_ACCEPTABLE_PRICE:
                    logger.info(
                        f"⏭️ Пропуск #{order_id} ('{title}'): потолок бюджета ({max_allowed} ₽) "
                        f"ниже минимального порога ({config.MIN_ACCEPTABLE_PRICE} ₽)."
                    )
                    save_order(
                        kwork_id=order_id,
                        title=title,
                        description=description,
                        budget_info=budget_info,
                        desired_price=desired,
                        is_feasible=False,
                        reasoning=f"Потолок бюджета ({max_allowed} ₽) ниже минимальной планки ({config.MIN_ACCEPTABLE_PRICE} ₽)",
                        status="SKIPPED_BUDGET_TOO_LOW"
                    )
                    continue

                # 3. Фильтр исключенных тематик (видеопродакшн / контент-заводы)
                forbidden_topics = (
                    r"(?:контент[- ]завод|генераци[яиею]\s+(?:видео|ролик|рилс|reels|shorts|tiktok)|"
                    r"видеогенераци[яиею]|видеопродакшн|создани[ея]\s+(?:видео|ролик|shorts|reels|tiktok)|"
                    r"монтаж.*видео|съемк[аи]|видеомонтаж|озвучк[аи].*видео)"
                )
                if re.search(forbidden_topics, f"{title} {description}", re.IGNORECASE):
                    logger.info(f"⏭️ Пропуск #{order_id} ('{title}'): видеопроизводство/контент-заводы исключены.")
                    save_order(
                        kwork_id=order_id,
                        title=title,
                        description=description,
                        budget_info=budget_info,
                        desired_price=desired,
                        is_feasible=False,
                        reasoning="Исключенная тематика: видеопродакшн / контент-заводы / генерация видео",
                        status="SKIPPED_FORBIDDEN_TOPIC"
                    )
                    continue

                # 4. Анализ через Gemini
                logger.info(f"\n🔍 Анализ заказа #{order_id}: '{title}' (откликов: {offers_count})")
                try:
                    analysis = await asyncio.to_thread(analyze_kwork_order, title, description, budget_info)
                except Exception as e:
                    logger.error(f"Не удалось проанализировать заказ #{order_id} через Gemini: {e}")
                    continue

                if not analysis.is_feasible:
                    logger.info(f"❌ Заказ #{order_id} признан нецелесообразным: {analysis.reasoning}")
                    save_order(
                        kwork_id=order_id,
                        title=title,
                        description=description,
                        budget_info=budget_info,
                        desired_price=analysis.desired_budget,
                        is_feasible=False,
                        reasoning=analysis.reasoning,
                        status="REJECTED_UNFEASIBLE"
                    )
                    await asyncio.sleep(2.0)
                    continue

                # 5. Задача целесообразна: сохраняем в БД со статусом WAITING_APPROVAL
                logger.info(f"✅ Заказ #{order_id} подходит! Подготовлен отклик. Отправляем в Telegram на согласование...")
                form_price = getattr(analysis, "kwork_form_price", None) or getattr(analysis, "final_offer_price", 1000)
                real_price = getattr(analysis, "real_suggested_price", None) or getattr(analysis, "final_offer_price", 1000)

                save_order(
                    kwork_id=order_id,
                    title=title,
                    description=description,
                    budget_info=budget_info,
                    desired_price=analysis.desired_budget,
                    is_feasible=True,
                    reasoning=analysis.reasoning,
                    tech_stack=analysis.tech_stack,
                    proposal_title=analysis.proposal_title,
                    proposal_text=analysis.proposal_text,
                    duration_days=analysis.duration_days,
                    status="WAITING_APPROVAL"
                )

                # Отправка карточки согласования с инлайн-кнопками в Telegram
                await send_order_card_for_approval(
                    order_id=order_id,
                    title=title,
                    budget_info=budget_info,
                    desired_budget=analysis.desired_budget,
                    suggested_price=real_price,
                    form_price=form_price,
                    proposal_text=analysis.proposal_text,
                    duration_days=analysis.duration_days
                )

                await asyncio.sleep(3.0)

        except Exception as e:
            logger.error(f"Ошибка во время итерации мониторинга: {e}", exc_info=True)

        delay = random.uniform(config.CHECK_INTERVAL_MIN, config.CHECK_INTERVAL_MAX)
        logger.info(f"⏳ Ожидание {int(delay)} сек. до следующей проверки биржи...")
        await asyncio.sleep(delay)

async def main():
    print("=" * 65)
    print("🤖 KWORK AUTOMATION BOT (HUMAN-IN-THE-LOOP + AIOGRAM 3)")
    print("=" * 65)

    if not config.GEMINI_API_KEY or config.GEMINI_API_KEY == "your_gemini_api_key_here":
        print("\n❌ ОШИБКА: Не задан GEMINI_API_KEY в .env!")
        sys.exit(1)

    print(f"🔹 Модель Gemini: {config.GEMINI_MODEL}")
    print(f"🔹 URL биржи: {config.KWORK_URL}")
    print(f"🔹 Режим DRY_RUN: {'ВКЛЮЧЕН (тест)' if config.DRY_RUN else 'ВЫКЛЮЧЕН (боевой)'}")
    print(f"🔹 Браузер: {'Скрытый (headless)' if config.HEADLESS else 'Видимый (экран)'}")
    print(f"🔹 Telegram-уведомления: {'ВКЛЮЧЕНЫ' if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID else 'ВЫКЛЮЧЕНЫ'}")
    print("=" * 65)

    # Инициализация БД
    init_db()
    stats = get_stats()
    print(f"📊 Текущая статистика базы данных: {stats}\n")

    # Авто-установка Chromium Playwright для облака
    try:
        import subprocess
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    except Exception as e:
        logger.warning(f"Предупреждение при загрузке Chromium: {e}")

    # Инициализируем браузер
    bot_browser = KworkBot()
    await bot_browser.start_browser()

    if not await bot_browser.check_authorization():
        logger.error("Не удалось подтвердить авторизацию в Kwork. Завершение работы.")
        await bot_browser.close_browser()
        return

    # Регистрируем обработчик для кнопки "Отправить отклик" из Telegram
    async def handle_telegram_approval(order_id: str):
        order_data = get_order_by_id(order_id)
        if not order_data:
            return False, None, f"Заказ #{order_id} не найден в базе данных"

        return await bot_browser.submit_proposal_by_id(
            order_id=order_id,
            proposal_title=order_data.get("proposal_title") or f"Заказ #{order_id}",
            proposal_text=order_data.get("proposal_text") or "",
            price=order_data.get("desired_price") or 1000,
            duration_days=order_data.get("duration_days") or 3
        )

    set_approve_handler(handle_telegram_approval)

    # Отправляем приветственное сообщение в Telegram
    t_bot = get_bot()
    if t_bot and config.TELEGRAM_CHAT_ID:
        try:
            await t_bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text="🟢 <b>Kwork Automation Bot запущен в режиме согласования!</b>\n"
                     "Когда бот найдет подходящий заказ, вы получите сообщение с кнопками <b>[🚀 Отправить]</b> и <b>[❌ Пропустить]</b>."
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить стартовое сообщение в Telegram: {e}")

    try:
        # Запускаем одновременно Telegram-бота и фоновый цикл парсера
        await asyncio.gather(
            start_telegram_polling(),
            run_parser_loop(bot_browser)
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("\n🛑 Остановка приложения пользователем...")
    finally:
        logger.info("Закрытие браузера Playwright...")
        await bot_browser.close_browser()
        if t_bot:
            await t_bot.session.close()
        logger.info("Приложение полностью остановлено.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
