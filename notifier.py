import logging
import json
import urllib.request
from pathlib import Path
from typing import Optional, Union
import config

logger = logging.getLogger("kwork_bot")

def is_telegram_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)

def send_telegram_message(text: str) -> bool:
    if not is_telegram_configured():
        return False

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as response:
            res_data = json.loads(response.read().decode())
            return res_data.get("ok", False)
    except Exception as e:
        logger.warning(f"Не удалось отправить сообщение в Telegram: {e}")
        return False

def send_telegram_photo(photo_path: Union[str, Path], caption: str = "") -> bool:
    if not is_telegram_configured():
        return False

    photo_path = Path(photo_path)
    if not photo_path.exists():
        return send_telegram_message(caption)

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto"
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    b_boundary = boundary.encode("utf-8")
    crlf = b"\r\n"
    
    try:
        with open(photo_path, "rb") as f:
            file_bytes = f.read()

        body = bytearray()
        
        # chat_id
        body += b"--" + b_boundary + crlf
        body += b'Content-Disposition: form-data; name="chat_id"' + crlf + crlf
        body += str(config.TELEGRAM_CHAT_ID).encode("utf-8") + crlf
        
        # caption
        if caption:
            clean_caption = caption[:1000]
            body += b"--" + b_boundary + crlf
            body += b'Content-Disposition: form-data; name="caption"' + crlf + crlf
            body += clean_caption.encode("utf-8") + crlf
            body += b"--" + b_boundary + crlf
            body += b'Content-Disposition: form-data; name="parse_mode"' + crlf + crlf
            body += b"HTML" + crlf

        # photo
        body += b"--" + b_boundary + crlf
        body += f'Content-Disposition: form-data; name="photo"; filename="{photo_path.name}"'.encode("utf-8") + crlf
        body += b"Content-Type: image/png" + crlf + crlf
        body += file_bytes + crlf
        
        body += b"--" + b_boundary + b"--" + crlf

        req = urllib.request.Request(
            url,
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            res_data = json.loads(response.read().decode())
            return res_data.get("ok", False)
    except Exception as e:
        logger.warning(f"Не удалось отправить скриншот в Telegram: {e}. Отправка текстом...")
        return send_telegram_message(caption)

def notify_order_offer(order_id: str, title: str, budget_info: str, form_price: int, real_price: int, proposal_text: str, duration_days: int, is_dry_run: bool, screenshot_path: Optional[Union[str, Path]] = None):
    mode_badge = "🛡️ <b>[ТЕСТОВЫЙ РЕЖИМ DRY_RUN]</b>" if is_dry_run else "🚀 <b>[ОТКЛИК ОТПРАВЛЕН]</b>"
    
    price_info = f"{form_price} ₽"
    if real_price and real_price != form_price:
        price_info = f"{form_price} ₽ (в форме Kwork) | Реальная цена: {real_price} ₽"

    caption = (
        f"{mode_badge}\n\n"
        f"📌 <b>Заказ #{order_id}:</b> {title}\n"
        f"💰 <b>Бюджет:</b> {budget_info}\n"
        f"🏷️ <b>Предложение:</b> {price_info}\n"
        f"⏱️ <b>Срок:</b> {duration_days} дн.\n\n"
        f"📝 <b>Текст отклика:</b>\n{proposal_text[:600]}...\n\n"
        f"🔗 https://kwork.ru/projects/{order_id}/view"
    )

    if screenshot_path and Path(screenshot_path).exists():
        send_telegram_photo(screenshot_path, caption)
    else:
        send_telegram_message(caption)
