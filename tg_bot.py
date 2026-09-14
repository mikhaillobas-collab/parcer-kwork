import asyncio
import html
import logging
import re
from pathlib import Path
from typing import Optional, Callable, Any

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, ForceReply
from aiogram.client.default import DefaultBotProperties

import config
from database import update_order_status, get_order_by_id, get_stats, update_order_proposal
from gemini_analyzer import kwork_price_limits, rewrite_proposal_price, text_mentions_price

logger = logging.getLogger("kwork_bot.tg")

# Глобальный экземпляр бота и диспетчера
bot: Optional[Bot] = None
dp: Optional[Dispatcher] = None

# Функция обратного вызова для запуска отправки в Playwright
# Сигнатура: async def on_approve(order_id: str) -> Tuple[bool, Optional[Path], str]
_approve_handler: Optional[Callable[[str], Any]] = None

# Статусы, в которых отклик ещё можно отправить или изменить его цену (FAILED_SUBMIT — после неудачной отправки)
EDITABLE_STATUSES = ("WAITING_APPROVAL", "FAILED_SUBMIT")

class PriceEdit(StatesGroup):
    waiting_price = State()

def set_approve_handler(handler: Callable[[str], Any]) -> None:
    global _approve_handler
    _approve_handler = handler

def get_bot() -> Optional[Bot]:
    global bot
    if bot is None and config.TELEGRAM_BOT_TOKEN:
        bot = Bot(
            token=config.TELEGRAM_BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML)
        )
    return bot

def get_dispatcher() -> Dispatcher:
    global dp
    if dp is None:
        dp = Dispatcher()
        register_handlers(dp)
    return dp

def parse_price_input(text: str) -> Optional[int]:
    """Цена из сообщения пользователя: '7000', '7 000 ₽', '7к', '7,5 тыс' -> рубли."""
    match = re.fullmatch(r"\s*(\d[\d\s.,]*?)\s*(к|k|тыс\.?)?\s*(?:₽|руб\.?|р\.?)?\s*", (text or "").lower())
    if not match:
        return None
    number = re.sub(r"\s", "", match.group(1))
    if match.group(2):  # тысячи, допускаем дробную часть: 7,5к
        try:
            return round(float(number.replace(",", ".")) * 1000) or None
        except ValueError:
            return None
    digits = re.sub(r"\D", "", number)
    return int(digits) if digits and int(digits) > 0 else None

def price_limits_text(budget_info: str) -> str:
    min_price, max_price = kwork_price_limits(budget_info or "")
    return f"от {min_price} до {max_price} ₽" if max_price else f"от {min_price} ₽"

def build_order_card(order_id: str, title: str, budget_info: str, price_line: str, proposal_text: str, duration_days: int) -> str:
    """Текст карточки заказа для согласования."""
    return (
        f"🔔 <b>НАЙДЕН НОВЫЙ ПОДХОДЯЩИЙ ЗАКАЗ!</b>\n\n"
        f"📌 <b>#{order_id}:</b> {html.escape(title or '')}\n"
        f"💰 <b>Бюджет клиента:</b> {html.escape(budget_info or '')}\n"
        f"🏷️ <b>Наше предложение:</b> {price_line}\n"
        f"⏱️ <b>Срок:</b> {duration_days} дн.\n\n"
        f"📝 <b>Сгенерированный отклик:</b>\n"
        f"<i>{html.escape(proposal_text or '')}</i>\n\n"
        f"🔗 <a href='https://kwork.ru/projects/{order_id}/view'>Открыть заказ на Kwork</a>"
    )

def build_order_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Отправить отклик", callback_data=f"approve:{order_id}"),
            InlineKeyboardButton(text="❌ Пропустить", callback_data=f"reject:{order_id}")
        ],
        [InlineKeyboardButton(text="✏️ Изменить цену", callback_data=f"price:{order_id}")]
    ])

async def restore_card_keyboard(message: types.Message, order_id: str) -> None:
    """Возвращает кнопки на карточку после неудачной отправки: можно изменить цену и отправить снова."""
    try:
        await message.edit_reply_markup(reply_markup=build_order_keyboard(order_id))
    except Exception as e:
        logger.warning(f"Не удалось вернуть кнопки на карточку #{order_id}: {e}")

