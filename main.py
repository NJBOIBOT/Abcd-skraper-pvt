import os
import time
import math
import shutil
import logging
import asyncio
import threading
import subprocess
from urllib.parse import urlparse
import yt_dlp
import static_ffmpeg
from flask import Flask
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from dotenv import load_dotenv

# Initialize static ffmpeg binaries so ffmpeg and ffprobe are available in PATH
try:
    static_ffmpeg.add_paths()
except Exception as e:
    logging.warning(f"Failed to add static_ffmpeg paths: {e}")

# Try importing hachoir metadata parser for video metadata (width, height, duration)
try:
    from hachoir.metadata import extractMetadata
    from hachoir.parser import createParser
    HACHOIR_AVAILABLE = True
except ImportError:
    HACHOIR_AVAILABLE = False

# Load environment variables
load_dotenv()

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("VideoDownloaderBot")

# Telegram API Configuration
API_ID_ENV = os.getenv("API_ID")
API_HASH_ENV = os.getenv("API_HASH")
BOT_TOKEN_ENV = os.getenv("BOT_TOKEN")

if not API_ID_ENV or not API_HASH_ENV or not BOT_TOKEN_ENV:
    logger.warning("API_ID, API_HASH, or BOT_TOKEN missing in environment variables. Ensure they are set in production.")

API_ID = int(API_ID_ENV) if API_ID_ENV and API_ID_ENV.isdigit() else 0
API_HASH = API_HASH_ENV or ""
BOT_TOKEN = BOT_TOKEN_ENV or ""
PORT = int(os.getenv("PORT", 8080))

# Temporary directory for downloads
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Active Tasks Tracking
# Structure: task_id -> {"url": str, "title": str, "cancelled": bool, "status": str, "file_path": str, "msg": Message}
active_tasks = {}

# Start Pyrogram Client with parse_mode set to HTML by default
app = Client(
    "video_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    parse_mode=ParseMode.HTML
)

# ==================== Helper Functions ====================

def humanbytes(size):
    """Convert bytes to human readable string."""
    if not size:
        return "0 B"
    size = float(size)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def TimeFormatter(seconds: int) -> str:
    """Format seconds into HH:MM:SS."""
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def generate_progress_bar(percent: float) -> str:
    """Generate a visual progress bar string."""
    completed = int(percent / 10)
    remaining = 10 - completed
    return "█" * completed + "░" * remaining


async def progress_callback(current, total, message: Message, task_id: str, action: str, last_update_time: list):
    """Progress callback for Pyrogram upload/download updates."""
    if task_id not in active_tasks or active_tasks[task_id].get("cancelled"):
        raise Exception("Task Cancelled by User")

    now = time.time()
    # Throttle updates to avoid Telegram flood limits (update every 3 seconds)
    if now - last_update_time[0] < 3 and current < total:
        return

    last_update_time[0] = now
    percentage = (current / total) * 100 if total else 0
    progress_bar = generate_progress_bar(percentage)

    text = (
        f"<b>{action}...</b>\n"
        f"<code>[{progress_bar}]</code> {percentage:.1f}%\n"
        f"<b>Downloaded:</b> {humanbytes(current)} / {humanbytes(total)}\n"
    )

    cancel_btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel Task", callback_data=f"cancel|{task_id}")]
    ])

    try:
        await message.edit_text(text, reply_markup=cancel_btn, parse_mode=ParseMode.HTML)
    except Exception:
        pass


def get_video_metadata(file_path):
    """Extract width, height, and duration from video file using hachoir."""
    width, height, duration = 0, 0, 0
    if HACHOIR_AVAILABLE:
        try:
            parser = createParser(file_path)
            if parser:
                with parser:
                    metadata = extractMetadata(parser)
                    if metadata:
                        if metadata.has("duration"):
                            duration = metadata.get("duration").seconds
                        if metadata.has("width"):
                            width = metadata.get("width")
                        if metadata.has("height"):
                            height = metadata.get("height")
        except Exception as e:
            logger.warning(f"Failed to extract video metadata: {e}")
    return width, height, duration


