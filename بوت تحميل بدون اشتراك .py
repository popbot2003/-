import os
import re
import json
import time
import shutil
import tempfile
import asyncio
from pathlib import Path

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

TOKEN_FILE = Path(__file__).parent / ".bot_token.json"
PLACEHOLDER = "ضع_توكن_البوت_هنا"

MAX_CONCURRENT_DOWNLOADS = 3
CACHE_TTL = 300

MAX_FILE_SIZE_MB = 45
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

PREFERRED_HEIGHTS = [720, 480, 360, 240, 144]

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
format_cache = {}


# =========================================================
# TOKEN MANAGEMENT
# =========================================================

def load_token():
    env_token = os.getenv("BOT_TOKEN")
    if env_token and env_token.strip() and env_token != PLACEHOLDER:
        return env_token.strip()

    if TOKEN_FILE.exists():
        try:
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            token = (data.get("token") or "").strip()
            if token and token != PLACEHOLDER:
                return token
        except Exception as e:
            print("⚠️ تعذر قراءة ملف التوكن:", repr(e))
    return None


def save_token(token):
    try:
        TOKEN_FILE.write_text(
            json.dumps({"token": token.strip()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"💾 تم حفظ التوكن في: {TOKEN_FILE}")
    except Exception as e:
        print("⚠️ تعذر حفظ التوكن:", repr(e))


def validate_token_format(token):
    if not token:
        return False
    return bool(re.match(r"^\d{6,}:[A-Za-z0-9_\-]{30,}$", token.strip()))


def prompt_token():
    print("\n" + "=" * 55)
    print("🔐 لم يتم العثور على توكن البوت.")
    print("=" * 55)
    print("📌 احصل على التوكن من @BotFather في تيليجرام:")
    print("   1) افتح @BotFather")
    print("   2) أرسل /mybots ثم اختر البوت")
    print("   3) اضغط API Token ثم انسخه")
    print("=" * 55)

    while True:
        try:
            token = input("👉 الصق التوكن هنا: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n❌ تم الإلغاء.")
            return None

        if not token:
            print("⚠️ التوكن فارغ، حاول مرة أخرى.\n")
            continue

        if not validate_token_format(token):
            print("⚠️ صيغة التوكن غير صحيحة.")
            print("   المتوقع مثل: 123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n")
            continue

        return token


# =========================================================
# URLS
# =========================================================

YOUTUBE_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=[\w-]+|shorts/[\w-]+)|youtu\.be/[\w-]+)[^\s]*",
    re.I,
)
TIKTOK_RE = re.compile(
    r"https?://(?:www\.)?(?:tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)/[^\s]+",
    re.I,
)
INSTAGRAM_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p|tv)/[\w\-]+[^\s]*",
    re.I,
)
FACEBOOK_RE = re.compile(
    r"https?://(?:www\.)?(?:facebook\.com|fb\.watch)/[^\s]+",
    re.I,
)


# =========================================================
# FFMPEG CHECK
# =========================================================

def _setup_ffmpeg():
    if shutil.which("ffmpeg"):
        print("✅ FFmpeg متوفر في PATH.")
        return True
    print("⚠️ FFmpeg غير متوفر! سيتم تعطيل دمج الصوت/الفيديو.")
    print("   على Ubuntu/Debian: sudo apt install -y ffmpeg")
    return False


FFMPEG_AVAILABLE = _setup_ffmpeg()


# =========================================================
# PROXY
# =========================================================

PROXY = os.getenv("YTDL_PROXY", "").strip()
if PROXY:
    print(f"🌐 سيتم استخدام الوكيل: {PROXY.split('@')[-1]}")
else:
    print("ℹ️ لا يوجد وكيل مُعرّف (YTDL_PROXY). قد تفشل بعض فيديوهات يوتيوب الجغرافية.")


# =========================================================
# YT-DLP BASE OPTS
# =========================================================

BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "retries": 3,
    "fragment_retries": 3,
    "socket_timeout": 20,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
        )
    },
}

if PROXY:
    BASE_OPTS["proxy"] = PROXY

