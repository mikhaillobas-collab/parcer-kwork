import asyncio
import logging
from pathlib import Path
from typing import Optional, Callable, Any

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.client.default import DefaultBotProperties

import config
from database import update_order_status, get_order_by_id, get_stats

logger = logging.getLogger("kwork_bot.tg")

# Глобальный экземпляр бота и диспетчера
bot: Optional[Bot] = None
dp: Optional[Dispatcher] = None

# Функция обратного вызова для запуска отправки в Playwright
# Сигнатура: async def on_approve(order_id: str) -> Tuple[bool, Optional[Path], str]
_approve_handler: Optional[Callable[[str], Any]] = None

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

    @dispatcher.callback_query(F.data.startswith("approve:"))
    async def process_approve(callback: types.CallbackQuery):
        if str(callback.from_user.id) != str(config.TELEGRAM_CHAT_ID):
            await callback.answer("⛔ Доступ запрещен!", show_alert=True)
            return

        order_id = callback.data.split(":", 1)[1]
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
                    await status_msg.edit_text(f"❌ <b>Ошибка отправки заказа #{order_id}:</b>\n{err_msg}")
            except Exception as e:
                logger.error(f"Ошибка при исполнении callback approve для #{order_id}: {e}")
                await status_msg.edit_text(f"❌ Ошибка выполнения: {e}")
        else:
            await status_msg.edit_text("⚠️ Обработчик браузера не готов. Попробуйте позже.")

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
    с инлайн-кнопками [🚀 Отправить] и [❌ Пропустить].
    """
    t_bot = get_bot()
    if not t_bot or not config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram не настроен, пропуск отправки карточки согласования.")
        return False

    price_str = f"{suggested_price} ₽"
    if form_price and form_price != suggested_price:
        price_str = f"{form_price} ₽ (в форму Kwork) | Реальная цена: {suggested_price} ₽"

    text = (
        f"🔔 <b>НАЙДЕН НОВЫЙ ПОДХОДЯЩИЙ ЗАКАЗ!</b>\n\n"
        f"📌 <b>#{order_id}:</b> {title}\n"
        f"💰 <b>Бюджет клиента:</b> {budget_info}\n"
        f"🏷️ <b>Наше предложение:</b> {price_str}\n"
        f"⏱️ <b>Срок:</b> {duration_days} дн.\n\n"
        f"📝 <b>Сгенерированный отклик:</b>\n"
        f"<i>{proposal_text}</i>\n\n"
        f"🔗 <a href='https://kwork.ru/projects/{order_id}/view'>Открыть заказ на Kwork</a>"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Отправить отклик", callback_data=f"approve:{order_id}"),
            InlineKeyboardButton(text="❌ Пропустить", callback_data=f"reject:{order_id}")
        ]
    ])

    try:
        await t_bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            reply_markup=keyboard,
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
