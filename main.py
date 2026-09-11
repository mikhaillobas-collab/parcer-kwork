import logging
import random
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import config
from database import init_db, is_order_processed, save_order, get_stats
from gemini_analyzer import analyze_kwork_order
from kwork_parser import KworkBot
from notifier import notify_order_offer


logger = logging.getLogger("kwork_bot")

def main():
    print("=" * 65)
    print("🤖 KWORK AUTOMATION BOT (GEMINI 3.7 / 3.8 / 2.5 POWERED)")
    print("=" * 65)

    # Проверка ключа API
    if not config.GEMINI_API_KEY or config.GEMINI_API_KEY == "your_gemini_api_key_here":
        print("\n❌ ОШИБКА: Не задан GEMINI_API_KEY в файле .env!")
        print("Пожалуйста, создайте или откройте файл .env и вставьте ваш ключ:")
        print("GEMINI_API_KEY=AIzaSy...")
        print("Ключ можно получить бесплатно на https://aistudio.google.com/\n")
        sys.exit(1)

    print(f"🔹 Модель Gemini: {config.GEMINI_MODEL}")
    print(f"🔹 URL биржи: {config.KWORK_URL}")
    print(f"🔹 Режим DRY_RUN (безопасный тест): {'ВКЛЮЧЕН (отклики НЕ списываются)' if config.DRY_RUN else 'ВЫКЛЮЧЕН (БОЕВОЙ РЕЖИМ)'}")
    print(f"🔹 Браузер: {'Скрытый (headless)' if config.HEADLESS else 'Видимый (окно на экране)'}")
    print(f"🔹 Интервал проверки: {config.CHECK_INTERVAL_MIN} - {config.CHECK_INTERVAL_MAX} сек.")
    print("=" * 65)

    # Инициализация БД
    init_db()
    stats = get_stats()
    print(f"📊 Текущая статистика базы данных: {stats}\n")

    bot = KworkBot()
    try:
        bot.start_browser()

        # Проверка и ожидание авторизации пользователя
        if not bot.check_authorization():
            logger.error("Не удалось подтвердить авторизацию. Завершение работы.")
            return

        logger.info("🚀 Запуск цикла регулярного мониторинга биржи...")

        iteration = 0
        while True:
            iteration += 1
            logger.info(f"\n--- [Итерация #{iteration}] Проверка биржи Kwork ---")

            try:
                orders = bot.fetch_orders_from_exchange()
                new_orders = [o for o in orders if not is_order_processed(o["id"])]
                logger.info(f"Новых необработанных заказов: {len(new_orders)}")

                for order in new_orders:
                    order_id = order["id"]
                    title = order["title"]
                    description = order["description"]
                    budget_info = order["budget_info"]
                    offers_count = order.get("offers_count", 0)

                    # Фильтр: если предложений по заказу > MAX_EXISTING_OFFERS (по умолчанию 10), не отправляем
                    if offers_count > config.MAX_EXISTING_OFFERS:
                        logger.info(f"⏭️ Пропуск заказа #{order_id} ('{title}'): уже {offers_count} предложений (лимит: {config.MAX_EXISTING_OFFERS}).")
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

                    logger.info(f"\n🔍 Анализ заказа #{order_id}: '{title}' (уже подано откликов: {offers_count})")
                    logger.info(f"Бюджет: {budget_info}")

                    try:
                        analysis = analyze_kwork_order(title, description, budget_info)

                    except Exception as e:
                        logger.error(f"Не удалось проанализировать заказ #{order_id} через Gemini: {e}")
                        continue

                    logger.info(f"Выполнимость: {'✅ ДА' if analysis.is_feasible else '❌ НЕТ'}")
                    logger.info(f"Обоснование: {analysis.reasoning}")

                    if not analysis.is_feasible:
                        # Сохраняем как невыполнимый, чтобы больше не парсить
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
                        time.sleep(3.0)
                        continue

                    # Задача выполнима — готовим отклик
                    logger.info(f"Рекомендуемый стек: {analysis.tech_stack}")
                    logger.info(f"Срок выполнения: {analysis.duration_days} дня")
                    logger.info(f"Название заказа: '{analysis.proposal_title}'")
                    logger.info(f"Желаемая цена: {analysis.desired_budget} ₽")
                    logger.info(f"Текст отклика ({len(analysis.proposal_text)} симв.):\n{analysis.proposal_text}\n")

                    # Открываем форму и заполняем
                    success = bot.fill_and_submit_offer(order, analysis)
                    status = ("DRY_RUN_SAVED" if config.DRY_RUN else "SUBMITTED") if success else "ERROR"

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
                        status=status
                    )

                    # Отправка уведомления в Telegram (с отчетом и скриншотом)
                    if success:
                        shot_file = config.SCREENSHOTS_DIR / f"dry_run_{order_id}.png"
                        notify_order_offer(
                            order_id=order_id,
                            title=title,
                            budget_info=budget_info,
                            form_price=getattr(analysis, "kwork_form_price", None) or getattr(analysis, "final_offer_price", 1000),
                            real_price=getattr(analysis, "real_suggested_price", None) or getattr(analysis, "final_offer_price", 1000),
                            proposal_text=analysis.proposal_text,
                            duration_days=analysis.duration_days,
                            is_dry_run=config.DRY_RUN,
                            screenshot_path=shot_file if shot_file.exists() else None
                        )

                    # Небольшая пауза между отправкой нескольких откликов
                    time.sleep(random.uniform(4.0, 7.0))

            except Exception as e:
                logger.error(f"Ошибка во время итерации мониторинга: {e}", exc_info=True)

            # Пауза перед следующей проверкой биржи
            delay = random.uniform(config.CHECK_INTERVAL_MIN, config.CHECK_INTERVAL_MAX)
            logger.info(f"⏳ Ожидание {int(delay)} сек. до следующей проверки биржи...")
            time.sleep(delay)

    except KeyboardInterrupt:
        logger.info("\n🛑 Получен сигнал остановки пользователем (Ctrl+C).")
    finally:
        logger.info("Завершение сессии и сохранение состояния браузера...")
        bot.close_browser()
        logger.info("Бот остановлен.")

if __name__ == "__main__":
    main()