YOUTUBE_CLIENT_OPTS = [
    {"youtube": {"player_client": ["android", "web"]}},
    {"youtube": {"player_client": ["ios", "web"]}},
    {"youtube": {"player_client": ["tv_embedded", "web"]}},
]


# =========================================================
# HELPERS
# =========================================================

def detect_url(text):
    for platform, pattern in [
        ("youtube", YOUTUBE_RE),
        ("tiktok", TIKTOK_RE),
        ("instagram", INSTAGRAM_RE),
        ("facebook", FACEBOOK_RE),
    ]:
        match = pattern.search(text or "")
        if match:
            return platform, match.group(0)
    return None, None


def format_duration(seconds):
    if not seconds:
        return "—"
    try:
        seconds = int(seconds)
    except Exception:
        return "—"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_size(size):
    if not size:
        return "—"
    try:
        size = float(size)
    except Exception:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "—"


def get_size(fmt):
    return fmt.get("filesize") or fmt.get("filesize_approx")


def platform_icon(platform):
    return {
        "youtube": "📺",
        "tiktok": "🎵",
        "instagram": "📸",
        "facebook": "📘",
    }.get(platform, "🎬")


# =========================================================
# SAFE EDIT MESSAGE
# =========================================================

async def safe_edit_message(query, text, reply_markup=None):
    message = query.message
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await message.chat.send_message(text, reply_markup=reply_markup)
    except Exception as e:
        print("send failed:", repr(e))


# =========================================================
# EXTRACT INFO (with YouTube fallbacks)
# =========================================================

def _extract_once(url, extra_args=None):
    opts = dict(BASE_OPTS)
    opts["skip_download"] = True
    if extra_args:
        opts["extractor_args"] = extra_args
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def extract_info(url):
    cached = format_cache.get(url)
    if cached and time.time() - cached["time"] < CACHE_TTL:
        return cached["data"]

    info = None
    last_error = None

    try:
        info = _extract_once(url)
    except Exception as e:
        last_error = e
        print("⚠️ default extract failed:", repr(e))

        if YOUTUBE_RE.search(url):
            for args in YOUTUBE_CLIENT_OPTS:
                try:
                    print("🔄 retry with extractor_args:", args)
                    info = _extract_once(url, extra_args=args)
                    if info:
                        break
                except Exception as e2:
                    last_error = e2
                    print("⚠️ retry failed:", repr(e2))

    if not info:
        raise last_error or Exception("extraction failed")

    formats = info.get("formats", [])
    videos = {}
    videos_only = {}
    audios = []

    for fmt in formats:
        format_id = fmt.get("format_id")
        if not format_id:
            continue

        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")
        height = int(fmt.get("height") or 0)
        size = get_size(fmt)

        has_v = vcodec and vcodec != "none"
        has_a = acodec and acodec != "none"

        if has_v and has_a and height:
            score = (int(fmt.get("tbr") or 0), size or 0)
            current = videos.get(height)
            if current is None or score > current["_score"]:
                videos[height] = {
                    "format_id": format_id,
                    "height": height,
                    "ext": fmt.get("ext") or "mp4",
                    "filesize": size,
                    "combined": True,
                    "_score": score,
                }
        elif has_v and not has_a and height:
            score = (int(fmt.get("tbr") or 0), size or 0)
            current = videos_only.get(height)
            if current is None or score > current["_score"]:
                videos_only[height] = {
                    "format_id": format_id,
                    "height": height,
                    "ext": fmt.get("ext") or "mp4",
                    "filesize": size,
                    "combined": False,
                    "_score": score,
                }
        elif has_a and not has_v:
            audios.append(fmt)

    video_list = sorted(videos.values(), key=lambda x: x["height"], reverse=True)
    video_only_list = sorted(videos_only.values(), key=lambda x: x["height"], reverse=True)
    best_audio = None
    if audios:
        best_audio = max(
            audios,
            key=lambda f: (int(f.get("abr") or 0), get_size(f) or 0),
        )

    result = {
        "title": info.get("title") or "",
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "filesize": info.get("filesize") or info.get("filesize_approx"),
        "video": video_list,
        "video_only": video_only_list,
        "best_audio": best_audio,
        "raw_info": info,
    }

    format_cache[url] = {"time": time.time(), "data": result}
    return result