def register_handlers(dispatcher: Dispatcher) -> None:
    @dispatcher.message(CommandStart())
    async def cmd_start(message: types.Message):
        if str(message.chat.id) != str(config.TELEGRAM_CHAT_ID):
            await message.answer("⛔ У вас нет доступа к управлению этим ботом.")
            return

        stats = get_stats()
        await message.answer(
            f"👋 <b>Kwork Automation Bot на связи!</b>\n\n"
            f"Режим DRY_RUN: <b>{'ВКЛЮЧЕН (тест)' if config.DRY_RUN else 'ВЫКЛЮЧЕН (боевой)'}</b>\n"
            f"Статистика базы: {stats}\n\n"
            f"Я буду присылать вам подходящие заказы с биржи. Вы сможете одним нажатием подтверждать или отклонять отправку откликов!"
        )

    @dispatcher.message(Command("stats"))
    async def cmd_stats(message: types.Message):
        if str(message.chat.id) != str(config.TELEGRAM_CHAT_ID):
            return
        stats = get_stats()
        await message.answer(f"📊 <b>Текущая статистика:</b>\n{stats}")

    @dispatcher.message(Command("cancel"))
    async def cmd_cancel(message: types.Message, state: FSMContext):
        if str(message.chat.id) != str(config.TELEGRAM_CHAT_ID):
            return
        if await state.get_state() is None:
            await message.answer("Сейчас нечего отменять.")
            return
        await state.clear()
        await message.answer("Изменение цены отменено.")

    @dispatcher.callback_query(F.data.startswith("approve:"))
    async def process_approve(callback: types.CallbackQuery):
        if str(callback.from_user.id) != str(config.TELEGRAM_CHAT_ID):
            await callback.answer("⛔ Доступ запрещен!", show_alert=True)
            return

        order_id = callback.data.split(":", 1)[1]
        order = get_order_by_id(order_id)
        if not order or order["status"] not in EDITABLE_STATUSES:
            await callback.answer("Отклик по этому заказу уже отправлен или заказ отклонён", show_alert=True)
            return
        await callback.answer("⏳ Запускаю отправку отклика на Kwork...")

        # Обновляем текст сообщения, показывая статус обработки
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        status_msg = await callback.message.reply(f"🚀 <i>Заполняю форму и отправляю отклик на заказ #{order_id}...</i>")

        if _approve_handler:
            try:
                success, screenshot_path, err_msg = await _approve_handler(order_id)
                if success:
                    update_order_status(order_id, "SUBMITTED" if not config.DRY_RUN else "DRY_RUN_SUBMITTED")
                    caption = (
                        f"✅ <b>Отклик на заказ #{order_id} успешно "
                        f"{'смоделирован (DRY_RUN)' if config.DRY_RUN else 'ОТПРАВЛЕН на биржу'}!</b>"
                    )
                    if screenshot_path and Path(screenshot_path).exists():
                        photo = FSInputFile(str(screenshot_path))
                        await callback.message.reply_photo(photo=photo, caption=caption)
                    else:
                        await callback.message.reply(caption)
                    await status_msg.delete()
                else:
                    update_order_status(order_id, "FAILED_SUBMIT", error_msg=err_msg)
                    err_caption = f"❌ <b>Ошибка отправки заказа #{order_id}:</b>\n<code>{html.escape(err_msg[:400])}</code>"
                    if screenshot_path and Path(screenshot_path).exists():
                        photo = FSInputFile(str(screenshot_path))
                        await callback.message.reply_photo(photo=photo, caption=err_caption)
                        await status_msg.delete()
                    else:
                        await status_msg.edit_text(err_caption)
                    await restore_card_keyboard(callback.message, order_id)
            except Exception as e:
                logger.error(f"Ошибка при исполнении callback approve для #{order_id}: {e}")
                await status_msg.edit_text(f"❌ Ошибка выполнения: {html.escape(str(e))}")
                await restore_card_keyboard(callback.message, order_id)
        else:
            await status_msg.edit_text("⚠️ Обработчик браузера не готов. Попробуйте позже.")
            await restore_card_keyboard(callback.message, order_id)

    @dispatcher.callback_query(F.data.startswith("reject:"))
    async def process_reject(callback: types.CallbackQuery):
        if str(callback.from_user.id) != str(config.TELEGRAM_CHAT_ID):
            await callback.answer("⛔ Доступ запрещен!", show_alert=True)
            return

        order_id = callback.data.split(":", 1)[1]
        update_order_status(order_id, "REJECTED_BY_USER")
        await callback.answer("Заказ отклонен")
        try:
            await callback.message.edit_text(
                f"{callback.message.html_text}\n\n❌ <b>[ОТКЛОНЕНО ВАМИ]</b>",
                reply_markup=None
            )
        except Exception:
            pass

    @dispatcher.callback_query(F.data.startswith("price:"))
    async def process_price_edit(callback: types.CallbackQuery, state: FSMContext):
        if str(callback.from_user.id) != str(config.TELEGRAM_CHAT_ID):
            await callback.answer("⛔ Доступ запрещен!", show_alert=True)
            return

        order_id = callback.data.split(":", 1)[1]
        order = get_order_by_id(order_id)
        if not order or order["status"] not in EDITABLE_STATUSES:
            await callback.answer("Цену уже нельзя изменить: отклик отправлен или заказ отклонён", show_alert=True)
            return

        await state.set_state(PriceEdit.waiting_price)
        await state.update_data(order_id=order_id, card_message_id=callback.message.message_id)
        await callback.answer()
        current_price = order.get("form_price") or order.get("desired_price")
        current_text = f"Сейчас в отклике: {current_price} ₽. " if current_price else ""
        await callback.message.reply(
            f"✏️ <b>Новая цена для заказа #{order_id}</b>\n"
            f"{current_text}Kwork примет цену {price_limits_text(order['budget_info'])}.\n"
            f"Отправьте число, например <code>7000</code>. Суммы в тексте отклика бот согласует с новой ценой.\n"
            f"Отмена — /cancel",
            reply_markup=ForceReply(input_field_placeholder="Цена в рублях")
        )

    @dispatcher.message(PriceEdit.waiting_price)
    async def process_new_price(message: types.Message, state: FSMContext):
        if str(message.chat.id) != str(config.TELEGRAM_CHAT_ID):
            return

        data = await state.get_data()
        order_id = data.get("order_id")
        order = get_order_by_id(order_id) if order_id else None
        if not order or order["status"] not in EDITABLE_STATUSES:
            await state.clear()
            await message.answer(f"Заказ #{order_id} уже обработан — цену изменить нельзя.")
            return

        new_price = parse_price_input(message.text or "")
        if new_price is None:
            await message.answer("Не понял цену. Отправьте число, например <code>7000</code>, или /cancel для отмены.")
            return
        min_price, max_price = kwork_price_limits(order["budget_info"] or "")
        if new_price < min_price or (max_price and new_price > max_price):
            await message.answer(
                f"Kwork не примет {new_price} ₽: для этого заказа цена должна быть {price_limits_text(order['budget_info'])}. "
                f"Отправьте другое число или /cancel."
            )
            return

        await state.clear()
        proposal_text = order["proposal_text"] or ""
        warning = ""
        if text_mentions_price(proposal_text):
            status_msg = await message.answer(f"⏳ Меняю цену на {new_price} ₽ и согласую с ней суммы в тексте отклика (до минуты)...")
            try:
                proposal_text = await asyncio.to_thread(rewrite_proposal_price, proposal_text, new_price)
            except Exception as e:
                logger.error(f"Не удалось согласовать текст отклика #{order_id} с новой ценой: {e}")
                warning = (
                    "\n\n⚠️ Текст отклика пересчитать не удалось — в нём могут остаться старые суммы. "
                    "Проверьте текст перед отправкой или измените цену ещё раз."
                )
        else:
            status_msg = await message.answer(f"⏳ Меняю цену на {new_price} ₽...")

        update_order_proposal(order_id, new_price, proposal_text)
        card_text = build_order_card(
            order_id, order["title"], order["budget_info"], f"{new_price} ₽ (изменена вами)", proposal_text, order["duration_days"]
        )
        try:
            await message.bot.edit_message_text(
                text=card_text,
                chat_id=message.chat.id,
                message_id=data["card_message_id"],
                reply_markup=build_order_keyboard(order_id),
                disable_web_page_preview=True
            )
            where = "Карточка заказа выше обновлена"
        except Exception as e:
            logger.warning(f"Не удалось обновить карточку #{order_id}, отправляю новую: {e}")
            await message.answer(card_text, reply_markup=build_order_keyboard(order_id), disable_web_page_preview=True)
            where = "Ниже — обновлённая карточка"
        done_text = f"✅ Цена заказа #{order_id} изменена на {new_price} ₽. {where}: проверьте текст и отправляйте.{warning}"
        try:
            await status_msg.edit_text(done_text)
        except Exception:
            await message.answer(done_text)

