import asyncio
import os
import re
import subprocess
import tempfile
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message

from yt_dlp import YoutubeDL

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))

MAX_BYTES = 49 * 1024 * 1024
YOUTUBE_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/\S+",
    re.I
)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()


def is_owner(message: Message) -> bool:
    return OWNER_ID == 0 or message.from_user.id == OWNER_ID


def download_2160p(url: str, output_dir: str):
    options = {
        "format": (
            "bestvideo[height>=2160]+bestaudio/"
            "best[height>=2160]/"
            "bestvideo*+bestaudio/best"
        ),
        "outtmpl": f"{output_dir}/original.%(ext)s",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
    }

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    files = list(Path(output_dir).glob("original.*"))

    if not files:
        raise RuntimeError("فایل ویدیو پیدا نشد.")

    return info, files[0]


def compress_video(input_path: Path, output_path: Path, crf: int):
    command = [
        "ffmpeg",
        "-y",
        "-i", str(input_path),
        "-c:v", "libx265",
        "-preset", "slow",
        "-crf", str(crf),
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-tag:v", "hvc1",
        str(output_path),
    ]

    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )


async def prepare_video(url: str, status_message: Message):
    temp_dir = tempfile.mkdtemp()

    await status_message.edit_text("در حال دریافت نسخه 2160p…")

    info, original = await asyncio.to_thread(
        download_2160p,
        url,
        temp_dir
    )

    original_size = original.stat().st_size

    if original_size <= MAX_BYTES:
        return info, original, "2160p اصلی", False

    crf_values = [26, 28, 30, 32, 34, 36]

    for crf in crf_values:
        await status_message.edit_text(
            f"فشرده‌سازی 2160p با CRF {crf}…"
        )

        compressed = Path(temp_dir) / f"compressed_{crf}.mp4"

        await asyncio.to_thread(
            compress_video,
            original,
            compressed,
            crf
        )

        if compressed.stat().st_size <= MAX_BYTES:
            original.unlink(missing_ok=True)
            return info, compressed, f"2160p فشرده CRF {crf}", True

    original.unlink(missing_ok=True)
    raise RuntimeError(
        "حتی با فشرده‌سازی H.265 هم فایل زیر 50MB نشد."
    )


@dp.message(CommandStart())
async def start_handler(message: Message):
    if not is_owner(message):
        return

    await message.answer(
        "سلام 👋\n"
        "لینک یوتیوب شورت را بفرست.\n\n"
        "ربات ابتدا 2160p را می‌گیرد.\n"
        "اگر فایل از 50MB بزرگ‌تر شد، فقط فشرده می‌کند "
        "و رزولوشن را کم نمی‌کند."
    )


@dp.message(F.text)
async def youtube_link_handler(message: Message):
    if not is_owner(message):
        return

    url = message.text.strip()

    if not YOUTUBE_RE.search(url):
        return

    status = await message.answer("در حال بررسی لینک…")

    try:
        info, file_path, quality, compressed = await prepare_video(
            url,
            status
        )

        title = info.get("title", "ویدیو")
        size_mb = file_path.stat().st_size / 1_048_576

        await message.answer_video(
            video=file_path.read_bytes(),
            caption=(
                f"<b>{title}</b>\n"
                f"کیفیت: {quality}\n"
                f"حجم: {size_mb:.1f} MB"
                + ("\nفشرده‌سازی شد." if compressed else "")
            ),
            supports_streaming=True
        )

        await status.delete()
        file_path.unlink(missing_ok=True)

    except Exception as error:
        await status.edit_text(
            f"خطا:\n<code>{error}</code>"
        )


async def health_handler(request):
    return web.Response(text="Bot is running")


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT
    )

    await site.start()


async def main():
    await start_health_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