# =========================================================
# SMART PICK
# =========================================================

def pick_best_video(info):
    videos = info.get("video", [])
    if videos:
        by_height = {v["height"]: v for v in videos}
        for h in PREFERRED_HEIGHTS:
            fmt = by_height.get(h)
            if not fmt:
                continue
            size = fmt.get("filesize")
            if size is None or size <= MAX_FILE_SIZE:
                return fmt
        return min(videos, key=lambda v: v.get("filesize") or float("inf"))

    if FFMPEG_AVAILABLE:
        vo = info.get("video_only", [])
        if vo:
            by_height = {v["height"]: v for v in vo}
            for h in PREFERRED_HEIGHTS:
                fmt = by_height.get(h)
                if fmt:
                    return fmt
            return vo[0]

    return None


# =========================================================
# BUILD CANDIDATES
# =========================================================

def build_video_candidates(info, picked):
    candidates = []
    videos = sorted(info.get("video", []), key=lambda x: x["height"], reverse=True)
    videos_only = sorted(info.get("video_only", []), key=lambda x: x["height"], reverse=True)

    if picked:
        if picked.get("combined"):
            candidates.append(picked["format_id"])
        else:
            audio_id = (info.get("best_audio") or {}).get("format_id")
            if audio_id:
                candidates.append(f"{picked['format_id']}+{audio_id}")
            candidates.append(picked["format_id"])

    for v in videos:
        if v["format_id"] not in candidates:
            candidates.append(v["format_id"])

    if FFMPEG_AVAILABLE:
        audio_id = (info.get("best_audio") or {}).get("format_id")
        for v in videos_only:
            if audio_id:
                sel = f"{v['format_id']}+{audio_id}"
                if sel not in candidates:
                    candidates.append(sel)
            if v["format_id"] not in candidates:
                candidates.append(v["format_id"])

    candidates.append("bv*+ba/b")
    candidates.append("bestvideo+bestaudio/best")
    candidates.append("best[ext=mp4]/best")
    candidates.append("best")

    seen = set()
    final = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            final.append(c)
    return final


def build_audio_candidates(info):
    candidates = ["bestaudio/best", "best"]
    seen = set()
    final = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            final.append(c)
    return final


# =========================================================
# CLEAN TEMP
# =========================================================

def clean_temp_dir(temp_dir):
    for p in Path(temp_dir).glob("*"):
        try:
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass


# =========================================================
# DOWNLOAD VIDEO
# =========================================================

async def download_video(url, info, picked):
    temp_dir = tempfile.mkdtemp(prefix="video_")
    candidates = build_video_candidates(info, picked)

    last_error = None
    result_info = None
    file_path = None
    used_height = None

    try:
        for selector in candidates:
            opts = dict(BASE_OPTS)
            opts.update({
                "skip_download": False,
                "format": selector,
                "outtmpl": os.path.join(temp_dir, "%(title).100s.%(ext)s"),
                "concurrent_fragment_downloads": 4,
            })
            if FFMPEG_AVAILABLE:
                opts["merge_output_format"] = "mp4"

            print(f"🎬 Trying: {selector}")

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    meta = await asyncio.to_thread(
                        ydl.extract_info, url, download=True
                    )

                files = [p for p in Path(temp_dir).glob("*") if p.is_file()]
                files = [p for p in files if not p.name.endswith(".part")]
                if not files:
                    clean_temp_dir(temp_dir)
                    continue

                file_path = max(files, key=lambda x: x.stat().st_mtime)
                result_info = meta
                used_height = meta.get("height")
                print(f"✅ Success: {selector} | {used_height}p")
                break

            except Exception as e:
                last_error = e
                print(f"❌ Failed: {selector} -> {repr(e)}")
                clean_temp_dir(temp_dir)
                continue

        if not file_path:
            raise last_error or Exception("no file")

        return str(file_path), (result_info or {}).get("title", ""), used_height

    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


