import json
import logging
import math
import re
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Type, TypeVar

import httpx
from openai import OpenAI, APIStatusError
from pydantic import BaseModel, Field
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_FALLBACK_MODELS, LLM_SCREEN_MODEL, LLM_PROXY, MIN_ACCEPTABLE_PRICE, PRICING_RULES_PATH

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
        description="Цена предложения в рублях — ровно та, что указана в правилах цены (её введут в поле «Стоимость» формы Kwork)."
    )
    real_suggested_price: int = Field(
        default=3000,
        description="Равна kwork_form_price."
    )
    final_offer_price: int = Field(
        default=3000,
        description="Итоговая стоимость для поля формы Kwork (равна kwork_form_price)."
    )
    client_requested_cases: bool = Field(
        default=False,
        description=(
            "True ТОЛЬКО если заказчик в тексте заказа сам просит показать кейсы, портфолио, примеры выполненных работ "
            "или спрашивает, делал ли исполнитель похожие проекты / есть ли опыт в подобных задачах. "
            "False, если заказчик об этом не просит: просто описывает задачу, перечисляет требования к навыкам "
            "('нужен опыт с Python', 'опыт — большой плюс') или слово 'кейс' употреблено в другом смысле (кейсы CS2, бизнес-кейс)."
        )
    )
    proposal_text: str = Field(

        default="",
        description=(
            "Текст отклика на проект для формы Kwork. СТРОГО ОТ 160 ДО 1900 СИМВОЛОВ (Kwork требует минимум 150 знаков!). "
            "Структура: 1) Вежливое приветствие. 2) Подтверждение готовности взяться за выполнение с четким перечислением "
            "понятого функционала из ТЗ (если задача — доработка, обязательно подчеркнуть это!). "
            "3) Краткое описание стека и архитектуры. "
            "4) Коротко обосновать цену предложения (одна сумма — kwork_form_price; без упоминаний формы Kwork, «реальной стоимости» и других сумм). "
            "5) Готовность обсудить детали и приступить к работе. "
            "Фразу про отсутствие кейсов включать ТОЛЬКО если client_requested_cases = true, иначе кейсы и портфолио не упоминать. "
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

    # Цена без «Допустимого»: "Цена до: 3 000 ₽" — предложить больше этой суммы Kwork не даст
    match_fixed = re.search(r"Цена до:?\s*([\d\s]+)", budget_str, re.IGNORECASE)
    if match_fixed and not desired:
        f = re.sub(r"\s+", "", match_fixed.group(1))
        if f.isdigit():
            desired = int(f)
            max_allowed = max_allowed or desired

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

CASES_PHRASE = "Кейсов именно в этой нише нет, но стек и проект понятен, сделать не проблема."

# Без этих слов в тексте заказа просьбы о кейсах / примерах работ точно нет
CASES_MENTION_RE = re.compile(
    r"кейс|портфолио|(?<!на)пример|опыт|похож|аналогичн|подобн|ранее\s+(?:выполн|сдел|реализ)|уже\s+делал",
    re.IGNORECASE
)

# Предложение отклика о том, что кейсов / портфолио / примеров работ нет
_CASES_WORDS = r"(?:кейс|портфолио|(?<!на)пример\w*\s+(?:\w+\s+)?работ)"
_ABSENCE_WORDS = r"(?:\bнет\b|отсутству|не\s+было|не\s+имею)"
NO_CASES_SENTENCE_RE = re.compile(
    rf"{_CASES_WORDS}[^.!?\n]*{_ABSENCE_WORDS}|{_ABSENCE_WORDS}[^.!?\n]*{_CASES_WORDS}",
    re.IGNORECASE
)

def apply_cases_phrase_rule(text: str, client_requested_cases: bool) -> str:
    """
    Фраза про отсутствие кейсов нужна в отклике, только если заказчик сам просит кейсы / примеры работ:
    при просьбе — добавляется, если модель её пропустила; без просьбы — такие предложения удаляются.
    """
    if not text:
        return text

    if client_requested_cases:
        if re.search(_CASES_WORDS, text, re.IGNORECASE):
            return text
        # Вставляем после приветствия и понимания задачи
        if "\n" in text:
            sep = "\n\n" if "\n\n" in text else "\n"
            parts = text.split(sep)
            parts.insert(2 if len(parts) > 2 and len(parts[0]) <= 40 else 1, CASES_PHRASE)
            return sep.join(parts)
        sentences = re.split(r"(?<=[.!?])\s+", text)
        sentences.insert(min(2, len(sentences)), CASES_PHRASE)
        return " ".join(sentences)

    if not NO_CASES_SENTENCE_RE.search(text):
        return text
    lines = []
    for line in text.split("\n"):
        kept = [s for s in re.split(r"(?<=[.!?])\s+", line) if not NO_CASES_SENTENCE_RE.search(s)]
        if kept or not line.strip():
            lines.append(" ".join(kept))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

def _schema_for_prompt(node, in_properties: bool = False):
    """JSON-схема ответа без значений по умолчанию и служебных заголовков, чтобы не подсказывать модели цифры."""
    if isinstance(node, dict):
        return {
            key: _schema_for_prompt(value, key == "properties")
            for key, value in node.items()
            if in_properties or key not in ("default", "title")
        }
    if isinstance(node, list):
        return [_schema_for_prompt(value) for value in node]
    return node

# Схема передаётся в промпте: DeepSeek поддерживает режим JSON, но не проверку ответа по схеме
ANALYSIS_JSON_SCHEMA = json.dumps(_schema_for_prompt(ProposalAnalysis.model_json_schema()), ensure_ascii=False)

_client: Optional[OpenAI] = None

def get_llm_client() -> OpenAI:
    """Клиент OpenAI-совместимого API нейросети (по умолчанию DeepSeek)."""
    global _client
    if _client is None:
        # Прокси только явный (LLM_PROXY): старые GEMINI_PROXY / HTTPS_PROXY из окружения не подхватываются
        http_client = httpx.Client(proxy=LLM_PROXY or None, trust_env=False, timeout=httpx.Timeout(300.0, connect=20.0))
        _client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL, http_client=http_client)
    return _client

ModelT = TypeVar("ModelT", bound=BaseModel)

def request_json_from_llm(
    system_prompt: str,
    user_prompt: str,
    response_model: Type[ModelT],
    models: Optional[Sequence[str]] = None
) -> Tuple[ModelT, str]:
    """
    Запрос к нейросети в режиме JSON с проверкой ответа по pydantic-модели.
    Перебирает модели (по умолчанию основную и запасные). Возвращает (ответ, имя модели).
    """
    client = get_llm_client()
    last_error = None
    for model_name in dict.fromkeys(models or [LLM_MODEL, *LLM_FALLBACK_MODELS]):
        max_retries = 2
        for attempt in range(max_retries):
            try:
                logger.info(f"Запрос к нейросети (модель: {model_name}, попытка {attempt + 1})...")
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.3,
                    max_tokens=16000
                )
                choice = response.choices[0]
                content = (choice.message.content or "").strip()
                if not content:
                    raise ValueError(f"пустой ответ модели (finish_reason={choice.finish_reason})")
                return response_model(**json.loads(content)), model_name

            except APIStatusError as e:
                last_error = e
                if e.status_code in (401, 402, 403):
                    # Неверный ключ, закончился баланс или нет доступа — другие модели этого API не помогут
                    logger.error(f"Нейросеть отклонила запрос ({e.status_code}): {e}")
                    raise
                logger.warning(f"Модель {model_name} вернула ошибку: {e}.")
                break  # Временные ошибки (429/5xx) SDK уже повторил сам — переходим к следующей модели
            except ValueError as e:
                # Пустой ответ, битый JSON или ответ не по схеме — повторяем запрос
                last_error = e
                logger.warning(f"Модель {model_name} вернула некорректный ответ: {e}")
            except Exception as e:
                last_error = e
                logger.warning(f"Модель {model_name} недоступна: {e}.")
                break  # Сеть или таймаут — переходим к следующей модели

    logger.error(f"Все модели нейросети вернули ошибку. Последняя: {last_error}")
    raise last_error