async def send_order_card_for_approval(
    order_id: str,
    title: str,
    budget_info: str,
    desired_budget: Optional[int],
    suggested_price: int,
    form_price: int,
    proposal_text: str,
    duration_days: int
) -> bool:
    """
    Отправляет пользователю в Telegram карточку найденного заказа
    с инлайн-кнопками [🚀 Отправить], [❌ Пропустить] и [✏️ Изменить цену].
    """
    t_bot = get_bot()
    if not t_bot or not config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram не настроен, пропуск отправки карточки согласования.")
        return False

    price_str = f"{suggested_price} ₽"
    if form_price and form_price != suggested_price:
        price_str = f"{form_price} ₽ (в форму Kwork) | Реальная цена: {suggested_price} ₽"

    try:
        await t_bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=build_order_card(order_id, title, budget_info, price_str, proposal_text, duration_days),
            reply_markup=build_order_keyboard(order_id),
            disable_web_page_preview=True
        )
        return True
    except Exception as e:
        logger.error(f"Не удалось отправить интерактивную карточку #{order_id} в Telegram: {e}")
        return False

async def start_telegram_polling() -> None:
    """Запускает долгоиграющий polling Telegram-бота."""
    t_bot = get_bot()
    if not t_bot:
        logger.info("Telegram бот не инициализирован (нет токена).")
        return

    dispatcher = get_dispatcher()
    logger.info("🤖 Telegram бот запущен и слушает команды / кнопки...")
    try:
        await dispatcher.start_polling(t_bot, allowed_updates=["message", "callback_query"])
    except Exception as e:
        logger.error(f"Ошибка в Telegram polling: {e}")