# =========================================================
# DOWNLOAD AUDIO
# =========================================================

async def download_audio(url, info):
    temp_dir = tempfile.mkdtemp(prefix="audio_")
    candidates = build_audio_candidates(info)

    last_error = None
    result_info = None
    file_path = None

    try:
        for selector in candidates:
            opts = dict(BASE_OPTS)
            opts.update({
                "skip_download": False,
                "format": selector,
                "outtmpl": os.path.join(temp_dir, "%(title).100s.%(ext)s"),
                "concurrent_fragment_downloads": 4,
            })
            if FFMPEG_AVAILABLE:
                opts["postprocessors"] = [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "128",
                    }
                ]

            print(f"🎵 Trying: {selector}")

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    meta = await asyncio.to_thread(
                        ydl.extract_info, url, download=True
                    )

                files = [p for p in Path(temp_dir).glob("*") if p.is_file()]
                files = [p for p in files if not p.name.endswith(".part")]
                if not files:
                    clean_temp_dir(temp_dir)
                    continue

                file_path = max(files, key=lambda x: x.stat().st_mtime)
                result_info = meta
                print(f"✅ Success: {selector}")
                break

            except Exception as e:
                last_error = e
                print(f"❌ Failed: {selector} -> {repr(e)}")
                clean_temp_dir(temp_dir)
                continue

        if not file_path:
            raise last_error or Exception("no file")

        return str(file_path), (result_info or {}).get("title", "")

    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


# =========================================================
# PREVIEW
# =========================================================

async def send_preview(update, platform, url, info):
    duration = format_duration(info.get("duration"))

    picked = pick_best_video(info)
    if picked:
        size_text = format_size(picked.get("filesize"))
        quality_text = f"{picked['height']}p"
        if not picked.get("combined") and not FFMPEG_AVAILABLE:
            quality_text += " (بدون دمج)"
    else:
        size_text = "—"
        quality_text = "تلقائي"

    icon = platform_icon(platform)
    caption = (
        f"{icon} ⏱ {duration}\n"
        f"🎞 الجودة: {quality_text}  •  📦 {size_text}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎥 فيديو", callback_data="mode_video"),
            InlineKeyboardButton("🎵 صوت", callback_data="mode_audio"),
        ]
    ])

    thumbnail = info.get("thumbnail")
    if thumbnail:
        try:
            await update.message.reply_photo(
                photo=thumbnail, caption=caption, reply_markup=keyboard
            )
            return
        except Exception as e:
            print("Thumbnail error:", repr(e))

    await update.message.reply_text(caption, reply_markup=keyboard)


# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📥 أرسل رابط الفيديو")


# =========================================================
# MESSAGE
# =========================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    platform, url = detect_url(text)

    if not platform:
        await update.message.reply_text("❌ لم أجد رابطاً مدعوماً")
        return

    message = await update.message.reply_text("⏳ جاري التحليل...")

    try:
        info = await asyncio.to_thread(extract_info, url)

        context.user_data["url"] = url
        context.user_data["platform"] = platform
        context.user_data["info"] = info

        try:
            await message.delete()
        except Exception:
            pass

        await send_preview(update, platform, url, info)

    except Exception as e:
        print("Extraction error:", repr(e))
        try:
            err_str = str(e)
            if "not made this video available in your country" in err_str:
                msg = (
                    "❌ هذا الفيديو مقيّد جغرافياً ولا يمكن تحميله من موقع الخادم الحالي.\n"
                    "💡 الحل: إضافة وكيل (Proxy) من دولة مسموح لها عبر متغير البيئة YTDL_PROXY."
                )
            elif "Sign in to confirm" in err_str:
                msg = (
                    "❌ يوتيوب يطلب تسجيل الدخول للتحقق.\n"
                    "💡 جرّب إضافة وكيل عبر YTDL_PROXY أو استخدم كوكيز."
                )
            else:
                msg = f"❌ فشل التحليل.\nالسبب: {type(e).__name__}"
            await message.edit_text(msg)
        except Exception:
            pass


