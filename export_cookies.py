"""
اسکریپت کمکی برای گرفتن کوکی یوتیوب/اینستاگرام مستقیم از مرورگر نصب‌شده
روی سیستم خودت - بدون نیاز به افزونه‌ی مرورگر.

نحوه‌ی استفاده:
    1. با اکانت گوگل و اینستاگرام موردنظرت (ترجیحاً اکانت فرعی) توی مرورگر
       (کروم/فایرفاکس/اج) وارد youtube.com و instagram.com شو.
    2. مرورگر رو کامل ببند (این مهمه، وگرنه فایل کوکی قفله).
    3. این اسکریپت رو اجرا کن:
           pip install yt-dlp --break-system-packages
           python export_cookies.py chrome
       (به‌جای chrome می‌تونی از: firefox, edge, brave, opera هم استفاده کنی)
    4. یک فایل cookies.txt کنارش ساخته می‌شه.
    5. برای دیپلوی، مقدار base64 رو بگیر و توی COOKIES_B64 بذار:
           base64 -w0 cookies.txt      (لینوکس/مک)
           [Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.txt"))   (ویندوز)
"""

import sys

import yt_dlp


def main():
    if len(sys.argv) < 2:
        print("استفاده: python export_cookies.py <chrome|firefox|edge|brave|opera>")
        sys.exit(1)

    browser = sys.argv[1].lower()
    output_path = "cookies.txt"

    ydl_opts = {
        "cookiesfrombrowser": (browser, None, None, None),
        "quiet": True,
        "skip_download": True,
    }

    print(f"در حال خوندن کوکی از {browser}...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # با باز کردن یک YoutubeDL با cookiesfrombrowser، خودش کوکی رو از مرورگر
        # می‌خونه و در حافظه نگه می‌داره. برای نوشتنش روی دیسک به فرمت Netscape
        # از cookiejar داخلی استفاده می‌کنیم.
        ydl.cookiejar.save(output_path, ignore_discard=True, ignore_expires=True)

    print(f"✅ کوکی با موفقیت توی {output_path} ذخیره شد.")
    print("حالا این دستور رو بزن تا مقدار base64 رو بگیری:")
    print("  لینوکس/مک: base64 -w0 cookies.txt")
    print('  ویندوز (PowerShell): [Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.txt"))')


if __name__ == "__main__":
    main()