def generate_thumbnail(video_path, output_thumb_path):
    """Generate video thumbnail using ffmpeg."""
    try:
        cmd = [
            'ffmpeg', '-y', '-ss', '00:00:01', '-i', video_path,
            '-vframes', '1', '-q:v', '2', output_thumb_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if os.path.exists(output_thumb_path):
            return output_thumb_path
    except Exception as e:
        logger.warning(f"Thumbnail generation failed: {e}")
    return None


def get_ytdl_opts(custom_headers=None):
    """Return standard yt-dlp options."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'concurrent_fragment_downloads': 4,
        'nocheckcertificate': True,
        'ignoreerrors': True,
        'writethumbnail': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }
    if custom_headers:
        opts['http_headers'] = custom_headers
    return opts


# ==================== Telegram Commands ====================

@app.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    welcome_text = (
        "<b>👋 Welcome to Video Downloader Bot!</b>\n\n"
        "I can download videos from almost any website in high quality (up to 2GB)!\n\n"
        "<b>Supported sites include:</b>\n"
        "• YouTube, Instagram, TikTok, Twitter/X, Facebook, Reddit, Pinterest\n"
        "• Porntrex, Sxyprn, Vimeo, Dailymotion, and 1000+ more sites!\n\n"
        "<b>How to use:</b>\n"
        "Just send or forward any video URL directly in this chat!"
    )
    await message.reply_text(welcome_text, parse_mode=ParseMode.HTML)


@app.on_message(filters.command("help") & filters.private)
async def help_command(client: Client, message: Message):
    help_text = (
        "<b>📖 How to Download Videos:</b>\n\n"
        "1. Send a direct URL to a video page.\n"
        "2. Choose your preferred resolution/quality from the buttons.\n"
        "3. The bot will download and upload the video directly to Telegram.\n"
        "4. Click <b>❌ Cancel Task</b> anytime to stop the process."
    )
    await message.reply_text(help_text, parse_mode=ParseMode.HTML)


# ==================== Link Processing ====================

@app.on_message(filters.private & filters.text & ~filters.command(["start", "help"]))
async def link_handler(client: Client, message: Message):
    url = message.text.strip()

    if not (url.startswith("http://") or url.startswith("https://")):
        await message.reply_text("⚠️ Please send a valid video URL starting with http:// or https://")
        return

    status_msg = await message.reply_text("🔍 <i>Extracting video information... Please wait.</i>", parse_mode=ParseMode.HTML)
    task_id = f"{message.chat.id}_{message.id}"

    # Extract info via yt-dlp in executor thread
    loop = asyncio.get_event_loop()

    def extract_info():
        ydl_opts = get_ytdl_opts()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await loop.run_in_executor(None, extract_info)
    except Exception as e:
        logger.error(f"Error extracting info: {e}")
        info = None

    if not info:
        await status_msg.edit_text("❌ Failed to extract video information or unsupported URL.")
        return

    # Handle playlist/multi-video if needed
    if 'entries' in info and info['entries']:
        info = info['entries'][0]

    title = info.get('title', 'Video')
    duration = info.get('duration', 0)
    formats = info.get('formats', [])

    active_tasks[task_id] = {
        "url": url,
        "title": title,
        "duration": duration,
        "cancelled": False,
        "status": "selecting",
        "msg": status_msg
    }

    # Filter and categorize available qualities
    buttons = []
    seen_heights = set()

    # Sort formats by resolution / height
    valid_formats = []
    for f in formats:
        height = f.get('height')
        if height and height not in seen_heights and f.get('vcodec') != 'none':
            seen_heights.add(height)
            valid_formats.append((height, f.get('format_id'), f.get('ext', 'mp4')))

    valid_formats.sort(key=lambda x: x[0], reverse=True)

    # Build Quality Selection Buttons
    row = []
    for height, fmt_id, ext in valid_formats[:6]: # Show top resolutions
        btn_text = f"🎬 {height}p"
        row.append(InlineKeyboardButton(btn_text, callback_data=f"dl|{task_id}|{height}p|{fmt_id}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Always add Best Original Quality & Audio MP3 options
    buttons.append([
        InlineKeyboardButton("⭐ Original Best Quality", callback_data=f"dl|{task_id}|best|best"),
        InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"dl|{task_id}|audio|bestaudio")
    ])
    buttons.append([
        InlineKeyboardButton("❌ Cancel Task", callback_data=f"cancel|{task_id}")
    ])

    markup = InlineKeyboardMarkup(buttons)
    dur_str = TimeFormatter(int(duration)) if duration else "Unknown"

    text = (
        f"<b>🎬 {title}</b>\n"
        f"<b>⏱ Duration:</b> {dur_str}\n\n"
        f"<i>Select your preferred download quality:</i>"
    )

    await status_msg.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


# ==================== Callback Query Handler ====================

@app.on_callback_query()
async def callback_handler(client: Client, callback: CallbackQuery):
    data = callback.data.split("|")
    action = data[0]

    if action == "cancel":
        task_id = data[1]
        if task_id in active_tasks:
            active_tasks[task_id]["cancelled"] = True
            file_path = active_tasks[task_id].get("file_path")
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass

        await callback.answer("Task Cancelled!", show_alert=True)
        try:
            await callback.message.edit_text("❌ <b>Task was cancelled by user.</b>", parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return

    if action == "dl":
        task_id = data[1]
        quality_label = data[2]
        fmt_id = data[3]

        if task_id not in active_tasks:
            await callback.answer("Task expired or unavailable. Please send link again.", show_alert=True)
            return

        task = active_tasks[task_id]
        if task.get("cancelled"):
            await callback.answer("Task was cancelled.", show_alert=True)
            return

        task["status"] = "downloading"
        url = task["url"]
        title = task["title"]
        duration = task.get("duration", 0)

        await callback.answer("Starting download...")
        await callback.message.edit_text("⏳ <i>Starting video download... Please wait.</i>", parse_mode=ParseMode.HTML)

        # Prepare yt-dlp output template
        timestamp = int(time.time())
        out_filename = f"{task_id}_{timestamp}"
        out_template = os.path.join(DOWNLOAD_DIR, f"{out_filename}.%(ext)s")

        loop = asyncio.get_event_loop()
        last_update_time = [0]

        def ytdl_hook(d):
            if task.get("cancelled"):
                raise Exception("Task Cancelled by User")
            if d['status'] == 'downloading':
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                if total > 0:
                    asyncio.run_coroutine_threadsafe(
                        progress_callback(downloaded, total, callback.message, task_id, "Downloading Video", last_update_time),
                        loop
                    )

        # Configure yt-dlp download options
        ydl_opts = get_ytdl_opts()
        ydl_opts['outtmpl'] = out_template
        ydl_opts['progress_hooks'] = [ytdl_hook]

        if quality_label == "audio":
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        elif quality_label == "best":
            # Select best merged video+audio OR best single file containing both video+audio
            ydl_opts['format'] = 'bestvideo+bestaudio/bestvideo*+bestaudio*/best'
        else:
            ydl_opts['format'] = f"bestvideo[height<={quality_label.replace('p','')}]+bestaudio/best[height<={quality_label.replace('p','')}]/best"

        def run_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info_dict = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info_dict)
                if quality_label == "audio":
                    filename = os.path.splitext(filename)[0] + ".mp3"
                return filename

        try:
            downloaded_file = await loop.run_in_executor(None, run_download)
            task["file_path"] = downloaded_file
        except Exception as e:
            if task.get("cancelled"):
                active_tasks.pop(task_id, None)
                return
            logger.error(f"Download error: {e}")
            await callback.message.edit_text(f"❌ <b>Download Failed:</b> {str(e)[:200]}", parse_mode=ParseMode.HTML)
            active_tasks.pop(task_id, None)
            return

        if task.get("cancelled"):
            active_tasks.pop(task_id, None)
            return

        if not os.path.exists(downloaded_file):
            # Check for matching files in DOWNLOAD_DIR
            matching_files = [os.path.join(DOWNLOAD_DIR, f) for f in os.listdir(DOWNLOAD_DIR) if f.startswith(out_filename) and not f.endswith(('.jpg', '.webp', '.png'))]
            if matching_files:
                downloaded_file = matching_files[0]
                task["file_path"] = downloaded_file
            else:
                await callback.message.edit_text("❌ Downloaded file not found.", parse_mode=ParseMode.HTML)
                active_tasks.pop(task_id, None)
                return

        # Check File Size (Max 2GB Telegram Bot Limit)
        file_size = os.path.getsize(downloaded_file)
        if file_size > 2 * 1024 * 1024 * 1024:
            await callback.message.edit_text("⚠️ <b>File size exceeds 2GB limit.</b> Telegram bots cannot upload files larger than 2GB.")
            if os.path.exists(downloaded_file):
                os.remove(downloaded_file)
            active_tasks.pop(task_id, None)
            return

        # Start Upload Phase
        task["status"] = "uploading"
        last_update_time = [0]
        cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Task", callback_data=f"cancel|{task_id}")]])

        await callback.message.edit_text("🚀 <i>Starting upload to Telegram...</i>", reply_markup=cancel_btn, parse_mode=ParseMode.HTML)

        # Extract video metadata and generate thumbnail
        width, height, meta_duration = get_video_metadata(downloaded_file)
        final_duration = meta_duration if meta_duration > 0 else int(duration)

        thumb_file = os.path.join(DOWNLOAD_DIR, f"{out_filename}_thumb.jpg")
        generated_thumb = generate_thumbnail(downloaded_file, thumb_file)

        try:
            if quality_label == "audio" or downloaded_file.endswith(".mp3"):
                await client.send_audio(
                    chat_id=callback.message.chat.id,
                    audio=downloaded_file,
                    caption=f"🎵 <b>{title}</b>",
                    duration=final_duration,
                    progress=progress_callback,
                    progress_args=(callback.message, task_id, "Uploading Audio", last_update_time),
                    parse_mode=ParseMode.HTML
                )
            else:
                await client.send_video(
                    chat_id=callback.message.chat.id,
                    video=downloaded_file,
                    caption=f"🎬 <b>{title}</b>\n✨ Quality: {quality_label}",
                    duration=final_duration,
                    width=width,
                    height=height,
                    thumb=generated_thumb,
                    supports_streaming=True,
                    progress=progress_callback,
                    progress_args=(callback.message, task_id, "Uploading Video", last_update_time),
                    parse_mode=ParseMode.HTML
                )

            await callback.message.delete()
        except Exception as e:
            if not task.get("cancelled"):
                logger.error(f"Upload error: {e}")
                await callback.message.edit_text(f"❌ <b>Upload Failed:</b> {str(e)[:200]}", parse_mode=ParseMode.HTML)
        finally:
            # Cleanup downloaded video and thumbnail files from disk
            if os.path.exists(downloaded_file):
                try:
                    os.remove(downloaded_file)
                except Exception:
                    pass
            if generated_thumb and os.path.exists(generated_thumb):
                try:
                    os.remove(generated_thumb)
                except Exception:
                    pass
            active_tasks.pop(task_id, None)


# ==================== Render Keep-Alive Flask Server ====================

flask_app = Flask("VideoDownloaderBot")

@flask_app.route('/')
@flask_app.route('/health')
def health_check():
    return "✅ Video Downloader Bot is running 24/7!", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)


# ==================== Main Execution ====================

if __name__ == "__main__":
    # Start Flask Web Server in a daemon thread for Render 24/7 Uptime
    threading.Thread(target=run_flask, daemon=True).start()

    logger.info(f"🚀 Bot Flask Server listening on port {PORT}")
    logger.info("🚀 Starting Telegram Video Downloader Bot...")
    app.run()
