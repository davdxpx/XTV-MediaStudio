# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""Archive-upload handling — extract .zip / .rar / .7z / .tar batches.

When a user uploads an archive of media files, the rename-flow detours
through this module: download → probe for password protection →
(optional) prompt → extract → auto-match each media file via TMDb →
enqueue as an auto-detected batch, which flows into the normal
confirm-screen pipeline in ``upload.py``.

The password retry loop handles up to three wrong attempts before
giving up and clearing the archive.
"""

import asyncio
import contextlib
import os
import random
import shutil
import time
import uuid

from pyrogram import Client, ContinuePropagation, StopPropagation, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from db import db
from plugins.flow.sessions import (
    batch_sessions,
    batch_status_msgs,
    batch_tasks,
    format_episode_str,
)
from utils.media.archive import check_password_protected, extract_archive
from utils.media.detect import analyze_filename, auto_match_tmdb
from utils.queue_manager import queue_manager
from utils.state import clear_session, get_data, get_state, set_state, update_data
from utils.tasks import spawn as _spawn_task
from utils.telegram.log import get_logger
from utils.telegram.progress import progress_for_pyrogram

logger = get_logger("plugins.flow.archive")


async def handle_archive_upload(client, message, user_id, file_name, state):
    msg = await message.reply_text("📦 **Archive detected!**\n\nDownloading to inspect contents...")

    download_dir = Config.DOWNLOAD_DIR
    os.makedirs(download_dir, exist_ok=True)

    archive_path = os.path.join(download_dir, f"{user_id}_{message.id}_{file_name}")
    start_time = time.time()

    try:
        downloaded_path = await client.download_media(
            message,
            file_name=archive_path,
            progress=progress_for_pyrogram,
            progress_args=(
                "📥 **Downloading Archive...**",
                msg,
                start_time,
                "core"
            )
        )

        if not downloaded_path or not os.path.exists(downloaded_path):
            await msg.edit_text("❌ Failed to download archive.")
            return

        is_protected = await check_password_protected(downloaded_path)

        if is_protected:
            update_data(user_id, "archive_path", downloaded_path)
            update_data(user_id, "archive_msg_id", msg.id)
            update_data(user_id, "archive_state", state)
            set_state(user_id, "awaiting_archive_password")
            await msg.edit_text(
                "🔐 **Password Protected Archive**\n\n"
                "This archive requires a password. Please send me the password to extract it.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_rename")]]
                )
            )
            return

        await process_extracted_archive(client, user_id, downloaded_path, msg, state)

    except Exception as e:
        logger.error(f"Archive processing error: {e}")
        with contextlib.suppress(Exception):
            await msg.edit_text(f"❌ Error processing archive: {e}")


@Client.on_message(filters.text & filters.private & ~filters.regex(r"^/"), group=4)
async def handle_password_input(client, message):
    user_id = message.from_user.id
    state = get_state(user_id)

    if state == "awaiting_archive_password":
        password = message.text.strip()
        data = get_data(user_id)
        archive_path = data.get("archive_path")
        msg_id = data.get("archive_msg_id")
        orig_state = data.get("archive_state")
        attempts = int(data.get("archive_password_attempts", 0)) + 1
        max_attempts = 3

        try:
            msg = await client.get_messages(user_id, msg_id)
            await msg.edit_text("⏳ **Attempting to extract with password...**")
            extract_ok = await process_extracted_archive(
                client, user_id, archive_path, msg, orig_state, password
            )
        except Exception as e:
            logger.error(f"Error handling password: {e}")
            await message.reply_text(f"Error: {e}")
            extract_ok = False

        # Retry loop: stay in awaiting_archive_password until success or max attempts.
        if extract_ok is False and attempts < max_attempts:
            update_data(user_id, "archive_password_attempts", attempts)
            remaining = max_attempts - attempts
            with contextlib.suppress(Exception):
                await message.reply_text(
                    f"❌ **Wrong password** or archive is corrupted.\n"
                    f"You have **{remaining}** attempt(s) left — send the password again or press Cancel."
                )
            raise StopPropagation

        # Either success, or too many wrong attempts — clean up session either way.
        update_data(user_id, "archive_path", None)
        update_data(user_id, "archive_msg_id", None)
        update_data(user_id, "archive_state", None)
        update_data(user_id, "archive_password_attempts", 0)
        if extract_ok is False:
            with contextlib.suppress(Exception):
                await message.reply_text(
                    "🚫 Too many failed attempts. Cancelling this archive."
                )
            clear_session(user_id)
        else:
            set_state(user_id, orig_state)

        raise StopPropagation

    raise ContinuePropagation


async def process_extracted_archive(client, user_id, archive_path, msg, state, password=None):
    # Late import to avoid circular dep with upload.process_batch.
    from plugins.flow.upload import process_batch

    await msg.edit_text("📦 **Extracting Archive...**\n\nPlease wait.")

    extract_dir = f"{archive_path}_extracted"
    success = await extract_archive(archive_path, extract_dir, password)

    if not success:
        # Don't delete the archive here: caller may want to retry the password.
        with contextlib.suppress(Exception):
            await msg.edit_text(
                "❌ **Extraction Failed!**\n\n"
                "The archive might be corrupted or the password was incorrect. "
                "Send the password again to retry, or press Cancel."
            )
        return False

    valid_exts = [".mkv", ".mp4", ".avi", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".srt", ".ass", ".vtt", ".mp3", ".flac", ".m4a", ".wav"]
    extracted_files = []

    for root, _dirs, files in os.walk(extract_dir):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in valid_exts:
                extracted_files.append(os.path.join(root, file))

    if not extracted_files:
        await msg.edit_text("⚠️ **No media files found in archive.**\n\nSupported formats: MKV, MP4, AVI, PNG, JPG, etc.")
        if os.path.exists(archive_path):
            os.remove(archive_path)
        shutil.rmtree(extract_dir, ignore_errors=True)
        return True

    await msg.edit_text(f"✅ **Extraction Complete!**\n\nFound {len(extracted_files)} media file(s). Processing...")

    for file_path in extracted_files:
        file_name = os.path.basename(file_path)

        metadata = analyze_filename(file_name)
        lang = await db.get_preferred_language(user_id)
        tmdb_data = await auto_match_tmdb(metadata, language=lang)

        if not tmdb_data:
            from utils.tmdb.gate import is_tmdb_available
            if not is_tmdb_available():
                await client.send_message(
                    user_id,
                    f"🔒 **TMDb disabled — skipping `{file_name}`**\n\n"
                    "Auto-detection needs a TMDb API key. Re-upload this file "
                    "via `/start` to rename it in General Mode instead.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Dismiss", callback_data="cancel_rename")]]),
                )
            else:
                await client.send_message(
                    user_id,
                    f"⚠️ **Detection Failed for `{file_name}`**\nSkipping.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Dismiss", callback_data="cancel_rename")]]),
                )
            continue

        quality = metadata["quality"]
        lang = metadata.get("language", "en")

        is_series = tmdb_data and tmdb_data.get("type") == "series"
        episode = metadata.get("episode")
        season = metadata.get("season")
        if is_series:
            episode = episode or 1
            season = season or 1
        is_subtitle = metadata["is_subtitle"]

        default_dumb_channel = await db.get_default_dumb_channel(user_id)
        if tmdb_data and tmdb_data.get("type") == "movie":
            mov_ch = await db.get_movie_dumb_channel(user_id)
            if mov_ch:
                default_dumb_channel = mov_ch
        elif tmdb_data and tmdb_data.get("type") == "series":
            ser_ch = await db.get_series_dumb_channel(user_id)
            if ser_ch:
                default_dumb_channel = ser_ch

        if user_id not in batch_sessions:
            batch_id = queue_manager.create_batch()
            batch_sessions[user_id] = {"batch_id": batch_id, "items": []}
            bmsg = await client.send_message(user_id, "⏳ **Sorting Files...**\nPlease wait a moment.")
            batch_status_msgs[user_id] = bmsg

        old_task = batch_tasks.pop(user_id, None)
        if old_task:
            old_task.cancel()

        is_priority = False
        has_batch_pro = False
        if Config.PUBLIC_MODE:
            user_doc = await db.get_user(user_id)
            if user_doc and user_doc.get("is_premium"):
                plan_name = user_doc.get("premium_plan", "standard")
                config = await db.get_public_config()
                if config.get("premium_system_enabled", False):
                    plan_settings = config.get(f"premium_{plan_name}", {})
                    plan_features = plan_settings.get("features", {})
                    is_priority = plan_features.get("priority_queue", False)
                    global_toggles = await db.get_feature_toggles()
                    has_batch_pro = global_toggles.get("batch_processing_pro", True) and plan_features.get("batch_processing_pro", False)
        else:
            global_toggles = await db.get_feature_toggles()
            has_batch_pro = global_toggles.get("batch_processing_pro", True)

        batch_id = batch_sessions[user_id]["batch_id"]
        item_id = str(uuid.uuid4())

        quality_priority = {"480p": 0, "720p": 1, "1080p": 2, "2160p": 3}
        is_series = tmdb_data and tmdb_data.get("type") == "series"
        sort_key = ((0, season, episode[0] if isinstance(episode, list) else episode) if is_series else (1, quality_priority.get(quality, 4), 0))
        display_name = f"S{season:02d}{format_episode_str(episode)}" if is_series else f"{quality}"

        class DummyMessage:
            def __init__(self, original_msg):
                self.id = original_msg.id + random.randint(1000, 999999)
                self.chat = original_msg.chat
                self.from_user = original_msg.from_user
                self.document = None
                self.video = None
                self.audio = None
                self.photo = None

            async def reply_text(self, *args, **kwargs):
                kwargs.pop("quote", None)
                return await client.send_message(self.chat.id, *args, **kwargs)

            async def delete(self):
                pass

        dummy_msg = DummyMessage(msg)

        queue_manager.add_to_batch(batch_id, item_id, sort_key, display_name, dummy_msg.id, is_priority=is_priority)

        data = {
            "file_message": dummy_msg,
            "file_chat_id": dummy_msg.chat.id,
            "file_message_id": dummy_msg.id,
            "local_file_path": file_path,
            "original_name": file_name,
            "quality": quality,
            "episode": episode,
            "season": season,
            "language": lang,
            "tmdb_id": tmdb_data.get("tmdb_id") if tmdb_data else None,
            "title": tmdb_data.get("title") if tmdb_data else None,
            "year": tmdb_data.get("year") if tmdb_data else None,
            "poster": tmdb_data.get("poster") if tmdb_data else None,
            "type": tmdb_data.get("type") if tmdb_data else None,
            "is_subtitle": is_subtitle,
            "is_auto": True,
            "dumb_channel": default_dumb_channel,
            "batch_id": batch_id,
            "item_id": item_id,
            "extract_dir": extract_dir,
            "specials": metadata.get("specials", []),
            "codec": metadata.get("codec", ""),
            "audio": metadata.get("audio", ""),
            # Per-category detector output (PR B).
            "source": (metadata.get("detected_groups") or {}).get("source") or "",
            "hdr": (metadata.get("detected_groups") or {}).get("hdr") or "",
            "edition": [(metadata.get("detected_groups") or {}).get("edition")] if (metadata.get("detected_groups") or {}).get("edition") else [],
            "release": list((metadata.get("detected_groups") or {}).get("release") or []),
            "extras": list((metadata.get("detected_groups") or {}).get("extras") or []),
            "has_batch_pro": has_batch_pro,
        }

        batch_sessions[user_id]["items"].append({"message": dummy_msg, "data": data})

    if os.path.exists(archive_path):
        os.remove(archive_path)

    async def wait_and_process():
        try:
            delay = 1.0 if is_priority else (3.0 if has_batch_pro else 5.0)
            await asyncio.sleep(delay)
            if batch_tasks.get(user_id) == asyncio.current_task():
                batch_tasks.pop(user_id, None)
            await process_batch(client, user_id)
        except asyncio.CancelledError:
            pass

    if user_id in batch_sessions and batch_sessions[user_id]["items"]:
        batch_tasks[user_id] = _spawn_task(
            wait_and_process(), user_id=user_id, label="batch_wait_and_process"
        )
    else:
        shutil.rmtree(extract_dir, ignore_errors=True)

    return True
