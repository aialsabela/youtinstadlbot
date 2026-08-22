"""
لایه‌ی دانلود - دور yt-dlp رو با منطق حرفه‌ای می‌پیچه:
- ذخیره‌ی امن کوکی (از base64 یا فایل) - برای یوتیوب و اینستاگرام مشترکه
- یوتیوب: تلاش خودکار روی چند کلاینت (android/ios/tv/web) در صورت شکست
- اینستاگرام: پشتیبانی از پست تکی (عکس/ویدیو/ریل)، پست چرخشی چندرسانه‌ای (carousel)
  و حساب‌های خصوصی (با کوکی)
- خطاهای دقیق و قابل‌تشخیص برای لایه‌ی بالاتر (bot.py)
"""

import base64
import logging
import os
import tempfile

import yt_dlp

from config import Config

logger = logging.getLogger(__name__)

# ترتیب کلاینت‌هایی که یوتیوب رو امتحان می‌کنیم؛ هرکدوم شکست خورد، بعدی رو امتحان می‌کنیم.
# None یعنی هیچ کلاینت خاصی اجبار نمی‌شه و خود yt-dlp با منطق پیش‌فرضش (که معمولاً
# ترکیبی از چند کلاینته و بهتر نگه‌داری می‌شه) فرمت‌ها رو انتخاب می‌کنه - این حالت
# اول امتحان می‌شه چون قابل‌اعتمادترینه. اجبار به یک کلاینت تکی (مخصوصاً android)
# این روزها زیاد باعث خطای "فرمت موجود نیست" می‌شه، چون یوتیوب برای اون کلاینت‌ها
# محدودیت‌های تازه (نیاز به PO token) گذاشته؛ برای همین فقط به‌عنوان fallback
# برای حالت‌های بلاک‌شده (خطای "Sign in to confirm") نگه داشته شدن.
YOUTUBE_CLIENT_CHAIN = [None, "tv_embedded", "web", "android", "ios"]

# زنجیره‌ی فرمت ویدیو: بدون فیلتر سخت‌گیرانه‌ی پسوند - ffmpeg خودش موقع merge به mp4 تبدیل می‌کنه.
# فیلتر کردن روی ext=mp4/m4a باعث می‌شد بعضی کلاینت‌ها (خصوصاً اندروید) هیچ فرمتی پیدا نکنن.
VIDEO_FORMAT_SELECTOR = "bv*+ba/best"

# فرمت‌سلکتور برای کیفیت‌های مشخص (ارتفاع تصویر به پیکسل)
QUALITY_HEIGHTS = {"1080": 1080, "720": 720, "480": 480, "360": 360}

# فرمت صوتی خروجی برای حالت "فقط صدا"
AUDIO_CODEC = "mp3"
AUDIO_QUALITY_KBPS = "192"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}

# حداکثر تعداد آیتم قابل ارسال در یک پست چرخشی (محدودیت خود تلگرام برای media group)
MAX_CAROUSEL_ITEMS = 10

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class DownloaderError(Exception):
    """خطای پایه برای هر مشکل دانلود."""


class BotCheckError(DownloaderError):
    """پلتفرم درخواست احراز هویت/کوکی کرده (Sign in / rate-limited / login required)."""


class UnavailableError(DownloaderError):
    """رسانه خصوصی/حذف‌شده/محدود به کشور خاصه یا اصلاً وجود نداره."""


class FileTooLargeError(DownloaderError):
    """حجم یا مدت‌زمان فایل بیشتر از حد مجازه."""


def is_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def is_instagram(url: str) -> bool:
    return "instagram.com" in url or "instagr.am" in url


