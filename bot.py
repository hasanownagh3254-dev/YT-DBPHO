import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message
from yt_dlp import YoutubeDL


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("youtube-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
PORT = int(os.environ.get("PORT", "10000"))

# Decimal bytes, leaving room below Telegram's upload limit.
MAX_BYTES = 49_000_000
TARGET_BYTES = 47_000_000

MAX_DURATION = 180
MAX_DOWNLOAD_BYTES = 800_000_000
PROCESS_TIMEOUT = 7200

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)

ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}

dp = Dispatcher()
job_lock = asyncio.Lock()


class UserError(Exception):
    pass


def authorized(message: Message) -> bool:
    return (
        message.from_user is not None
        and message.from_user.id == OWNER_ID
        and message.chat.type == "private"
    )


def extract_url(text: str):
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,،؛)")
        parsed = urlsplit(url)

        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in ALLOWED_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 80, 443}
        ):
            return url

    return None


def run_command(command, timeout=PROCESS_TIMEOUT):
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise UserError(
            "زمان پردازش بیش از حد شد. سرور رایگان ممکن است "
            "برای فشرده‌سازی این فایل توان کافی نداشته باشد."
        ) from exc

    if result.returncode != 0:
        logger.error("Command failed: %s", result.stderr[-3000:])
        raise UserError(
            "پردازش فایل با FFmpeg ناموفق بود؛ گزارش Render را بررسی کن."
        )

    return result.stdout


def probe(path: Path):
    raw = run_command(
        [
            "ffprobe",
            "-v", "error",
            "-show_streams",
            "-show_format",
            "-of", "json",
            str(path),
        ],
        timeout=60,
    )

    data = json.loads(raw)
    video = next(
        (
            stream
            for stream in data.get("streams", [])
            if stream.get("codec_type") == "video"
        ),
        None,
    )

    if video is None:
        raise UserError("فایل دریافت‌شده ویدیو ندارد.")

    duration = float(data.get("format", {}).get("duration") or 0)
    if duration <= 0:
        duration = float(video.get("duration") or 0)

    if duration <= 0:
        raise UserError("مدت ویدیو قابل تشخیص نیست.")

    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "duration": duration,
        "codec": video.get("codec_name", "unknown"),
    }


def download_video(url: str, directory: Path):
    def download_guard(progress):
        total = (
            progress.get("total_bytes")
            or progress.get("total_bytes_estimate")
            or 0
        )
        downloaded = progress.get("downloaded_bytes") or 0

        if max(total, downloaded) > MAX_DOWNLOAD_BYTES:
            raise UserError(
                "حجم دانلود برای این سرور بیش از حد تعیین‌شده است."
            )

    options = {
        "format": "bestvideo+bestaudio/best",
        "format_sort": ["res", "fps"],
        "js_runtimes": {"node": {}},
        "outtmpl": str(directory / "source.%(ext)s"),
        "merge_output_format": "mkv",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 1,
        "progress_hooks": [download_guard],
        "max_filesize": MAX_DOWNLOAD_BYTES,
    }

    # Optional Render secret file. Never commit cookies to GitHub.
    cookies_file = os.environ.get("YOUTUBE_COOKIES_FILE", "").strip()
    if cookies_file:
        if not Path(cookies_file).is_file():
            raise UserError("فایل کوکی تنظیم شده است ولی پیدا نشد.")
        options["cookiefile"] = cookies_file

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

        if not info or info.get("_type") in {"playlist", "multi_video"}:
            raise UserError("فقط لینک یک ویدیو را بفرست، نه پلی‌لیست.")

        if info.get("is_live"):
            raise UserError("پخش زنده پشتیبانی نمی‌شود.")

        duration = info.get("duration")
        if not duration or duration > MAX_DURATION:
            raise UserError(
                "این ربات فقط ویدیوهای حداکثر سه‌دقیقه‌ای را می‌پذیرد."
            )

        result = ydl.extract_info(url, download=True)

    candidates = [
        path for path in directory.glob("source.*")
        if path.suffix.lower() in {".mkv", ".mp4", ".webm", ".mov"}
    ]

    if len(candidates) != 1:
        raise UserError("فایل نهایی دانلود به‌درستی آماده نشد.")

    return result, candidates[0]


