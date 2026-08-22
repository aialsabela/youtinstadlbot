import asyncio
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Config
from channel_gate import build_join_url, get_channel, is_admin, is_member, set_channel
from downloader import (
    BotCheckError,
    Downloader,
    DownloaderError,
    FileTooLargeError,
    UnavailableError,
    is_youtube,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO),
)
logger = logging.getLogger(__name__)

URL_REGEX = re.compile(
    r"(https?://)?(www\.)?(youtube\.com|youtu\.be|instagram\.com)/\S+",
    re.IGNORECASE,
)

PENDING_TTL_SECONDS = 30 * 60  # لینک‌های انتخاب‌نشده بعد از این مدت فراموش می‌شن

# --- وضعیت مشترک سراسر ربات -------------------------------------------------

downloader = Downloader()
download_semaphore = asyncio.Semaphore(Config.MAX_CONCURRENT_DOWNLOADS)

_last_request_time: dict[int, float] = {}
_cooldown_lock = threading.Lock()

_pending_urls: dict[str, tuple[str, float]] = {}
_pending_lock = threading.Lock()


def extract_url(text: str) -> str | None:
    match = URL_REGEX.search(text)
    return match.group(0) if match else None


def check_cooldown(user_id: int) -> float:
    """اگه کاربر توی دوره‌ی cooldown باشه، ثانیه‌های باقی‌مونده رو برمی‌گردونه؛ وگرنه ۰."""
    now = time.monotonic()
    with _cooldown_lock:
        last = _last_request_time.get(user_id, 0.0)
        remaining = Config.USER_COOLDOWN_SECONDS - (now - last)
        if remaining > 0:
            return remaining
        _last_request_time[user_id] = now
        return 0.0


def store_pending_url(url: str) -> str:
    """لینک رو با یه توکن کوتاه ذخیره می‌کنه (چون callback_data تلگرام محدودیت حجم داره)."""
    token = uuid.uuid4().hex[:10]
    now = time.monotonic()
    with _pending_lock:
        expired = [t for t, (_, ts) in _pending_urls.items() if now - ts > PENDING_TTL_SECONDS]
        for t in expired:
            _pending_urls.pop(t, None)
        _pending_urls[token] = (url, now)
    return token


def get_pending_url(token: str) -> str | None:
    with _pending_lock:
        entry = _pending_urls.get(token)
        return entry[0] if entry else None


def build_quality_keyboard(token: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🎬 بهترین کیفیت", callback_data=f"dl|{token}|video|best")],
        [
            InlineKeyboardButton("1080p", callback_data=f"dl|{token}|video|1080"),
            InlineKeyboardButton("720p", callback_data=f"dl|{token}|video|720"),
        ],
        [
            InlineKeyboardButton("480p", callback_data=f"dl|{token}|video|480"),
            InlineKeyboardButton("360p", callback_data=f"dl|{token}|video|360"),
        ],
        [InlineKeyboardButton("🎵 فقط صدا (MP3)", callback_data=f"dl|{token}|audio|best")],
    ]
    return InlineKeyboardMarkup(rows)


def build_join_keyboard(token: str, channel: str) -> InlineKeyboardMarkup:
    rows = []
    url = build_join_url(channel)
    if url:
        rows.append([InlineKeyboardButton("📢 عضویت در کانال", url=url)])
    rows.append([InlineKeyboardButton("✅ عضو شدم، بررسی کن", callback_data=f"chk|{token}")])
    return InlineKeyboardMarkup(rows)


def join_required_text(channel: str) -> str:
    label = channel if channel.startswith("@") else "کانال ما"
    return (
        f"برای استفاده از ربات، اول باید عضو {label} بشی.\n\n"
        "بعد از عضویت، روی دکمه‌ی «عضو شدم، بررسی کن» بزن."
    )