def _resolve_cookies_path() -> str | None:
    """
    مسیر فایل کوکی رو برمی‌گردونه. اگه COOKIES_B64 ست شده باشه (روش امن پیشنهادی)،
    محتوا رو دیکد کرده و توی یه فایل موقت می‌نویسه. در غیر این‌صورت از COOKIES_FILE
    (مسیر فایلی که از قبل روی دیسک/ریپو هست) استفاده می‌کنه.
    یک فایل کوکی می‌تونه هم‌زمان کوکی یوتیوب و اینستاگرام رو داشته باشه.
    """
    if Config.COOKIES_B64:
        try:
            raw = base64.b64decode(Config.COOKIES_B64)
        except Exception as e:
            logger.error(f"COOKIES_B64 نامعتبره و قابل دیکد نیست: {e}")
            return None

        fd, path = tempfile.mkstemp(prefix="cookies_", suffix=".txt")
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        logger.info("کوکی از COOKIES_B64 با موفقیت بارگذاری شد.")
        return path

    if Config.COOKIES_FILE and os.path.exists(Config.COOKIES_FILE):
        logger.info(f"کوکی از فایل {Config.COOKIES_FILE} بارگذاری شد.")
        return Config.COOKIES_FILE

    logger.warning(
        "هیچ کوکی‌ای تنظیم نشده. برای پست‌های خصوصی اینستاگرام و بعضی ویدیوهای "
        "یوتیوب باید COOKIES_B64 یا COOKIES_FILE رو تنظیم کنی."
    )
    return None