def compress_video(source: Path, directory: Path, metadata: dict):
    duration = metadata["duration"]
    audio_bitrate = 128_000

    # Budget includes audio and a margin for container overhead.
    total_bitrate = int(TARGET_BYTES * 8 / duration)
    video_bitrate = int(total_bitrate * 0.96) - audio_bitrate

    if video_bitrate <= 0:
        raise UserError("بودجه حجم برای این ویدیو کافی نیست.")

    output = directory / "compressed.mp4"

    # Always encode from the original, not from a previous compressed copy.
    for attempt in range(3):
        output.unlink(missing_ok=True)

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(source),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-map_metadata", "-1",
            "-map_chapters", "-1",
            "-c:v", "libx265",
            "-preset", "fast",
            "-threads", "1",
            "-x265-params",
            "pools=none:frame-threads=1:log-level=error",
            "-b:v", str(video_bitrate),
            "-pix_fmt", "yuv420p",
            "-tag:v", "hvc1",
            "-c:a", "aac",
            "-b:a", str(audio_bitrate),
            "-movflags", "+faststart",
            str(output),
        ]

        run_command(command)

        if not output.exists() or output.stat().st_size == 0:
            raise UserError("فایل فشرده‌شده ساخته نشد.")

        actual_size = output.stat().st_size

        if actual_size <= MAX_BYTES:
            encoded = probe(output)

            # No resolution reduction is allowed.
            if (
                encoded["width"] != metadata["width"]
                or encoded["height"] != metadata["height"]
            ):
                raise UserError(
                    "ابعاد خروجی با ورودی یکسان نیست؛ ارسال متوقف شد."
                )

            return output

        video_bitrate = int(
            video_bitrate * (TARGET_BYTES / actual_size) * 0.92
        )

    raise UserError(
        "فایل پس از تلاش‌های فشرده‌سازی هنوز بزرگ‌تر از سقف ارسال است."
    )


async def update_status(message: Message, text: str):
    try:
        await message.edit_text(text)
    except Exception:
        logger.warning("Could not update status message.")


@dp.message(CommandStart())
async def start_handler(message: Message):
    if not authorized(message):
        return

    await message.answer(
        "لینک یوتیوب شورت را بفرست.\n\n"
        "بهترین کیفیت موجود دانلود می‌شود.\n"
        "اگر حجم بزرگ باشد، با حفظ ابعاد تصویر فشرده می‌شود.\n"
        "خروجی به‌صورت فایل ارسال می‌شود.\n\n"
        "فقط ویدیوهای حداکثر سه‌دقیقه‌ای پذیرفته می‌شوند."
    )


@dp.message(F.text)
async def video_handler(message: Message):
    if not authorized(message):
        return

    url = extract_url(message.text or "")

    if not url:
        await message.answer(
            "یک لینک معتبر با https:// از youtube.com یا youtu.be بفرست."
        )
        return

    if job_lock.locked():
        await message.answer(
            "یک ویدیو در حال پردازش است؛ بعد از پایان دوباره لینک بده."
        )
        return

    async with job_lock:
        status = await message.answer(
            "در حال بررسی و دانلود بهترین کیفیت موجود…"
        )

        try:
            with tempfile.TemporaryDirectory(prefix="youtube-") as temp:
                directory = Path(temp)

                info, source = await asyncio.to_thread(
                    download_video, url, directory
                )

                metadata = await asyncio.to_thread(probe, source)

                if metadata["duration"] > MAX_DURATION + 1:
                    raise UserError(
                        "مدت فایل بیشتر از سه دقیقه است."
                    )

                compressed = False
                output = source

                if source.stat().st_size > MAX_BYTES:
                    await update_status(
                        status,
                        "در حال فشرده‌سازی با حفظ ابعاد تصویر…\n"
                        "روی سرور رایگان ممکن است زمان زیادی طول بکشد.",
                    )

                    output = await asyncio.to_thread(
                        compress_video, source, directory, metadata
                    )
                    compressed = True

                title = str(info.get("title") or "YouTube Short")[:500]
                size_mb = output.stat().st_size / 1_000_000

                caption = (
                    f"{title}\n\n"
                    f"ابعاد: {metadata['width']} × {metadata['height']}\n"
                    f"حجم: {size_mb:.2f} MB\n"
                    f"وضعیت: "
                    f"{'فشرده‌شده با H.265' if compressed else 'بدون بازفشرده‌سازی'}"
                )

                await update_status(status, "در حال ارسال فایل…")

                await message.answer_document(
                    document=FSInputFile(
                        output,
                        filename=f"short{output.suffix}",
                    ),
                    caption=caption,
                    request_timeout=600,
                )

                await update_status(status, "فایل ارسال شد.")

        except UserError as exc:
            await update_status(status, str(exc))

        except Exception:
            logger.exception("Video job failed")
            await update_status(
                status,
                "دانلود یا ارسال ناموفق بود.\n"
                "ممکن است یوتیوب دسترسی سرور را محدود کرده باشد، "
                "ویدیو نیاز به ورود داشته باشد، یا منابع سرور کافی نباشد.\n"
                "جزئیات خطا در Logs سرویس Render ثبت شده است.",
            )


async def health_handler(request):
    return web.json_response(
        {"status": "ok", "busy": job_lock.locked()}
    )


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required.")

    if OWNER_ID <= 0:
        raise RuntimeError(
            "OWNER_ID must be your positive numeric Telegram user ID."
        )

    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    session = AiohttpSession(timeout=600)
    bot = Bot(token=BOT_TOKEN, session=session)

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        await bot.session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
