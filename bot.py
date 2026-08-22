import os
import re
import logging
import asyncio
import tempfile
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# تنظیمات
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

# حداکثر حجم فایل قابل ارسال توسط ربات‌های تلگرام (بدون سرور اختصاصی) ~ ۵۰ مگابایت
MAX_FILE_SIZE_MB = 50

URL_REGEX = re.compile(
    r"(https?://)?(www\.)?(youtube\.com|youtu\.be|instagram\.com)/[^\s]+",
    re.IGNORECASE,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# توابع کمکی
# ---------------------------------------------------------------------------

def extract_url(text: str) -> str | None:
    match = URL_REGEX.search(text)
    return match.group(0) if match else None


def download_media(url: str, out_dir: str) -> dict:
    """با yt-dlp لینک رو دانلود می‌کنه و مسیر فایل رو برمی‌گردونه."""
    out_template = os.path.join(out_dir, "%(title).80s.%(ext)s")

    cookies_file = os.environ.get("COOKIES_FILE")  # مسیر فایل کوکی، اگه ست شده باشه

    ydl_opts = {
        "outtmpl": out_template,
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "max_filesize": MAX_FILE_SIZE_MB * 1024 * 1024,
        "nocheckcertificate": True,
        "retries": 5,
        "fragment_retries": 5,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
        # از کلاینت اندروید یوتیوب استفاده می‌کنه که کمتر بلاک می‌شه
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }

    if cookies_file and os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filepath = ydl.prepare_filename(info)
        return {"path": filepath, "title": info.get("title", "video")}


# ---------------------------------------------------------------------------
# هندلرها
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! 👋\n\n"
        "لینک ویدیوی یوتیوب یا اینستاگرام رو برام بفرست تا برات دانلودش کنم.\n\n"
        f"⚠️ به‌خاطر محدودیت تلگرام، فایل‌های بالای {MAX_FILE_SIZE_MB} مگابایت قابل ارسال نیستن."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "فقط کافیه لینک ویدیو رو بفرستی، خودم بقیه‌ش رو انجام می‌دم 🙂"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    url = extract_url(text)

    if not url:
        await update.message.reply_text(
            "لطفاً یک لینک معتبر از یوتیوب یا اینستاگرام بفرست."
        )
        return

    status_msg = await update.message.reply_text("⏳ در حال دانلود، چند لحظه صبر کن...")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)

    tmp_dir = tempfile.mkdtemp(prefix="ytbot_")
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, download_media, url, tmp_dir)

        filepath = result["path"]
        title = result["title"]

        if not os.path.exists(filepath):
            await status_msg.edit_text("❌ فایل پیدا نشد. شاید لینک خصوصی یا نامعتبر باشه.")
            return

        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            await status_msg.edit_text(
                f"❌ حجم فایل ({size_mb:.1f}MB) بیشتر از حد مجاز تلگرامه "
                f"({MAX_FILE_SIZE_MB}MB)."
            )
            return

        await status_msg.edit_text("📤 در حال ارسال...")
        with open(filepath, "rb") as f:
            await update.message.reply_video(
                video=f,
                caption=title[:1000],
                supports_streaming=True,
                write_timeout=120,
                read_timeout=120,
            )
        await status_msg.delete()

    except yt_dlp.utils.DownloadError as e:
        logger.warning(f"Download error: {e}")
        error_text = str(e)
        # کوتاه‌شده‌ی پیام خطای واقعی رو نشون می‌دیم تا بشه دلیل رو فهمید
        await status_msg.edit_text(
            "❌ دانلود ناموفق بود.\n\n"
            f"جزئیات خطا:\n`{error_text[:500]}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("Unexpected error")
        await status_msg.edit_text(f"❌ خطای غیرمنتظره: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# سرور کوچک برای پلتفرم‌هایی مثل Render که نیاز به یک پورت HTTP باز دارن
# (Railway نیازی به این نداره، ولی اجراش ضرری هم نمی‌زنه)
# ---------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running.")

    def log_message(self, format, *args):
        pass  # لاگ‌های اضافه رو خاموش می‌کنیم


def _run_keep_alive_server():
    port = int(os.environ.get("PORT", 0))
    if not port:
        return  # اگه PORT ست نشده (مثلاً روی Railway/لوکال)، نیازی به این سرور نیست
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"Keep-alive HTTP server listening on port {port}")


# ---------------------------------------------------------------------------
# اجرا
# ---------------------------------------------------------------------------

def main():
    if BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise RuntimeError(
            "توکن ربات رو تنظیم نکردی! متغیر محیطی BOT_TOKEN رو ست کن یا مستقیم توی کد بذار."
        )

    _run_keep_alive_server()

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