def analyze_kwork_order(
    title: str,
    description: str,
    budget_info: str = ""
) -> ProposalAnalysis:
    """
    Анализирует заказ Kwork с помощью нейросети (OpenAI-совместимый API, по умолчанию DeepSeek)
    с комбинированным контролем цен:
    - Проверяет техническую осуществимость.
    - Различает доработку существующего проекта и разработку с нуля.
    - Цена: желаемый бюджет заказчика, но не ниже минимальной планки (при неизвестном бюджете — оценка по тарифной сетке).
    - Формирует аргументированный профессиональный отклик (от 160 до 1900 символов).
    """
    pricing_rules = load_pricing_rules()

    extracted_desired, extracted_max = parse_budget_details(budget_info)
    offer_price = default_offer_price(budget_info)
    if offer_price:
        price_rule = (
            f"   - Цена предложения уже рассчитана: {offer_price} руб. — желаемый бюджет заказчика, "
            f"но не ниже нашей минимальной планки {MIN_ACCEPTABLE_PRICE} руб. и в пределах допустимого бюджета.\n"
            f"   - Поставь kwork_form_price = real_suggested_price = final_offer_price = {offer_price}. Не повышай и не понижай её.\n"
        )
    else:
        price_rule = (
            "   - Бюджет заказчика неизвестен: оцени цену по тарифной сетке и трудозатратам (category, estimated_hours), "
            f"не ниже {MIN_ACCEPTABLE_PRICE} руб. kwork_form_price = real_suggested_price = final_offer_price = эта цена.\n"
        )

    system_prompt = (
        "Ты — опытный senior-разработчик и фрилансер на бирже Kwork. "
        "Твоя задача: детально проанализировать заказ с биржи, оценить техническую осуществимость "
        "и произвести комбинированную оценку адекватной стоимости работы.\n\n"
        f"Тарифная сетка разработчика:\n{json.dumps(pricing_rules, ensure_ascii=False, indent=2)}\n"
        f"Минимальная планка стоимости заказа: {MIN_ACCEPTABLE_PRICE} руб.\n\n"
        "Правила цены:\n"
        "1. ЦЕНА ПРЕДЛОЖЕНИЯ:\n"
        f"{price_rule}"
        "   - В тексте отклика упоминай только эту цену и коротко обоснуй её объёмом работ. НЕ упоминай форму Kwork, "
        "«реальную стоимость», «максимальный» или «допустимый» бюджет, доплаты и другие суммы за нашу работу.\n"
        "   - Если задача микроскопическая (на 15 минут) и не тянет на минимальную планку 3000 руб., а бюджет заказчика < 3000 руб.: "
        "установи is_feasible = False.\n"
        "2. КРИТИЧЕСКИ ВАЖНО: Различай задачи 'написать с нуля' и 'доработать/исправить существующий проект'!\n"
        "   Если в ТЗ сказано, что бот/скрипт уже есть — предлагай именно доработку и точечное решение проблемы.\n"
        "3. ПРАВИЛО ПО ИСКЛЮЧЕННЫМ ТЕМАМ (ВИДЕОПРОДАКШН / КОНТЕНТ-ЗАВОДЫ / NO-CODE):\n"
        "   - Если задача связана с созданием 'контент-заводов', генерацией видео/рилсов/shorts, видеомонтажом, съемкой, озвучкой видео — "
        "СТРОГО ПРОПУСКАЕМ ЗАКАЗ (is_feasible = False, reasoning = 'Тематика видеопродакшна и контент-заводов не входит в наш профиль').\n"
        "   - Если же задача требует ручной работы в визуальных конструкторах ботов/связок (Salebot, Bothelp, Senler, ManyChat, LeadConverter, Make, Albato) "
        "и нужны готовые кейсы — ПРОПУСКАЕМ заказ! (is_feasible = False, reasoning = 'Требуется ручная настройка в no-code конструкторах ботов/связок, готовых кейсов нет').\n"
        "4. ФРАЗА ПРО КЕЙСЫ (поле client_requested_cases):\n"
        "   - client_requested_cases = true ТОЛЬКО если заказчик сам просит кейсы, портфолио, примеры выполненных работ "
        "или спрашивает о похожих выполненных проектах / опыте в подобных задачах.\n"
        f"   - Только в этом случае добавь в отклик фразу: '{CASES_PHRASE}'\n"
        "   - Если заказчик об этом не просит (просто описывает задачу, перечисляет требования к навыкам) — "
        "client_requested_cases = false, и в отклике НЕ упоминай кейсы, портфолио, примеры работ и их отсутствие.\n"
        "6. Структура отклика (proposal_text):\n"
        "   - Приветствие.\n"
        "   - Подтверждение понимания задачи (доработка или разработка, ключевые методы и технологии).\n"
        "   - Фраза про кейсы — строго по правилу 4 (только если client_requested_cases = true).\n"
        "   - Краткий стек технологий и план решения.\n"
        "   - Короткое обоснование цены предложения — одна сумма, без упоминаний формы Kwork и других цен.\n"
        "   - Длина: СТРОГО от 160 до 1900 символов (требование Kwork: не менее 150 символов!).\n"
        "7. Название заказа (proposal_title): кратко и емко (СТРОГО до 70 символов).\n"
        "8. Срок выполнения (duration_days): строго 2 или 3 дня.\n\n"
        "ФОРМАТ ОТВЕТА: верни ровно один JSON-объект со всеми полями из JSON-схемы ниже, без markdown и пояснений. "
        "Описания полей в схеме — такие же обязательные правила, как и правила выше.\n"
        f"JSON-схема: {ANALYSIS_JSON_SCHEMA}"
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

    result, model_name = request_json_from_llm(system_prompt, user_prompt, ProposalAnalysis)

    # Синхронизируем извлеченные регуляркой бюджеты
    if extracted_desired and not result.desired_budget:
        result.desired_budget = extracted_desired
    if extracted_max and not result.max_allowed_budget:
        result.max_allowed_budget = extracted_max

    # Цена предложения: при известном бюджете — рассчитанная по правилу (желаемый бюджет, но не ниже минимальной планки
    # и в пределах допустимого), иначе — оценка модели по тарифной сетке, но не ниже минимальной планки
    model_prices = {result.kwork_form_price, result.real_suggested_price}
    if offer_price:
        if result.kwork_form_price != offer_price:
            logger.info(f"💡 Модель предложила {result.kwork_form_price} ₽ — ставим цену по правилу: {offer_price} ₽.")
        result.kwork_form_price = offer_price
    else:
        result.kwork_form_price = max(result.kwork_form_price or 0, MIN_ACCEPTABLE_PRICE)
    result.real_suggested_price = result.kwork_form_price
    result.final_offer_price = result.kwork_form_price

    # Ограничение длины названия (до 70 символов)
    if len(result.proposal_title) > 70:
        result.proposal_title = result.proposal_title[:67] + "..."

    # Срок строго 2 или 3 дня
    if result.duration_days not in (2, 3):
        result.duration_days = 2 if result.duration_days <= 2 else 3

    # Фраза про отсутствие кейсов — только если заказчик сам просит кейсы / примеры работ
    result.client_requested_cases = result.client_requested_cases and bool(
        CASES_MENTION_RE.search(f"{title}\n{description}")
    )
    proposal_before = result.proposal_text
    result.proposal_text = apply_cases_phrase_rule(proposal_before, result.client_requested_cases)
    if result.proposal_text != proposal_before:
        logger.info(
            "Фраза про кейсы добавлена: заказчик просит кейсы, а модель её пропустила"
            if result.client_requested_cases else
            "Фраза про кейсы удалена: заказчик не просит кейсы"
        )

    # Модель вписала в текст свою цену вместо цены предложения — согласуем текст с ценой
    other_prices = {p for p in model_prices if p and p != result.kwork_form_price} & set(prices_in_text(result.proposal_text))
    if result.is_feasible and other_prices:
        logger.info(f"В тексте отклика другая цена ({', '.join(map(str, sorted(other_prices)))} ₽) — согласую текст с ценой {result.kwork_form_price} ₽")
        try:
            result.proposal_text = rewrite_proposal_price(result.proposal_text, result.kwork_form_price)
        except Exception as e:
            logger.warning(f"Не удалось согласовать текст отклика с ценой: {e}")

    # Минимальная длина отклика Kwork (не менее 150 символов)
    if result.is_feasible and len(result.proposal_text.strip()) < 150:
        result.proposal_text += (
            f"\n\nГотов обсудить детали и приступить к работе. "
            f"Стек: {result.tech_stack}. Гарантирую качественный результат и поддержку!"
        )

    logger.info(f"Анализ выполнен моделью {model_name}")
    return result

class OrderScreening(BaseModel):
    is_feasible: bool = Field(description="false — только если заказ явно не подходит под профиль исполнителя; при сомнении true")
    reasoning: str = Field(default="", description="Одно предложение: почему")

def screen_kwork_order(title: str, description: str, budget_info: str = "") -> OrderScreening:
    """
    Предварительный отбор заказа дешёвой моделью (LLM_SCREEN_MODEL): отсеивает явно чужие заказы,
    чтобы полный анализ и отклик умной моделью делались только для подходящих.
    """
    system_prompt = (
        "Ты — предварительный фильтр заказов с биржи Kwork для разработчика-автоматизатора.\n"
        "Профиль исполнителя: написание скриптов, парсеров данных, Telegram-ботов, веб-сервисов, интеграций API, "
        "обработка таблиц/текста, фронтенд/бэкенд.\n"
        "is_feasible = false ставь ТОЛЬКО если заказ явно не про это:\n"
        "- физическая или офлайн-работа, звонки, продажи и поиск клиентов, участие в опросах и тестировании продукта;\n"
        "- дизайн, написание текстов и переводы, SMM, ручной набор или расшифровка текста;\n"
        "- видеопродакшн, контент-заводы, генерация и монтаж видео, съемка, озвучка;\n"
        "- ручная настройка в no-code конструкторах ботов/связок (Salebot, Bothelp, Senler, ManyChat, LeadConverter, Make, Albato) с готовыми кейсами.\n"
        "Во всех остальных случаях, в том числе если сомневаешься, — is_feasible = true: подробный анализ сделает старшая модель.\n"
        'Ответ — JSON-объект {"is_feasible": true или false, "reasoning": "одно предложение"} без markdown.'
    )
    user_prompt = f"Заголовок: {title}\nБюджет: {budget_info}\n\nТекст задания:\n{description}"
    result, model_name = request_json_from_llm(system_prompt, user_prompt, OrderScreening, models=[LLM_SCREEN_MODEL])
    logger.info(f"Предварительный отбор ({model_name}): {'подходит' if result.is_feasible else 'не подходит'} — {result.reasoning}")
    return result

def kwork_price_limits(budget_info: str) -> Tuple[int, Optional[int]]:
    """
    Допустимая цена предложения на Kwork: от 20% желаемого бюджета (но не меньше 500 ₽)
    до «Допустимого» бюджета, а если его нет — до желаемого. Верхняя граница None, если бюджет неизвестен.
    """
    desired, max_allowed = parse_budget_details(budget_info)
    min_price = max(500, math.ceil(desired * 0.2)) if desired else 500
    return min_price, max_allowed or desired

def default_offer_price(budget_info: str) -> Optional[int]:
    """
    Цена предложения по умолчанию: желаемый бюджет заказчика, но не ниже минимальной планки (MIN_ACCEPTABLE_PRICE)
    и в пределах допустимой цены Kwork. None — бюджет заказчика неизвестен.
    """
    desired, _ = parse_budget_details(budget_info)
    if not desired:
        return None
    min_price, max_price = kwork_price_limits(budget_info)
    price = max(desired, MIN_ACCEPTABLE_PRICE, min_price)
    return min(price, max_price) if max_price else price

def prices_in_text(text: str) -> List[int]:
    """Суммы в рублях, упомянутые в тексте: '3 000 ₽', '6000 руб.' -> [3000, 6000]."""
    return [int(re.sub(r"\D", "", m)) for m in re.findall(r"\d[\d\s]{2,}(?=\s*(?:₽|руб|р\.))", text or "")]

def text_mentions_price(text: str) -> bool:
    """Есть ли в тексте суммы в рублях — тогда при смене цены текст нужно согласовать."""
    return bool(re.search(r"₽|руб|\d\s*(?:тыс|р\.)", text or "", re.IGNORECASE))

class ProposalRewrite(BaseModel):
    proposal_text: str = Field(min_length=150, max_length=2000, description="Текст отклика, согласованный с новой ценой")

def rewrite_proposal_price(proposal_text: str, new_price: int) -> str:
    """Согласует суммы в готовом отклике с ценой, заданной вручную в Telegram. Остальной текст не меняется."""
    price = f"{new_price:,}".replace(",", " ")
    system_prompt = (
        "Ты редактируешь готовый отклик фрилансера на бирже Kwork. Цену предложения изменили вручную: "
        f"теперь это {price} ₽ — ровно эта сумма будет в поле «Стоимость» формы Kwork.\n"
        "Правила:\n"
        f"1. В тексте должна остаться одна цена нашей работы — {price} ₽. Предложения, где говорится о стоимости, перепиши целиком, "
        "чтобы они звучали естественно: убери противопоставление «реальная стоимость — сумма в форме», упоминания формы Kwork, "
        "«максимального бюджета», доплат и любых других сумм за нашу работу.\n"
        "2. Короткое обоснование цены оставь (что входит в работу). Бюджет заказчика упоминать можно, если это уместно.\n"
        "3. Всё, что не касается цены, оставь дословно: первое слово, приветствие, описание задачи, стек, фразы про кейсы, разбивку на абзацы.\n"
        "4. Длина текста — от 160 до 1900 символов.\n"
        "Ответ — JSON-объект с одним полем proposal_text, без markdown и пояснений."
    )
    result, model_name = request_json_from_llm(system_prompt, f"Текущий текст отклика:\n{proposal_text}", ProposalRewrite)
    logger.info(f"Текст отклика согласован с ценой {new_price} ₽ (модель {model_name})")
    return result.proposal_text.strip()