class Downloader:
    """رابط اصلی برای دانلود رسانه از یوتیوب/اینستاگرام."""

    def __init__(self) -> None:
        self._cookies_path = _resolve_cookies_path()

    # -- تنظیمات مشترک -------------------------------------------------

    def _base_opts(self, out_dir: str, use_cookies: bool = True) -> dict:
        out_template = os.path.join(out_dir, "%(id)s_%(autonumber)s.%(ext)s")

        opts: dict = {
            "outtmpl": out_template,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
            "max_filesize": Config.MAX_FILE_SIZE_MB * 1024 * 1024,
            "nocheckcertificate": True,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "http_headers": {"User-Agent": USER_AGENT},
        }

        if use_cookies and self._cookies_path:
            opts["cookiefile"] = self._cookies_path

        return opts

    @staticmethod
    def _media_type_for(filepath: str) -> str:
        ext = os.path.splitext(filepath)[1].lower()
        return "photo" if ext in IMAGE_EXTENSIONS else "video"

    @staticmethod
    def _find_entry_file(out_dir: str, entry_id: str, already_used: set) -> str | None:
        """فایل مربوط به یک آیتم مشخص (با id) رو توی پوشه‌ی خروجی پیدا می‌کنه."""
        candidates = [
            os.path.join(out_dir, f)
            for f in os.listdir(out_dir)
            if os.path.isfile(os.path.join(out_dir, f))
            and os.path.join(out_dir, f) not in already_used
            and (entry_id in f if entry_id else True)
        ]
        if not candidates:
            return None
        return max(candidates, key=os.path.getmtime)

    # -- مسیر یوتیوب -----------------------------------------------------

    def _probe_duration(self, url: str, out_dir: str) -> int:
        """
        فقط مدت‌زمان ویدیو رو (بدون دانلود واقعی) می‌گیره. این یه استخراج جدا و
        سبکه تا با فرآیند دانلود اصلی تداخل نکنه (فراخوانی process_ie_result
        بیشتر از یک‌بار روی همون info می‌تونه باعث خطای انتخاب فرمت بشه).
        """
        opts = self._base_opts(out_dir)
        opts["noplaylist"] = True
        opts["skip_download"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info.get("duration") or 0
        except Exception as e:
            logger.warning(f"گرفتن مدت‌زمان ویدیو ناموفق بود (نادیده گرفته می‌شه): {e}")
            return 0

    def _download_youtube(self, url: str, out_dir: str, mode: str = "video", quality: str = "best") -> dict:
        """
        mode: "video" یا "audio"
        quality: "best" یا یکی از کلیدهای QUALITY_HEIGHTS ("1080","720","480","360") - فقط برای mode=video
        """
        duration = self._probe_duration(url, out_dir)
        if duration and duration > Config.MAX_DURATION_SECONDS:
            raise FileTooLargeError(
                f"مدت‌زمان ویدیو ({duration // 60} دقیقه) بیشتر از حد مجازه."
            )

        last_error: Exception | None = None
        saw_sign_in_required = False
        attempt_summaries: list[str] = []

        # پاس اول بدون کوکی (یه باگ شناخته‌شده‌ی تازه‌ی yt-dlp هست که پاس‌دادن کوکی
        # به ویدیوهای عمومی گاهی باعث خطای "The page needs to be reloaded" می‌شه)،
        # پاس دوم با کوکی - فقط اگه کوکی تنظیم شده باشه و پاس اول شکست بخوره.
        cookie_passes = [False, True] if self._cookies_path else [False]

        for use_cookies in cookie_passes:
            for client in YOUTUBE_CLIENT_CHAIN:
                opts = self._base_opts(out_dir, use_cookies=use_cookies)
                opts["noplaylist"] = True
                if client:
                    opts["extractor_args"] = {"youtube": {"player_client": [client]}}
                # اگه client مقدار None باشه، هیچ extractor_args ست نمی‌شه و yt-dlp
                # با منطق پیش‌فرض خودش (معمولاً ترکیبی از چند کلاینت) عمل می‌کنه.

                if mode == "audio":
                    opts["format"] = "ba/b"
                    opts["postprocessors"] = [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": AUDIO_CODEC,
                        "preferredquality": AUDIO_QUALITY_KBPS,
                    }]
                else:
                    if quality == "best" or quality not in QUALITY_HEIGHTS:
                        opts["format"] = VIDEO_FORMAT_SELECTOR
                    else:
                        h = QUALITY_HEIGHTS[quality]
                        opts["format"] = (
                            f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/best"
                        )
                    opts["merge_output_format"] = "mp4"

                tag = f"{client or 'auto'}{'+cookies' if use_cookies else ''}"

                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        # یک تک‌فراخوانی extract_info با download=True: هم استخراج و هم
                        # دانلود واقعی رو انجام می‌ده. فراخوانی جدای process_ie_result
                        # بعد از این باعث خطای کاذب "Requested format is not available" می‌شد.
                        info = ydl.extract_info(url, download=True)
                        filepath = ydl.prepare_filename(info)
                        expected_ext = f".{AUDIO_CODEC}" if mode == "audio" else ".mp4"

                        if not os.path.exists(filepath):
                            base, _ = os.path.splitext(filepath)
                            candidate = base + expected_ext
                            filepath = candidate if os.path.exists(candidate) else self._find_entry_file(
                                out_dir, "", set()
                            )

                        if not filepath or not os.path.exists(filepath):
                            raise UnavailableError("فایل خروجی بعد از دانلود پیدا نشد.")

                        return {
                            "title": info.get("title") or "video",
                            "items": [{"path": filepath, "type": "audio" if mode == "audio" else "video"}],
                        }

                except FileTooLargeError:
                    raise

                except yt_dlp.utils.DownloadError as e:
                    msg = str(e)
                    last_error = e

                    if "Sign in to confirm" in msg or "not a bot" in msg:
                        saw_sign_in_required = True
                        attempt_summaries.append(f"{tag}: نیاز به ورود/کوکی")
                        logger.warning(f"[youtube:{tag}] بلاک شد (نیاز به کوکی): {msg[:200]}")
                        continue

                    if any(k in msg for k in ["Private video", "This video is unavailable",
                                               "has been removed", "not available in your country",
                                               "content isn't available", "age-restricted"]):
                        raise UnavailableError(msg) from e

                    if "Requested format is not available" in msg and mode == "video" and quality != "best":
                        # کیفیت درخواستی موجود نیست؛ به‌جای شکست کامل، بهترین کیفیت موجود رو می‌گیریم
                        attempt_summaries.append(f"{tag}: کیفیت {quality} موجود نبود")
                        logger.warning(f"[youtube:{tag}] کیفیت {quality} موجود نبود، best امتحان می‌شه.")
                        quality = "best"
                        continue

                    attempt_summaries.append(f"{tag}: {msg[:150]}")
                    logger.warning(f"[youtube:{tag}] شکست خورد، کلاینت بعدی: {msg[:200]}")
                    continue

                except Exception as e:
                    last_error = e
                    attempt_summaries.append(f"{tag}: {str(e)[:150]}")
                    logger.warning(f"[youtube:{tag}] خطای غیرمنتظره: {e}")
                    continue

        final_msg = str(last_error) if last_error else "دلیل نامشخص"
        if saw_sign_in_required or "Sign in to confirm" in final_msg or "not a bot" in final_msg:
            raise BotCheckError(final_msg)

        detail = " | ".join(attempt_summaries) if attempt_summaries else final_msg
        raise DownloaderError(detail)

    # -- مسیر اینستاگرام ---------------------------------------------

    def _download_instagram(self, url: str, out_dir: str) -> dict:
        """
        پست تکی (عکس/ویدیو/ریل) و پست چرخشی (carousel با چند عکس/ویدیو) رو
        پشتیبانی می‌کنه. برای هر دو حالت عمومی و خصوصی (با کوکی) کار می‌کنه.
        """
        opts = self._base_opts(out_dir)
        opts["format"] = f"{VIDEO_FORMAT_SELECTOR}/best"
        opts["merge_output_format"] = "mp4"
        # False می‌ذاریم چون پست چرخشی اینستاگرام توی yt-dlp به‌صورت چند "entry" مدل می‌شه
        opts["noplaylist"] = False
        opts["playlist_items"] = f"1-{MAX_CAROUSEL_ITEMS}"

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as e:
            msg = str(e)

            if any(k in msg for k in ["Login required", "Requested content is not available",
                                       "rate-limit", "wait a few minutes", "checkpoint_required"]):
                raise BotCheckError(msg) from e

            if any(k in msg for k in ["Private", "does not exist", "not found",
                                       "unable to fetch", "no longer available"]):
                raise UnavailableError(msg) from e

            raise DownloaderError(msg) from e

        entries = info.get("entries")
        raw_entries = list(entries) if entries is not None else [info]
        raw_entries = [e for e in raw_entries if e]

        if not raw_entries:
            raise UnavailableError("هیچ رسانه‌ای در این پست پیدا نشد.")

        items = []
        used_paths: set = set()

        for entry in raw_entries[:MAX_CAROUSEL_ITEMS]:
            entry_id = str(entry.get("id") or "")
            filepath = self._find_entry_file(out_dir, entry_id, used_paths)

            if not filepath or not os.path.exists(filepath):
                continue

            used_paths.add(filepath)
            items.append({"path": filepath, "type": self._media_type_for(filepath)})

        if not items:
            raise UnavailableError("دانلود رسانه‌های این پست ناموفق بود.")

        title = info.get("title") or info.get("description") or "Instagram post"
        return {"title": title.strip()[:200] if title else "Instagram post", "items": items}

    # -- نقطه‌ی ورود عمومی ---------------------------------------------

    def download(self, url: str, out_dir: str, mode: str = "video", quality: str = "best") -> dict:
        """
        لینک رو دانلود می‌کنه و برمی‌گردونه:
        {"title": str, "items": [{"path": str, "type": "video"|"photo"|"audio"}, ...]}
        برای یوتیوب همیشه یک آیتم؛ برای اینستاگرام ممکنه چند آیتم باشه (پست چرخشی).
        mode/quality فقط برای یوتیوب معنا دارن (اینستاگرام همیشه بهترین کیفیت موجود).
        """
        if is_youtube(url):
            return self._download_youtube(url, out_dir, mode=mode, quality=quality)
        elif is_instagram(url):
            return self._download_instagram(url, out_dir)
        else:
            raise DownloaderError("این لینک پشتیبانی نمی‌شه. فقط یوتیوب و اینستاگرام.")
