import json
import logging
import re
import time
from pathlib import Path
from typing import Optional, Tuple

from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, GEMINI_MODEL, MIN_ACCEPTABLE_PRICE, PRICING_RULES_PATH, GEMINI_PROXY, GEMINI_BASE_URL

logger = logging.getLogger(__name__)

def load_pricing_rules() -> dict:
    """Загружает тарифную сетку из pricing_rules.json."""
    if PRICING_RULES_PATH.exists():
        try:
            with open(PRICING_RULES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Ошибка чтения pricing_rules.json: {e}")
    return {
        "hourly_rate": 1000,
        "min_order_price": 1000,
        "categories": {
            "bugfix_or_modification": {"base_price": 1500, "estimated_hours": 2},
            "simple_parser": {"base_price": 2000, "estimated_hours": 3},
            "complex_parser": {"base_price": 4000, "estimated_hours": 5},
            "simple_bot": {"base_price": 2500, "estimated_hours": 3},
            "advanced_bot": {"base_price": 5000, "estimated_hours": 6},
            "api_integration": {"base_price": 3000, "estimated_hours": 4},
            "general_script": {"base_price": 2000, "estimated_hours": 3}
        }
    }

class ProposalAnalysis(BaseModel):
    is_feasible: bool = Field(
        description="True, если задача может быть качественно решена разработчиком/автоматизатором (написание скриптов, парсеров данных, Telegram-ботов, веб-сервисов, интеграций API, обработка таблиц/текста, фронтенд/бэкенд). False, если задача физическая или бюджет неприемлемо низкий для огромного объема."
    )
    reasoning: str = Field(
        description="Краткое обоснование решения (почему задача выполнима или не выполнима)."
    )
    category: str = Field(
        default="general_script",
        description="Категория задачи из тарифной сетки: bugfix_or_modification, simple_parser, complex_parser, simple_bot, advanced_bot, api_integration, general_script."
    )
    estimated_hours: int = Field(
        default=2,
        description="Оценка трудоемкости в часах (например, 1, 2, 4, 6 ч)."
    )
    is_placeholder_budget: bool = Field(
        default=False,
        description="True, если указанный бюджет (например, 500 руб.) является явной заглушкой, либо заказчик в тексте просит назвать свою цену, либо бюджет явно не соответствует масштабу задачи."
    )
    tech_stack: str = Field(
        default="",
        description="Краткий рекомендуемый стек технологий для решения (например, 'Python, Playwright, BeautifulSoup, SQLite' или 'Python, Aiogram 3, PostgreSQL')."
    )
    duration_days: int = Field(
        default=2,
        description="Срок выполнения в днях: строго 2 или 3 (в зависимости от объема задач)."
    )
    proposal_title: str = Field(
        default="",
        description="Краткое, емкое название заказа исходя из задачи (СТРОГО не более 70 символов)."
    )
    desired_budget: Optional[int] = Field(
        default=None,
        description="Желаемый бюджет покупателя в рублях целым числом."
    )
    max_allowed_budget: Optional[int] = Field(
        default=None,
        description="Максимально допустимый бюджет покупателя в рублях (если указан в блоке 'Допустимый: до X')."
    )
    calculated_fair_price: int = Field(
        default=3000,
        description="Справедливая расчетная цена проекта на основе тарифной сетки и трудозатрат."
    )
    kwork_form_price: int = Field(
        default=1000,
        description="Цена для ввода в инпут формы Kwork. СТРОГО не выше max_allowed_budget площадки, чтобы не вызвать ошибку валидации!"
    )
    real_suggested_price: int = Field(
        default=3000,
        description="Реальная предлагаемая стоимость задачи под ключ (может быть выше kwork_form_price, в этом случае разница обосновывается в тексте)."
    )
    final_offer_price: int = Field(
        default=3000,
        description="Итоговая стоимость для поля формы Kwork (равна kwork_form_price)."
    )
    proposal_text: str = Field(

        default="",
        description=(
            "Текст отклика на проект для формы Kwork. СТРОГО ОТ 160 ДО 1900 СИМВОЛОВ (Kwork требует минимум 150 знаков!). "
            "Структура: 1) Вежливое приветствие. 2) Подтверждение готовности взяться за выполнение с четким перечислением "
            "понятого функционала из ТЗ (если задача — доработка, обязательно подчеркнуть это!). "
            "3) Краткое описание стека и архитектуры. "
            "4) Если цена отличается от желаемого бюджета 500 руб. (заглушки) — деликатно обосновать цену объемом работ и качеством. "
            "5) Готовность обсудить детали и приступить к работе. "
            "Тон: уверенный, профессиональный разработчик (НЕ упоминать, что вы ИИ/нейросеть/бот!)."
        )
    )

def parse_budget_details(budget_str: str) -> Tuple[Optional[int], Optional[int]]:
    """Извлекает желаемый и допустимый бюджет покупателя."""
    if not budget_str:
        return None, None

    desired = None
    max_allowed = None

    # Желаемый бюджет
    match_des = re.search(r"Желаемый бюджет(?: покупателя)?:\s*(?:до\s*)?([\d\s]+)", budget_str, re.IGNORECASE)
    if match_des:
        d = re.sub(r"\s+", "", match_des.group(1))
        if d.isdigit():
            desired = int(d)

    # Допустимый бюджет
    match_max = re.search(r"Допустимый:\s*(?:до\s*)?([\d\s]+)", budget_str, re.IGNORECASE)
    if match_max:
        m = re.sub(r"\s+", "", match_max.group(1))
        if m.isdigit():
            max_allowed = int(m)

    # Диапазон (например, "500 - 1500")
    match_range = re.search(r"(\d[\d\s]*)\s*-\s*(\d[\d\s]*)", budget_str)
    if match_range:
        d = re.sub(r"\s+", "", match_range.group(1))
        m = re.sub(r"\s+", "", match_range.group(2))
        if d.isdigit() and not desired:
            desired = int(d)
        if m.isdigit() and not max_allowed:
            max_allowed = int(m)

    return desired, max_allowed

def analyze_kwork_order(
    title: str,
    description: str,
    budget_info: str = ""
) -> ProposalAnalysis:
    """
    Анализирует заказ Kwork с помощью Google Gemini с комбинированным контролем цен:
    - Проверяет техническую осуществимость.
    - Различает доработку существующего проекта и разработку с нуля.
    - Оценивает адекватность цены по тарифной сетке и допустимому диапазону Kwork.
    - Формирует аргументированный профессиональный отклик (от 160 до 1900 символов).
    """
    http_options = None
    client_args = {}
    if GEMINI_PROXY:
        client_args["proxy"] = GEMINI_PROXY

    if client_args or GEMINI_BASE_URL:
        http_options = types.HttpOptions(
            base_url=GEMINI_BASE_URL or None,
            client_args=client_args or None
        )

    client = genai.Client(api_key=GEMINI_API_KEY, http_options=http_options)
    pricing_rules = load_pricing_rules()

    extracted_desired, extracted_max = parse_budget_details(budget_info)

    system_prompt = (
        "Ты — опытный senior-разработчик и фрилансер на бирже Kwork. "
        "Твоя задача: детально проанализировать заказ с биржи, оценить техническую осуществимость "
        "и произвести комбинированную оценку адекватной стоимости работы.\n\n"
        f"Тарифная сетка разработчика:\n{json.dumps(pricing_rules, ensure_ascii=False, indent=2)}\n"
        f"Минимальная планка стоимости заказа: {MIN_ACCEPTABLE_PRICE} руб.\n\n"
        "Правила ценообразования и ВИЛКА ЦЕН ПЛОЩАДКИ:\n"
        "1. ОЦЕНКА БЮДЖЕТА ЗАКАЗЧИКА (ПРИОРИТЕТНОЕ ПРАВИЛО!):\n"
        "   - На Kwork заказчик указывает 'Желаемый бюджет' (desired_budget) и 'Допустимый: до X' (max_allowed_budget).\n"
        "   - ЕСЛИ ЗАКАЗЧИК УКАЗАЛ АДЕКВАТНЫЙ / ХОРОШИЙ БЮДЖЕТ (например: 10 000, 20 000, 50 000, 190 000 руб.):\n"
        "     * СТРОГО ЗАПРЕЩЕНО ДЕМПИНГОВАТЬ И ЗАНИЖАТЬ ЦЕНУ! Не сбивай цену до 3000-5000 руб., если клиент готов платить 50 000 - 190 000 руб.!\n"
        "     * В этом случае выставляй реальную цену (real_suggested_price) и цену для формы (kwork_form_price) РОВНО ПО БЮДЖЕТУ ЗАКАЗЧИКА "
        "(например, если желаемый бюджет 190 000 руб., предлагай 180 000 - 190 000 руб., аргументируя это масштабом, качеством, тестами и сопровождением).\n"
        "   - ЕСЛИ ЗАКАЗЧИК УКАЗАЛ ЗАГЛУШКУ ИЛИ МИКРО-БЮДЖЕТ (например: 500 руб., 1000 руб., а задача большая):\n"
        "     * Определи категорию из сетки и реальные часы (estimated_hours).\n"
        "     * real_suggested_price = расчетная цена (минимум 3000 руб. или base_price/hours * hourly_rate).\n"
        "     * kwork_form_price = max_allowed_budget (потолок площадки, чтобы не выдать ошибку валидации формы Kwork).\n"
        "     * В тексте отклика (proposal_text) ОБЯЗАТЕЛЬНО включи отдельный абзац с обоснованием реальной цены ({real_suggested_price} руб.).\n"
        "   - Если задача микроскопическая (на 15 минут) и не тянет на минимальную планку 3000 руб., а бюджет заказчика < 3000 руб.: "
        "установи is_feasible = False.\n"
        "2. КРИТИЧЕСКИ ВАЖНО: Различай задачи 'написать с нуля' и 'доработать/исправить существующий проект'!\n"
        "   Если в ТЗ сказано, что бот/скрипт уже есть — предлагай именно доработку и точечное решение проблемы.\n"
        "3. ПРАВИЛО ПО ИСКЛЮЧЕННЫМ ТЕМАМ (ВИДЕОПРОДАКШН / КОНТЕНТ-ЗАВОДЫ / NO-CODE):\n"
        "   - Если задача связана с созданием 'контент-заводов', генерацией видео/рилсов/shorts, видеомонтажом, съемкой, озвучкой видео — "
        "СТРОГО ПРОПУСКАЕМ ЗАКАЗ (is_feasible = False, reasoning = 'Тематика видеопродакшна и контент-заводов не входит в наш профиль').\n"
        "   - Если же задача требует ручной работы в визуальных конструкторах ботов/связок (Salebot, Bothelp, Senler, ManyChat, LeadConverter, Make, Albato) "
        "и нужны готовые кейсы — ПРОПУСКАЕМ заказ! (is_feasible = False, reasoning = 'Требуется ручная настройка в no-code конструкторах ботов/связок, готовых кейсов нет').\n"
        "   - Если заказчик в ТЗ просит кейсы по программным задачам (код на Python, парсеры, Telegram-боты, API): пиши в отклике: "
        "'Кейсов именно в этой нише нет, но стек и проект понятен, сделать не проблема.'\n"
        "6. Структура отклика (proposal_text):\n"
        "   - Приветствие.\n"
        "   - Подтверждение понимания задачи (доработка или разработка, ключевые методы и технологии).\n"
        "   - Если заказчик спрашивал кейсы — фраза: 'Кейсов именно в этой теме нет, но стек и проект понятен, сделать не проблема.'\n"
        "   - Краткий стек технологий и план решения.\n"
        "   - Обоснование цены (с учетом вилки платформы и реального предложения).\n"
        "   - Длина: СТРОГО от 160 до 1900 символов (требование Kwork: не менее 150 символов!).\n"
        "7. Название заказа (proposal_title): кратко и емко (СТРОГО до 70 символов).\n"
        "8. Срок выполнения (duration_days): строго 2 или 3 дня."
    )

    user_prompt = f"""
Информация о заказе с биржи Kwork:
Заголовок: {title}
Информация о бюджете: {budget_info}
Извлеченный желаемый бюджет: {extracted_desired} руб.
Извлеченный допустимый максимум: {extracted_max} руб.

Полный текст задания:
{description}
"""

    gen_config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=ProposalAnalysis,
        system_instruction=system_prompt,
        temperature=0.3,
    )

    models_to_try = [GEMINI_MODEL]
    fallback_models = [
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-flash-latest"
    ]
    for m in fallback_models:
        if m not in models_to_try:
            models_to_try.append(m)

    last_error = None
    for model_name in models_to_try:
        max_retries = 2
        for attempt in range(max_retries):
            try:
                logger.info(f"Запрос к Gemini (модель: {model_name}, попытка {attempt + 1})...")
                response = client.models.generate_content(
                    model=model_name,
                    contents=user_prompt,
                    config=gen_config
                )
                data = json.loads(response.text)
                result = ProposalAnalysis(**data)

                # Синхронизируем извлеченные регуляркой бюджеты
                if extracted_desired and not result.desired_budget:
                    result.desired_budget = extracted_desired
                if extracted_max and not result.max_allowed_budget:
                    result.max_allowed_budget = extracted_max

                # Если заказчик указал реальный хороший бюджет (например, от 5 000 руб.),
                # мы ни в коем случае не демпингуем и ставим цену под его бюджет!
                client_target_budget = extracted_desired or result.desired_budget or extracted_max or result.max_allowed_budget
                if client_target_budget and client_target_budget >= 5000:
                    if result.kwork_form_price < client_target_budget:
                        logger.info(
                            f"💡 Заказчик готов платить {client_target_budget} ₽ (ИИ предложил {result.kwork_form_price} ₽). "
                            f"Корректируем предложение под бюджет заказчика: {client_target_budget} ₽."
                        )
                        result.kwork_form_price = client_target_budget
                        result.real_suggested_price = max(result.real_suggested_price, client_target_budget)

                # Проверяем вилку формы Kwork: kwork_form_price не может превышать max_allowed_budget
                if result.max_allowed_budget and result.kwork_form_price > result.max_allowed_budget:
                    result.kwork_form_price = result.max_allowed_budget

                if not result.kwork_form_price or result.kwork_form_price < 500:
                    result.kwork_form_price = result.max_allowed_budget or result.desired_budget or 1000

                # Синхронизируем final_offer_price для поля ввода формы
                result.final_offer_price = result.kwork_form_price

                # Ограничение длины названия (до 70 символов)
                if len(result.proposal_title) > 70:
                    result.proposal_title = result.proposal_title[:67] + "..."

                # Срок строго 2 или 3 дня
                if result.duration_days not in (2, 3):
                    result.duration_days = 2 if result.duration_days <= 2 else 3

                # Минимальная длина отклика Kwork (не менее 150 символов)
                if result.is_feasible and len(result.proposal_text.strip()) < 150:
                    result.proposal_text += (
                        f"\n\nГотов обсудить детали и приступить к работе. "
                        f"Стек: {result.tech_stack}. Гарантирую качественный результат и поддержку!"
                    )

                return result

            except Exception as e:
                last_error = e
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    # Извлекаем рекомендуемое Google время ожидания, если указано
                    wait_sec = 8.0
                    retry_match = re.search(r"retry in (\d+(?:\.\d+)?)s", err_str, re.IGNORECASE)
                    if retry_match:
                        wait_sec = float(retry_match.group(1)) + 2.0
                    logger.warning(
                        f"Лимит запросов Google API (429 RESOURCE_EXHAUSTED). "
                        f"Ожидание {wait_sec:.1f} сек. перед повторной попыткой..."
                    )
                    time.sleep(wait_sec)
                else:
                    logger.warning(f"Модель {model_name} вернула ошибку: {e}.")
                    break  # При других ошибках (например 404/400) сразу переходим к следующей модели

    logger.error(f"Все доступные модели Gemini вернули ошибку. Последняя: {last_error}")
    raise last_error

