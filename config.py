"""
تنظیمات ربات - همه‌ی مقادیر از متغیرهای محیطی خونده می‌شن.
"""

import os


class Config:
    # توکن ربات تلگرام (اجباری)
    BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")

    # حداکثر حجم فایل قابل ارسال (تلگرام معمولی: ۵۰ مگابایت)
    MAX_FILE_SIZE_MB: int = int(os.environ.get("MAX_FILE_SIZE_MB", "50"))

    # کوکی به دو روش قابل تنظیمه:
    # ۱) COOKIES_B64: محتوای فایل cookies.txt به‌صورت base64 (روش امن و پیشنهادی -
    #    نیازی به کامیت کردن فایل حساس توی گیت نیست)
    # ۲) COOKIES_FILE: مسیر یک فایل کوکی که از قبل توی ریپو/سرور قرار داده شده
    COOKIES_B64: str = os.environ.get("COOKIES_B64", "")
    COOKIES_FILE: str = os.environ.get("COOKIES_FILE", "")

    # حداکثر تعداد دانلود همزمان (برای جلوگیری از فشار زیاد روی سرور رایگان)
    MAX_CONCURRENT_DOWNLOADS: int = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))

    # حداقل فاصله‌ی زمانی بین دو درخواست هر کاربر (ثانیه) - جلوگیری از اسپم
    USER_COOLDOWN_SECONDS: int = int(os.environ.get("USER_COOLDOWN_SECONDS", "8"))

    # پورت برای health-check سرور (Render خودکار PORT رو ست می‌کنه)
    PORT: int = int(os.environ.get("PORT", "0"))

    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

    # حداکثر مدت‌زمان مجاز ویدیو به ثانیه (پیش‌فرض: ۲ ساعت) - جلوگیری از دانلود فایل‌های عظیم
    MAX_DURATION_SECONDS: int = int(os.environ.get("MAX_DURATION_SECONDS", str(2 * 60 * 60)))

    # --- عضویت اجباری در کانال ---------------------------------------------
    # کانالی که کاربر باید عضوش باشه تا بتونه از ربات استفاده کنه.
    # فرمت: یوزرنیم عمومی با @ (مثل @mychannel) یا آیدی عددی کانال خصوصی (مثل -1001234567890).
    # خالی = غیرفعال (بدون نیاز به عضویت). این مقدار فقط "پیش‌فرض اولیه"‌ست؛
    # با دستور /setchannel توی خود ربات هم قابل تغییره (بدون نیاز به ریدیپلوی).
    REQUIRED_CHANNEL: str = os.environ.get("REQUIRED_CHANNEL", "")

    # لینک دعوت، فقط لازمه اگه کانال خصوصیه (آیدی عددی داره، نه یوزرنیم عمومی)
    CHANNEL_INVITE_LINK: str = os.environ.get("CHANNEL_INVITE_LINK", "")

    # مسیر فایلی که مقدار کانال رو (بعد از تغییر با /setchannel) روی دیسک نگه می‌داره
    CHANNEL_STATE_FILE: str = os.environ.get("CHANNEL_STATE_FILE", "channel_state.json")

    # آیدی عددی کاربرهایی که اجازه دارن با /setchannel کانال رو تغییر بدن (با کاما جدا کن)
    _ADMIN_IDS_RAW: str = os.environ.get("ADMIN_USER_IDS", "")
    ADMIN_USER_IDS: set[int] = {
        int(x) for x in _ADMIN_IDS_RAW.split(",") if x.strip().lstrip("-").isdigit()
    }

    @classmethod
    def validate(cls) -> None:
        if not cls.BOT_TOKEN:
            raise RuntimeError(
                "متغیر محیطی BOT_TOKEN تنظیم نشده. توکن رو از @BotFather بگیر و ست کن."
            )
        if cls.REQUIRED_CHANNEL and not cls.ADMIN_USER_IDS:
            import logging
            logging.getLogger(__name__).warning(
                "REQUIRED_CHANNEL تنظیم شده ولی ADMIN_USER_IDS خالیه؛ "
                "هیچ‌کس نمی‌تونه با /setchannel کانال رو تغییر بده."
            )