# =========================================================
# BUTTON
# =========================================================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    action = query.data
    info = context.user_data.get("info")
    url = context.user_data.get("url")

    if not info or not url:
        await safe_edit_message(query, "❌ انتهت الجلسة، أعد إرسال الرابط")
        return

    if action == "mode_video":
        picked = pick_best_video(info)
        quality_hint = f"{picked['height']}p" if picked else "تلقائي"
        await safe_edit_message(
            query,
            f"⏳ جاري التحميل بجودة {quality_hint}...\n"
            f"📦 قد يستغرق بعض الوقت حسب حجم الفيديو",
        )

        temp_file = None
        try:
            async with download_semaphore:
                temp_file, title, used_height = await download_video(
                    url, info, picked
                )

            file_size = os.path.getsize(temp_file)
            print(f"📦 Final size: {format_size(file_size)}")

            if file_size > MAX_FILE_SIZE:
                await query.message.reply_text(
                    f"⚠️ حجم الملف {format_size(file_size)} أكبر من حد تيليجرام "
                    f"({MAX_FILE_SIZE_MB}MB). جرّب جودة أقل."
                )
                return

            caption_parts = []
            if title:
                caption_parts.append(title[:700])
            if used_height:
                caption_parts.append(f"🎞 {used_height}p")
            caption = "\n".join(caption_parts) if caption_parts else None

            with open(temp_file, "rb") as video:
                await query.message.reply_video(
                    video=video,
                    caption=caption,
                    supports_streaming=True,
                    read_timeout=600,
                    write_timeout=600,
                )

        except Exception as e:
            print("Video error:", repr(e))
            try:
                await query.message.reply_text(
                    f"❌ فشل التحميل.\n{type(e).__name__}"
                )
            except Exception:
                pass
        finally:
            if temp_file:
                shutil.rmtree(
                    str(Path(temp_file).parent), ignore_errors=True
                )
        return

    if action == "mode_audio":
        await safe_edit_message(query, "⏳ جاري تحميل الصوت...")

        temp_file = None
        try:
            async with download_semaphore:
                temp_file, title = await download_audio(url, info)

            extension = Path(temp_file).suffix.lower()

            if extension == ".mp3":
                with open(temp_file, "rb") as audio:
                    await query.message.reply_audio(
                        audio=audio,
                        title=(title[:200] if title else None),
                        read_timeout=600,
                        write_timeout=600,
                    )
            else:
                with open(temp_file, "rb") as audio:
                    await query.message.reply_document(
                        document=audio,
                        caption=(title[:900] if title else None),
                        read_timeout=600,
                        write_timeout=600,
                    )

        except Exception as e:
            print("Audio error:", repr(e))
            try:
                await query.message.reply_text(
                    f"❌ فشل تحميل الصوت.\n{type(e).__name__}"
                )
            except Exception:
                pass
        finally:
            if temp_file:
                shutil.rmtree(
                    str(Path(temp_file).parent), ignore_errors=True
                )
        return


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):
    err_str = repr(context.error)
    ignore = (
        "Query is too old",
        "Message is not modified",
        "Message to delete not found",
        "message to edit not found",
    )
    if any(s in err_str for s in ignore):
        return
    print("BOT ERROR:", err_str)


# =========================================================
# MAIN
# =========================================================

def main():
    token = load_token()
    if not token:
        token = prompt_token()
        if not token:
            print("❌ لا يمكن تشغيل البوت بدون توكن.")
            return
        save_token(token)

    print("🔎 جاري التحقق من التوكن...")
    try:
        async def _check():
            b = Bot(token=token)
            async with b:
                return await b.get_me()

        me = asyncio.run(_check())
        print(f"✅ تم التحقق: @{me.username} ({me.first_name})")

    except Exception as e:
        print(f"❌ التوكن مرفوض: {e}")
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        token = prompt_token()
        if not token:
            return
        save_token(token)
        print("♻️ أعد تشغيل البرنامج.")
        return

    app = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(600)
        .write_timeout(600)
        .pool_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)

    print(f"🚀 Bot is running... (حد الملف: {MAX_FILE_SIZE_MB}MB)")
    app.run_polling(drop_pending_updates=True)


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
