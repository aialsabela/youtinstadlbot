"""
مدیریت «عضویت اجباری در کانال»:
- بررسی عضویت کاربر با Telegram Bot API
- امکان تغییر کانال در حین اجرا (بدون نیاز به ریدیپلوی) با دستور /setchannel
- ذخیره‌ی مقدار فعلی روی دیسک تا بعد از ری‌استارت هم حفظ بشه (تا حد امکان -
  روی Railway/Render فضای دیسک بین دیپلوی‌های جدید پاک می‌شه، پس برای پایداری
  کامل بهتره REQUIRED_CHANNEL رو هم توی env var آپدیت کنی)
"""

import json
import logging
import os
import threading

from telegram import Bot
from telegram.error import TelegramError

from config import Config

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_state = {
    "channel": Config.REQUIRED_CHANNEL,
    "invite_link": Config.CHANNEL_INVITE_LINK,
}


def _load_state() -> None:
    if not os.path.exists(Config.CHANNEL_STATE_FILE):
        return
    try:
        with open(Config.CHANNEL_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        with _lock:
            if "channel" in data:
                _state["channel"] = data["channel"]
            if "invite_link" in data:
                _state["invite_link"] = data["invite_link"]
        logger.info(f"وضعیت کانال از دیسک بارگذاری شد: {_state['channel'] or '(غیرفعال)'}")
    except Exception as e:
        logger.warning(f"خواندن {Config.CHANNEL_STATE_FILE} ناموفق بود: {e}")


def _save_state() -> None:
    try:
        with open(Config.CHANNEL_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"نوشتن {Config.CHANNEL_STATE_FILE} ناموفق بود: {e}")


_load_state()


def get_channel() -> str:
    with _lock:
        return _state["channel"]


def get_invite_link() -> str:
    with _lock:
        return _state["invite_link"]


def set_channel(channel: str, invite_link: str = "") -> None:
    """channel رو تغییر می‌ده. برای غیرفعال کردن، رشته‌ی خالی بده."""
    with _lock:
        _state["channel"] = channel
        _state["invite_link"] = invite_link
    _save_state()


def is_admin(user_id: int) -> bool:
    return user_id in Config.ADMIN_USER_IDS


async def is_member(bot: Bot, user_id: int) -> bool:
    """
    عضویت کاربر توی کانال الزامی رو بررسی می‌کنه. اگه هیچ کانالی تنظیم نشده،
    همیشه True برمی‌گردونه (یعنی این قابلیت غیرفعاله).
    """
    channel = get_channel()
    if not channel:
        return True

    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except TelegramError as e:
        # معمولاً یعنی ربات توی کانال ادمین نیست یا آیدی کانال اشتباهه؛
        # برای امنیت، دسترسی رو می‌بندیم ولی توی لاگ واضح می‌نویسیم چرا.
        logger.warning(
            f"بررسی عضویت کانال {channel} برای کاربر {user_id} شکست خورد: {e}. "
            "مطمئن شو ربات توی اون کانال ادمینه و آیدی/یوزرنیم کانال درسته."
        )
        return False


def build_join_url(channel: str) -> str | None:
    invite_link = get_invite_link()
    if invite_link:
        return invite_link
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}"
    return None