# --- هندلرهای تلگرام ---------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! 👋\n\n"
        "لینک ویدیوی یوتیوب یا اینستاگرام رو برام بفرست تا دانلودش کنم.\n"
        "برای لینک‌های یوتیوب می‌تونی کیفیت ویدیو یا حالت فقط-صدا (MP3) رو هم انتخاب کنی.\n\n"
        f"⚠️ حداکثر حجم مجاز: {Config.MAX_FILE_SIZE_MB}MB\n"
        f"⏳ فاصله‌ی لازم بین دو درخواست: {Config.USER_COOLDOWN_SECONDS} ثانیه"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "فقط کافیه لینک ویدیو رو بفرستی، خودم بقیه‌ش رو انجام می‌دم 🙂\n"
        "برای یوتیوب، بعد از فرستادن لینک، یه منو برای انتخاب کیفیت/MP3 نشون داده می‌شه.\n"
        "دستورها:\n/start - شروع\n/help - راهنما"
    )
    if is_admin(update.effective_user.id):
        text += (
            "\n\nدستورهای ادمین:\n"
            "/setchannel @channelusername - تنظیم کانال اجباری\n"
            "/setchannel off - غیرفعال کردن عضویت اجباری\n"
            "/setchannel - نمایش کانال فعلی"
        )
    await update.message.reply_text(text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    url = extract_url(text)

    if not url:
        await update.message.reply_text("لطفاً یک لینک معتبر از یوتیوب یا اینستاگرام بفرست.")
        return

    token = store_pending_url(url)
    user_id = update.effective_user.id

    channel = get_channel()
    if channel and not await is_member(context.bot, user_id):
        await update.message.reply_text(
            join_required_text(channel),
            reply_markup=build_join_keyboard(token, channel),
        )
        return

    if is_youtube(url):
        await update.message.reply_text(
            "چه چیزی می‌خوای دانلود کنم؟",
            reply_markup=build_quality_keyboard(token),
        )
        return

    # اینستاگرام: کیفیت انتخابی نداره، مستقیم دانلود می‌شه
    remaining = check_cooldown(user_id)
    if remaining > 0:
        await update.message.reply_text(f"⏳ لطفاً {remaining:.0f} ثانیه صبر کن و دوباره امتحان کن.")
        return

    if download_semaphore.locked():
        status_msg = await update.message.reply_text("⏳ سرور شلوغه، درخواستت توی صفه...")
    else:
        status_msg = await update.message.reply_text("⏳ در حال دانلود، چند لحظه صبر کن...")

    async with download_semaphore:
        await _process_download(context, update.effective_chat.id, url, status_msg)


async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        _, token, mode, quality = query.data.split("|", 3)
    except ValueError:
        await query.edit_message_text("❌ درخواست نامعتبره. لینک رو دوباره بفرست.")
        return

    url = get_pending_url(token)
    if not url:
        await query.edit_message_text("❌ این درخواست منقضی شده. لطفاً لینک رو دوباره بفرست.")
        return

    user_id = query.from_user.id

    channel = get_channel()
    if channel and not await is_member(context.bot, user_id):
        await query.edit_message_text(
            join_required_text(channel),
            reply_markup=build_join_keyboard(token, channel),
        )
        return

    remaining = check_cooldown(user_id)
    if remaining > 0:
        await query.edit_message_text(f"⏳ لطفاً {remaining:.0f} ثانیه صبر کن و دوباره امتحان کن.")
        return

    status_msg = query.message
    if download_semaphore.locked():
        await status_msg.edit_text("⏳ سرور شلوغه، درخواستت توی صفه...")
    else:
        await status_msg.edit_text("⏳ در حال دانلود، چند لحظه صبر کن...")

    async with download_semaphore:
        await _process_download(context, status_msg.chat_id, url, status_msg, mode=mode, quality=quality)


async def handle_membership_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """وقتی کاربر روی «عضو شدم، بررسی کن» می‌زنه."""
    query = update.callback_query

    try:
        token = query.data.split("|", 1)[1]
    except IndexError:
        await query.answer("درخواست نامعتبره.", show_alert=True)
        return

    url = get_pending_url(token)
    if not url:
        await query.answer("این درخواست منقضی شده. لینک رو دوباره بفرست.", show_alert=True)
        return

    user_id = query.from_user.id
    channel = get_channel()

    if channel and not await is_member(context.bot, user_id):
        await query.answer("هنوز عضو کانال نیستی 🙁", show_alert=True)
        return

    await query.answer("عضویت تایید شد ✅")

    if is_youtube(url):
        await query.edit_message_text(
            "چه چیزی می‌خوای دانلود کنم؟",
            reply_markup=build_quality_keyboard(token),
        )
        return

    # اینستاگرام: مستقیم دانلود می‌شه
    remaining = check_cooldown(user_id)
    status_msg = query.message
    if remaining > 0:
        await status_msg.edit_text(f"⏳ لطفاً {remaining:.0f} ثانیه صبر کن و دوباره امتحان کن.")
        return

    if download_semaphore.locked():
        await status_msg.edit_text("⏳ سرور شلوغه، درخواستت توی صفه...")
    else:
        await status_msg.edit_text("⏳ در حال دانلود، چند لحظه صبر کن...")

    async with download_semaphore:
        await _process_download(context, status_msg.chat_id, url, status_msg)


async def setchannel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    دستور ادمین برای تغییر کانال اجباری در حین اجرا (بدون نیاز به ریدیپلوی):
      /setchannel @channelusername
      /setchannel -1001234567890 https://t.me/+inviteHash   (کانال خصوصی + لینک دعوت)
      /setchannel off   (غیرفعال کردن قابلیت)
      /setchannel       (نمایش وضعیت فعلی)
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔️ این دستور فقط برای ادمین‌های ربات مجازه.")
        return

    args = context.args
    if not args:
        current = get_channel()
        status = current if current else "غیرفعال"
        await update.message.reply_text(f"کانال فعلی: {status}")
        return

    if args[0].lower() == "off":
        set_channel("", "")
        await update.message.reply_text("✅ قابلیت عضویت اجباری غیرفعال شد.")
        return

    channel = args[0]
    invite_link = args[1] if len(args) > 1 else ""

    if not (channel.startswith("@") or channel.startswith("-100")):
        await update.message.reply_text(
            "❌ فرمت کانال باید یوزرنیم عمومی (مثل @mychannel) یا آیدی عددی کانال "
            "خصوصی (مثل -1001234567890) باشه."
        )
        return

    set_channel(channel, invite_link)
    await update.message.reply_text(f"✅ کانال اجباری تنظیم شد: {channel}")


async def _process_download(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    url: str,
    status_msg,
    mode: str = "video",
    quality: str = "best",
) -> None:
    try:
        await status_msg.edit_text("⏳ در حال دانلود، چند لحظه صبر کن...")
    except TelegramError:
        pass

    action = ChatAction.UPLOAD_VOICE if mode == "audio" else ChatAction.UPLOAD_VIDEO
    await context.bot.send_chat_action(chat_id=chat_id, action=action)

    tmp_dir = tempfile.mkdtemp(prefix="ytbot_")
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, downloader.download, url, tmp_dir, mode, quality)

        title = result["title"]
        items = result["items"]

        valid_items = []
        oversized_count = 0
        for item in items:
            size_mb = os.path.getsize(item["path"]) / (1024 * 1024)
            if size_mb > Config.MAX_FILE_SIZE_MB:
                oversized_count += 1
                continue
            valid_items.append(item)

        if not valid_items:
            await status_msg.edit_text(
                f"❌ حجم فایل(ها) بیشتر از حد مجازه ({Config.MAX_FILE_SIZE_MB}MB)."
            )
            return

        await status_msg.edit_text("📤 در حال ارسال...")

        if len(valid_items) == 1:
            item = valid_items[0]
            with open(item["path"], "rb") as f:
                if item["type"] == "photo":
                    await context.bot.send_photo(
                        chat_id=chat_id, photo=f, caption=title[:1000],
                        write_timeout=180, read_timeout=180, connect_timeout=60,
                    )
                elif item["type"] == "audio":
                    await context.bot.send_audio(
                        chat_id=chat_id, audio=f, title=title[:64], caption=title[:1000],
                        write_timeout=180, read_timeout=180, connect_timeout=60,
                    )
                else:
                    await context.bot.send_video(
                        chat_id=chat_id, video=f, caption=title[:1000], supports_streaming=True,
                        write_timeout=180, read_timeout=180, connect_timeout=60,
                    )
        else:
            # پست چرخشی (carousel) - چند رسانه در قالب یک آلبوم ارسال می‌شه
            batch = valid_items[:10]  # محدودیت خود تلگرام برای media group
            open_files = []
            try:
                media = []
                for i, item in enumerate(batch):
                    f = open(item["path"], "rb")
                    open_files.append(f)
                    caption = title[:1000] if i == 0 else None
                    if item["type"] == "photo":
                        media.append(InputMediaPhoto(media=f, caption=caption))
                    else:
                        media.append(InputMediaVideo(media=f, caption=caption))

                await context.bot.send_media_group(
                    chat_id=chat_id, media=media,
                    write_timeout=180, read_timeout=180, connect_timeout=60,
                )
            finally:
                for f in open_files:
                    f.close()

            if len(valid_items) > 10:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(f"⚠️ این پست {len(valid_items)} رسانه داشت؛ فقط ۱۰ تای اول ارسال شد "
                          "(محدودیت تلگرام برای آلبوم)."),
                )

        if oversized_count:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ {oversized_count} مورد از رسانه‌های این پست به‌خاطر حجم زیاد ارسال نشد.",
            )

        await status_msg.delete()

    except BotCheckError:
        await status_msg.edit_text(
            "❌ پلتفرم درخواست‌ها رو محدود کرده یا نیاز به ورود داره "
            "(معمولاً برای IP سرورهای ابری پیش میاد یا رسانه خصوصیه).\n\n"
            "این مشکل با تنظیم کوکی حساب گوگل/اینستاگرام روی سرور حل می‌شه. "
            "توضیحات کامل توی README پروژه، بخش «حل خطای Sign in to confirm» هست."
        )

    except UnavailableError:
        await status_msg.edit_text(
            "❌ این محتوا در دسترس نیست (خصوصی، حذف‌شده، یا محدود به کشور خاصه)."
        )

    except FileTooLargeError as e:
        await status_msg.edit_text(f"❌ {e}")

    except DownloaderError as e:
        error_text = str(e)
        await status_msg.edit_text(
            f"❌ دانلود ناموفق بود.\n\nجزئیات خطا:\n`{error_text[:500]}`",
            parse_mode="Markdown",
        )

    except TelegramError as e:
        logger.warning(f"Telegram error while sending result: {e}")
        try:
            await status_msg.edit_text(f"❌ خطا در ارسال فایل به تلگرام: {e}")
        except TelegramError:
            pass

    except Exception as e:
        logger.exception("Unexpected error during download")
        try:
            await status_msg.edit_text(f"❌ خطای غیرمنتظره: {e}")
        except TelegramError:
            pass

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Unhandled exception: {context.error}", exc_info=context.error)


# --- سرور health-check برای پلتفرم‌هایی مثل Render --------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running.")

    def log_message(self, format, *args):
        pass


def _run_keep_alive_server() -> None:
    if not Config.PORT:
        return
    server = HTTPServer(("0.0.0.0", Config.PORT), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"Keep-alive HTTP server listening on port {Config.PORT}")


# --- اجرا ---------------------------------------------------------------

def main() -> None:
    Config.validate()
    _run_keep_alive_server()

    app: Application = ApplicationBuilder().token(Config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setchannel", setchannel_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_quality_choice, pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(handle_membership_check, pattern=r"^chk\|"))
    app.add_error_handler(on_error)

    logger.info(
        f"Bot starting... max_concurrent={Config.MAX_CONCURRENT_DOWNLOADS} "
        f"cooldown={Config.USER_COOLDOWN_SECONDS}s cookies={'yes' if (Config.COOKIES_B64 or Config.COOKIES_FILE) else 'no'} "
        f"required_channel={get_channel() or '(disabled)'} admins={len(Config.ADMIN_USER_IDS)}"
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
