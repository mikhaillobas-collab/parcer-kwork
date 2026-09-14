import sys
from gemini_analyzer import analyze_kwork_order
from database import init_db, save_order, get_stats

TEST_CASES = [
    {
        "title": "Сбор данных и парсинг товаров с интернет-магазина в Excel",
        "budget_info": "Желаемый бюджет покупателя: до 3 000 ₽\nДопустимый: до 5 000 ₽",
        "description": (
            "Необходимо разработать скрипт для сбора каталога товаров с сайта интернет-магазина. "
            "Нужно спарсить наименования, цены, артикулы, характеристики и ссылки на фото. "
            "Результат сохранить в Excel файл (.xlsx). Желательно на Python."
        )
    },
    {
        "title": "Расклейка объявлений по подъездам в г. Москва",
        "budget_info": "Желаемый бюджет: до 1 500 ₽",
        "description": (
            "Требуется расклеить 500 листовок формата А5 на доски объявлений около подъездов. "
            "Фотоотчет обязателен. Оплата по факту выполнения."
        )
    },
    {
        "title": "Разработка Telegram-бота для онлайн-записи клиентов",
        "budget_info": "Желаемый бюджет покупателя: до 4 500 ₽",
        "description": (
            "Нужен телеграм-бот для салона красоты. Функционал: выбор услуги, выбор мастера, "
            "выбор свободной даты и времени из календаря, уведомление администратора и напоминания клиенту за 2 часа."
        )
    }
]

def run_tests():
    print("=" * 60)
    print("🧪 ТЕСТИРОВАНИЕ АНАЛИЗАТОРА ЗАКАЗОВ И БАЗЫ ДАННЫХ")
    print("=" * 60)

    init_db()

    for idx, test in enumerate(TEST_CASES, 1):
        print(f"\n--- Тест #{idx}: {test['title']} ---")
        print(f"Бюджет: {test['budget_info']}")

        try:
            res = analyze_kwork_order(
                title=test["title"],
                description=test["description"],
                budget_info=test["budget_info"]
            )
            print(f"✔️ Выполнимость: {'ДА' if res.is_feasible else 'НЕТ'}")
            print(f"✔️ Обоснование: {res.reasoning}")

            if res.is_feasible:
                print(f"✔️ Стек: {res.tech_stack}")
                print(f"✔️ Срок: {res.duration_days} дн.")
                print(f"✔️ Название заказа ({len(res.proposal_title)} симв.): {res.proposal_title}")
                print(f"✔️ Распознанный бюджет: {res.desired_budget} ₽")
                print(f"✔️ Длина текста отклика: {len(res.proposal_text)} симв. (минимум 150)")
                print(f"✔️ Текст отклика:\n{res.proposal_text}")

                # Валидация требований
                assert 2 <= res.duration_days <= 3, "Срок должен быть 2 или 3 дня!"
                assert len(res.proposal_title) <= 70, "Название заказа не должно превышать 70 символов!"
                assert len(res.proposal_text) >= 150, "Текст отклика должен быть не менее 150 символов!"

            save_order(
                kwork_id=f"test_{idx}",
                title=test["title"],
                description=test["description"],
                budget_info=test["budget_info"],
                desired_price=res.desired_budget,
                is_feasible=res.is_feasible,
                reasoning=res.reasoning,
                tech_stack=res.tech_stack,
                proposal_title=res.proposal_title,
                proposal_text=res.proposal_text,
                duration_days=res.duration_days,
                status="TEST_PASSED"
            )

        except Exception as e:
            print(f"❌ Ошибка в тесте: {e}")

    print("\n" + "=" * 60)
    print("📊 Итоговая статистика БД:", get_stats())
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
