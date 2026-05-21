# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""File-upload dispatcher + batch orchestrator.

This module is the second-biggest piece of the flow package after
confirmation_screen. Two entry points:

 * ``process_batch`` drains a user's accumulated batch of uploads into
   the confirm-screen (or auto-detected) pipeline in a deterministic
   order (series by season+episode, movies alphabetically).

 * ``handle_file_upload`` is the single ``@Client.on_message`` handler
   that every document / video / photo / audio / voice in a private
   chat passes through. It dispatches by ``state`` to tool-specific
   branches (convert, audio, watermark, trim, mediainfo, voice, video
   note) or falls through to the rename pipeline (auto-detect →
   confirm screen → batch).

Nothing here changes user-visible behaviour compared to the monolithic
``plugins/flow.py`` — every state branch and handler keeps its exact
original body.
"""

import asyncio
import contextlib
import os
import re
import uuid

from pyrogram import Client, StopPropagation, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from db import db
from plugins.flow.sessions import (
    _touch_file_session,
    batch_sessions,
    batch_status_msgs,
    batch_tasks,
    file_sessions,
    format_episode_str,
)
from tools.AudioMetadataEditor import render_audio_menu
from utils.auth import check_force_sub
from utils.auth.gate import check_and_send_welcome, send_force_sub_gate
from utils.media.archive import is_archive
from utils.media.detect import analyze_filename
from utils.queue_manager import queue_manager
from utils.state import clear_session, get_data, get_state, set_state, update_data
from utils.tasks import spawn as _spawn_task
from utils.telegram.log import get_logger

logger = get_logger("plugins.flow.upload")


async def process_batch(client, user_id):
    """Drain one user's accumulated batch of uploads into the confirm /
    auto-detected pipelines in deterministic order."""
    if user_id not in batch_sessions:
        return

    batch_dict = batch_sessions.pop(user_id)
    batch = batch_dict.get("items", [])
    if not batch:
        return

    if user_id in batch_status_msgs:
        try:
            await batch_status_msgs[user_id].delete()
        except Exception:
            pass
        finally:
            del batch_status_msgs[user_id]

    def get_sort_key(item):
        data = item["data"]
        is_series = data.get("type") == "series"

        if is_series:
            ep = data.get("episode", 0)
            ep_sort = ep[0] if isinstance(ep, list) else ep
            return (0, data.get("season", 0), ep_sort)
        else:
            return (1, data.get("original_name", "").lower(), 0)

    sorted_batch = sorted(batch, key=get_sort_key)

    # Late imports to avoid circular deps (confirmation_screen imports
    # sessions, which this module also imports from).
    from plugins.flow.confirmation_screen import (
        update_auto_detected_message,
        update_confirmation_message,
    )

    for item in sorted_batch:
        message = item["message"]
        data = item["data"]
        is_auto = data.get("is_auto", False)

        msg = await message.reply_text("Processing file...", quote=True)
        file_sessions[msg.id] = data
        _touch_file_session(msg.id)

        if is_auto:
            await update_auto_detected_message(client, msg.id, user_id)
        else:
            await update_confirmation_message(client, msg.id, user_id)


@Client.on_message(
    (filters.document | filters.video | filters.photo | filters.audio | filters.voice)
    & filters.private,
    group=5,
)
async def handle_file_upload(client, message):
    user_id = message.from_user.id
    state = get_state(user_id)

    # --- Quick-mode shortcut -------------------------------------------------
    # No active session + the user's workflow preference is "quick" → jump
    # straight into the general-rename flow. Re-enters the same state
    # machine as the explicit "General Mode" button, but without making
    # the user click through Type selection first.
    if state is None:
        user_mode = await db.get_workflow_mode(user_id if Config.PUBLIC_MODE else None)
        if user_mode == "quick_mode":

            state = "awaiting_general_file"
            set_state(user_id, state)
            update_data(user_id, "type", "general")

            file_name = "unknown_file.bin"
            if message.document:
                file_name = message.document.file_name
            elif message.video:
                file_name = message.video.file_name
            elif message.audio:
                file_name = message.audio.file_name
            elif message.photo:
                file_name = f"image_{message.id}.jpg"
            if not file_name:
                file_name = "unknown_file.bin"
            update_data(user_id, "original_name", file_name)
            update_data(user_id, "file_message_id", message.id)
            update_data(user_id, "file_chat_id", message.chat.id)
            set_state(user_id, "awaiting_general_send_as")
            await message.reply_text(
                f"📄 **File Received:** `{file_name}`\n\n"
                "How would you like to receive the output?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📁 Send as Document (File)", callback_data="gen_send_as_document")],
                    [InlineKeyboardButton("▶️ Send as Media (Video/Photo/Audio)", callback_data="gen_send_as_media")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel_rename")]
                ])
            )
            return

    # Subsequent state branches live in separate append blocks below —
    # see upload.py step 2..5 commits for the rest of this handler.

    # --- awaiting_convert_file ----------------------------------------------
    if state == "awaiting_convert_file":
        if (
            not getattr(message, "photo", None)
            and not getattr(message, "video", None)
            and not getattr(message, "audio", None)
            and not getattr(message, "voice", None)
            and not getattr(message, "document", None)
        ):
            await message.reply_text("Please send an image, video, or audio file.")
            return

        file_name = "unknown_file.bin"
        file_kind = None  # "video" / "audio" / "image"

        if getattr(message, "video", None):
            file_name = message.video.file_name or "video.mp4"
            file_kind = "video"
        elif getattr(message, "audio", None):
            file_name = message.audio.file_name or "audio.mp3"
            file_kind = "audio"
        elif getattr(message, "voice", None):
            file_name = f"voice_{message.id}.ogg"
            file_kind = "audio"
        elif getattr(message, "photo", None):
            file_name = f"image_{message.id}.jpg"
            file_kind = "image"
        elif getattr(message, "document", None):
            file_name = message.document.file_name or "file.bin"
            mime = (message.document.mime_type or "").lower()
            if "video" in mime:
                file_kind = "video"
            elif "audio" in mime:
                file_kind = "audio"
            elif "image" in mime:
                file_kind = "image"
            else:
                # Fallback: sniff by extension.
                ext = os.path.splitext(file_name)[1].lower().lstrip(".")
                if ext in ("mp4", "mkv", "mov", "avi", "webm", "flv", "3gp", "ts", "m4v"):
                    file_kind = "video"
                elif ext in ("mp3", "m4a", "ogg", "opus", "flac", "wav", "wma", "aac"):
                    file_kind = "audio"
                elif ext in ("png", "jpg", "jpeg", "webp", "bmp", "tiff", "gif", "ico", "avif"):
                    file_kind = "image"

        if not file_kind:
            await message.reply_text(
                "❌ Could not determine file type.\n\n"
                "> Please send a clear **image**, **video**, or **audio** file."
            )
            return

        update_data(user_id, "original_name", file_name)
        update_data(user_id, "file_message_id", message.id)
        update_data(user_id, "file_chat_id", message.chat.id)
        update_data(user_id, "file_kind", file_kind)
        # Default audio bitrate — changeable via the Audio Bitrate submenu.
        update_data(user_id, "audio_bitrate", "192")

        # Render the new mega-edition category menu (Video/Audio/Image root).
        from tools.FileConverter import render_category_menu
        await render_category_menu(message, user_id, edit=False)
        return

    # --- awaiting_audio_thumb -----------------------------------------------
    if state == "awaiting_audio_thumb":
        if not getattr(message, "photo", None):
            await message.reply_text("Please send a photo for the cover art.")
            return

        update_data(user_id, "audio_thumb_id", message.photo.file_id)
        set_state(user_id, "awaiting_audio_menu")
        await render_audio_menu(client, message, user_id)
        raise StopPropagation

    # --- awaiting_watermark_image / _overlay --------------------------------
    if state == "awaiting_watermark_image":
        if not getattr(message, "photo", None) and not getattr(
            message, "document", None
        ):
            await message.reply_text("Please send an image.")
            return

        file_name = f"image_{message.id}.jpg"
        if getattr(message, "document", None):
            file_name = message.document.file_name or "image.jpg"
            if "image" not in (message.document.mime_type or ""):
                await message.reply_text("Please send a valid image document.")
                return

        update_data(user_id, "original_name", file_name)
        update_data(user_id, "file_message_id", message.id)
        update_data(user_id, "file_chat_id", message.chat.id)

        await message.reply_text(
            "©️ **Image Watermarker**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "> What type of watermark do you want to add?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📝 Text Watermark", callback_data="watermark_type_text"
                        ),
                        InlineKeyboardButton(
                            "🖼️ Image Watermark", callback_data="watermark_type_image"
                        ),
                    ],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel_rename")],
                ]
            ),
        )
        return

    if state == "awaiting_watermark_overlay":
        if not getattr(message, "photo", None) and not getattr(
            message, "document", None
        ):
            await message.reply_text(
                "Please send an image to use as the watermark overlay."
            )
            return

        file_id = (
            message.photo.file_id
            if getattr(message, "photo", None)
            else message.document.file_id
        )
        update_data(user_id, "watermark_content", file_id)
        set_state(user_id, "awaiting_watermark_position")

        await message.reply_text(
            "©️ **Image Watermarker**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "> Where should the watermark be placed?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("↖️ Top-Left", callback_data="wm_pos_topleft"),
                        InlineKeyboardButton("↗️ Top-Right", callback_data="wm_pos_topright"),
                    ],
                    [
                        InlineKeyboardButton("↙️ Bottom-Left", callback_data="wm_pos_bottomleft"),
                        InlineKeyboardButton("↘️ Bottom-Right", callback_data="wm_pos_bottomright"),
                    ],
                    [InlineKeyboardButton("⊹ Center", callback_data="wm_pos_center")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel_rename")],
                ]
            ),
        )
        raise StopPropagation

    # --- awaiting_audio_file ------------------------------------------------
    if state == "awaiting_audio_file":
        if (
            not getattr(message, "audio", None)
            and not getattr(message, "voice", None)
            and not getattr(message, "document", None)
        ):
            await message.reply_text("Please send an audio file.")
            return

        file_name = "audio.mp3"
        if getattr(message, "audio", None):
            file_name = message.audio.file_name or "audio.mp3"
            update_data(user_id, "audio_title", message.audio.title or "")
            update_data(user_id, "audio_artist", message.audio.performer or "")
        elif getattr(message, "document", None):
            file_name = message.document.file_name or "file.mp3"

        update_data(user_id, "original_name", file_name)
        update_data(user_id, "file_message_id", message.id)
        update_data(user_id, "file_chat_id", message.chat.id)

        set_state(user_id, "awaiting_audio_menu")
        await render_audio_menu(client, message, user_id)
        raise StopPropagation

    # === VIDEO TRIMMER STATES ===
    if state == "awaiting_trim_file":
        if not getattr(message, "video", None) and not getattr(message, "document", None):
            await message.reply_text(
                "❌ Please send a **video file** to trim.\n\n"
                "> Supported: MP4, MKV, AVI, MOV, WebM"
            )
            return

        file_name = "video.mkv"
        if getattr(message, "video", None):
            file_name = message.video.file_name or "video.mp4"
        elif getattr(message, "document", None):
            file_name = message.document.file_name or "file.bin"

        update_data(user_id, "original_name", file_name)
        update_data(user_id, "file_message_id", message.id)
        update_data(user_id, "file_chat_id", message.chat.id)
        set_state(user_id, "awaiting_trim_start")

        await message.reply_text(
            "✂️ **Video Trimmer**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"> 📄 **File:** `{file_name}`\n\n"
            "Send the **start timestamp** for the trim.\n"
            "**Format:** `HH:MM:SS` or `MM:SS`\n\n"
            "__Example:__ `00:01:30` or `1:30`",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_rename")]]
            ),
        )
        return

    if state == "awaiting_trim_start":
        if not getattr(message, "text", None):
            await message.reply_text("Please send a timestamp like `00:01:30` or `1:30`.")
            return

        from tools.VideoTrimmer import normalize_timestamp, validate_timestamp
        ts = message.text.strip()
        if not validate_timestamp(ts):
            await message.reply_text(
                "❌ Invalid timestamp format.\n\n"
                "> Use `HH:MM:SS` or `MM:SS`\n"
                "> Example: `00:01:30` or `1:30`"
            )
            return

        normalized = normalize_timestamp(ts)
        update_data(user_id, "trim_start", normalized)
        set_state(user_id, "awaiting_trim_end")

        await message.reply_text(
            "✂️ **Video Trimmer**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"> ▶️ **Start:** `{normalized}`\n\n"
            "Now send the **end timestamp** for the trim.\n"
            "**Format:** `HH:MM:SS` or `MM:SS`",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_rename")]]
            ),
        )
        return

    if state == "awaiting_trim_end":
        if not getattr(message, "text", None):
            await message.reply_text("Please send a timestamp like `00:05:00` or `5:00`.")
            return

        from tools.VideoTrimmer import normalize_timestamp, validate_timestamp
        ts = message.text.strip()
        if not validate_timestamp(ts):
            await message.reply_text(
                "❌ Invalid timestamp format.\n\n"
                "> Use `HH:MM:SS` or `MM:SS`\n"
                "> Example: `00:05:00` or `5:00`"
            )
            return

        normalized = normalize_timestamp(ts)
        update_data(user_id, "trim_end", normalized)
        session_data = get_data(user_id)

        data = {
            "type": "trim",
            "original_name": session_data.get("original_name"),
            "file_message_id": session_data.get("file_message_id"),
            "file_chat_id": session_data.get("file_chat_id"),
            "trim_start": session_data.get("trim_start"),
            "trim_end": normalized,
            "is_auto": False,
        }

        try:
            msg = await client.get_messages(
                session_data.get("file_chat_id"), session_data.get("file_message_id")
            )
            data["file_message"] = msg
            reply_msg = await client.send_message(
                user_id,
                "✂️ **Video Trimmer**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"> ▶️ **Start:** `{session_data.get('trim_start')}`\n"
                f"> ⏹️ **End:** `{normalized}`\n\n"
                "> ⏳ Trimming video..."
            )
            from plugins.process import process_file
            _spawn_task(
                process_file(client, reply_msg, data),
                user_id=user_id,
                label=f"process_file:trim:{user_id}",
                key=reply_msg.id,
            )
        except Exception as e:
            logger.error(f"Failed to get message for trim mode: {e}")
            await client.send_message(user_id, f"❌ Error: `{e}`")

        clear_session(user_id)
        return

    # === MEDIA INFO STATE ===
    if state == "awaiting_mediainfo_file":
        if (
            not getattr(message, "video", None)
            and not getattr(message, "audio", None)
            and not getattr(message, "document", None)
            and not getattr(message, "photo", None)
        ):
            await message.reply_text("Please send a media file (video, audio, image, or document).")
            return

        file_name = "unknown_file.bin"
        if getattr(message, "video", None):
            file_name = message.video.file_name or "video.mp4"
        elif getattr(message, "audio", None):
            file_name = message.audio.file_name or "audio.mp3"
        elif getattr(message, "document", None):
            file_name = message.document.file_name or "file.bin"
        elif getattr(message, "photo", None):
            file_name = f"image_{message.id}.jpg"

        status_msg = await message.reply_text(
            "ℹ️ **Media Info**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "> ⏳ Downloading and analyzing file..."
        )

        try:
            input_path = os.path.join(Config.DOWNLOAD_DIR, f"{user_id}_{message.id}_probe_input")
            downloaded = await client.download_media(message, file_name=input_path)
            if downloaded and os.path.exists(downloaded):
                from utils.media.ffmpeg_tools import probe_file
                probe_data, _ = await probe_file(downloaded)
                from tools.MediaInfo import format_media_info
                info_text = format_media_info(probe_data, file_name)
                await status_msg.edit_text(
                    info_text,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔄 Analyze Another", callback_data="media_info_menu")],
                         [InlineKeyboardButton("❌ Close", callback_data="help_close")]]
                    ),
                )
                with contextlib.suppress(Exception):
                    os.remove(downloaded)
            else:
                await status_msg.edit_text("❌ Failed to download file for analysis.")
        except Exception as e:
            logger.error(f"MediaInfo analysis failed: {e}")
            await status_msg.edit_text(f"❌ Analysis failed: `{e}`")

        clear_session(user_id)
        return

    # === VOICE NOTE CONVERTER STATE ===
    if state == "awaiting_voice_file":
        if (
            not getattr(message, "audio", None)
            and not getattr(message, "document", None)
            and not getattr(message, "voice", None)
        ):
            await message.reply_text(
                "❌ Please send an **audio file**.\n\n"
                "> Supported: MP3, FLAC, M4A, WAV, AAC, OGG"
            )
            return

        file_name = "audio.mp3"
        if getattr(message, "audio", None):
            file_name = message.audio.file_name or "audio.mp3"
        elif getattr(message, "document", None):
            file_name = message.document.file_name or "file.bin"
        elif getattr(message, "voice", None):
            file_name = "voice.ogg"

        update_data(user_id, "original_name", file_name)
        update_data(user_id, "file_message_id", message.id)
        update_data(user_id, "file_chat_id", message.chat.id)

        data = {
            "type": "voice_convert",
            "original_name": file_name,
            "file_message_id": message.id,
            "file_chat_id": message.chat.id,
            "file_message": message,
            "is_auto": False,
        }

        reply_msg = await client.send_message(
            user_id,
            "🎙️ **Voice Note Converter**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "> ⏳ Converting to OGG Opus voice note..."
        )
        from plugins.process import process_file
        _spawn_task(
            process_file(client, reply_msg, data),
            user_id=user_id,
            label=f"process_file:voice:{user_id}",
            key=reply_msg.id,
        )
        clear_session(user_id)
        return

    # === VIDEO NOTE CONVERTER STATE ===
    if state == "awaiting_videonote_file":
        if not getattr(message, "video", None) and not getattr(message, "document", None):
            await message.reply_text(
                "❌ Please send a **video file**.\n\n"
                "> The video will be cropped to a square and converted."
            )
            return

        file_name = "video.mp4"
        if getattr(message, "video", None):
            file_name = message.video.file_name or "video.mp4"
        elif getattr(message, "document", None):
            file_name = message.document.file_name or "file.bin"

        update_data(user_id, "original_name", file_name)
        update_data(user_id, "file_message_id", message.id)
        update_data(user_id, "file_chat_id", message.chat.id)

        data = {
            "type": "video_note",
            "original_name": file_name,
            "file_message_id": message.id,
            "file_chat_id": message.chat.id,
            "file_message": message,
            "is_auto": False,
        }

        reply_msg = await client.send_message(
            user_id,
            "⭕ **Video Note Converter**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "> ⏳ Cropping to square and converting..."
        )
        from plugins.process import process_file
        _spawn_task(
            process_file(client, reply_msg, data),
            user_id=user_id,
            label=f"process_file:videonote:{user_id}",
            key=reply_msg.id,
        )
        clear_session(user_id)
        return

    # --- awaiting_general_file ---------------------------------------------
    # User picked General Mode and now uploads the file.
    if state == "awaiting_general_file":
        file_name = "unknown_file.bin"
        if message.document:
            file_name = message.document.file_name
        elif message.video:
            file_name = message.video.file_name
        elif message.audio:
            file_name = message.audio.file_name
        elif message.photo:
            file_name = f"image_{message.id}.jpg"

        if not file_name:
            file_name = "unknown_file.bin"

        update_data(user_id, "original_name", file_name)
        update_data(user_id, "file_message_id", message.id)
        update_data(user_id, "file_chat_id", message.chat.id)

        set_state(user_id, "awaiting_general_send_as")
        await message.reply_text(
            f"📄 **File Received:** `{file_name}`\n\n"
            "How would you like to receive the output?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📁 Send as Document (File)",
                            callback_data="gen_send_as_document",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "▶️ Send as Media (Video/Photo/Audio)",
                            callback_data="gen_send_as_media",
                        )
                    ],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel_rename")],
                ]
            ),
        )
        return

    # -- Auth / force-sub / block-list ---------------------------------------
    if not Config.PUBLIC_MODE:
        if not (user_id == Config.CEO_ID or user_id in Config.ADMIN_IDS):
            return
    else:
        config = await db.get_public_config()
        if not await check_force_sub(client, user_id):
            await send_force_sub_gate(client, message, config)
            return

        await check_and_send_welcome(client, message, config)

    if await db.is_user_blocked(user_id):
        await message.reply_text(
            "🚫 **Access Blocked**\n\nYou have been blocked from using this bot."
        )
        return

    # -- File-size / quota / disk-space gates --------------------------------
    media = message.document or message.video or message.audio or message.photo

    file_size = getattr(media, "file_size", 0) if media else 0

    if file_size > 0:
        if file_size > 4000 * 1024 * 1024:
            await message.reply_text(
                "❌ **File Too Large**\n\nTelegram's absolute maximum file size is 4GB. This file cannot be processed."
            )
            return

        if file_size > 2000 * 1000 * 1000:
            if getattr(client, "user_bot", None) is None:
                await message.reply_text(
                    "❌ **𝕏TV Pro™ Required**\n\nThis file is larger than 2GB. The 𝕏TV Pro™ Premium Userbot must be configured to process files of this size."
                )
                return

            if Config.PUBLIC_MODE and not (user_id == Config.CEO_ID or user_id in Config.ADMIN_IDS):
                config = await db.get_public_config()
                access_setting = config.get("xtv_pro_4gb_access", "all")

                if access_setting != "all":
                    user_doc = await db.get_user(user_id)
                    is_premium = user_doc and user_doc.get("is_premium", False)
                    plan_name = user_doc.get("premium_plan", "standard") if user_doc else "standard"

                    if not is_premium:
                        await message.reply_text("❌ **Premium Required**\n\nThis file is larger than 2GB. Please upgrade to a Premium plan to process files up to 4GB.")
                        return

                    if access_setting == "premium_deluxe" and plan_name != "deluxe":
                        await message.reply_text("❌ **Premium Deluxe Required**\n\nThis file is larger than 2GB. Only Premium Deluxe users can process files up to 4GB. Please upgrade your plan.")
                        return

        quota_ok, error_msg, _ = await db.check_daily_quota(user_id, file_size)
        if not quota_ok:
            await message.reply_text(f"🛑 **Quota Exceeded**\n\n{error_msg}")
            return

        import shutil
        total, used, free = shutil.disk_usage(Config.DOWNLOAD_DIR)
        required_space = file_size * 2.5
        if free < required_space:
            required_mb = required_space / (1024 * 1024)
            free_mb = free / (1024 * 1024)
            await message.reply_text(
                f"❌ **System Error: Insufficient Disk Space**\n\n"
                f"The server does not have enough storage space to process this file.\n"
                f"Required: ~{required_mb:.2f} MB\n"
                f"Available: {free_mb:.2f} MB"
            )
            return

        await db.reserve_quota(user_id, file_size)

    # -- State guard ---------------------------------------------------------
    # If the user isn't explicitly waiting for a file upload we either
    # bounce them into auto-detection (state=None) or tell them what
    # they should be doing instead. Everything else falls through to
    # the batch accumulation block below.
    if state != "awaiting_file_upload":
        if state is None:
            from plugins.flow.confirmation_screen import handle_auto_detection
            await handle_auto_detection(client, message)
            return
        elif state == "awaiting_convert_file":
            pass
        else:
            state_labels = {
                "awaiting_type": "selecting a media type",
                "awaiting_search_movie": "searching for a movie",
                "awaiting_search_series": "searching for a series",
                "awaiting_manual_title": "entering a title manually",
                "awaiting_dumb_channel_selection": "selecting a channel",
                "awaiting_destination_selection": "selecting a destination folder",
                "awaiting_general_name": "entering a new filename",
                "awaiting_general_send_as": "choosing output format",
                "awaiting_language_custom": "entering a language code",
            }
            label = state_labels.get(state, "a different step")
            await message.reply_text(
                f"You're currently **{label}**.\n"
                "Please complete that step first, or cancel to start over.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel & Start Over", callback_data="cancel_rename")]
                ]),
                quote=True,
            )
            return

    # -- Filename + archive dispatch ----------------------------------------
    if message.photo:
        file_name = f"image_{message.id}.jpg"
    else:
        file_name = (
            message.document.file_name if message.document else message.video.file_name
        )

    if not file_name:
        file_name = "unknown.mkv"

    if is_archive(file_name):
        from plugins.flow.archive import handle_archive_upload
        await handle_archive_upload(client, message, user_id, file_name, state)
        return

    # -- Quality / episode / season pre-detection ----------------------------
    quality = "720p"
    if re.search(r"1080p", file_name, re.IGNORECASE):
        quality = "1080p"
    elif re.search(r"2160p|4k", file_name, re.IGNORECASE):
        quality = "2160p"
    elif re.search(r"480p", file_name, re.IGNORECASE):
        quality = "480p"

    episode = None
    season = None
    session_data = get_data(user_id)
    if session_data.get("type") == "series":
        episode = 1
        season = 1
        match = re.search(r"[sS](\d{1,2})[eE](\d{1,2}(?:[eE]\d{1,2})*)", file_name)
        if match:
            season = int(match.group(1))
            ep_list = [int(e) for e in re.split(r"[eE]", match.group(2)) if e]
            episode = ep_list if len(ep_list) > 1 else ep_list[0]
        else:
            match = re.search(r"[eE](\d{1,2}(?:[eE]\d{1,2})*)", file_name)
            if match:
                ep_list = [int(e) for e in re.split(r"[eE]", match.group(1)) if e]
                episode = ep_list if len(ep_list) > 1 else ep_list[0]
            else:
                match = re.search(r"(?:\s|\.|-|^)(\d{1,2})x(\d{1,2})(?:\s|\.|-|$)", file_name, re.IGNORECASE)
                if match:
                    season = int(match.group(1))
                    episode = int(match.group(2))
                else:
                    match = re.search(r"season\s*(\d+).*?episode\s*(\d+)", file_name, re.IGNORECASE)
                    if match:
                        season = int(match.group(1))
                        episode = int(match.group(2))

    lang = (
        session_data.get("language", "en") if session_data.get("is_subtitle") else None
    )

    # -- Premium flags: priority queue + batch processing pro ----------------
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
                # Check batch_processing_pro: global toggle AND per-plan
                global_toggles = await db.get_feature_toggles()
                has_batch_pro = global_toggles.get("batch_processing_pro", True) and plan_features.get("batch_processing_pro", False)
    else:
        # Private mode: check global toggle only
        global_toggles = await db.get_feature_toggles()
        has_batch_pro = global_toggles.get("batch_processing_pro", True)

    # -- Batch accumulation --------------------------------------------------
    if user_id not in batch_sessions:
        batch_id = queue_manager.create_batch()
        batch_sessions[user_id] = {"batch_id": batch_id, "items": []}
        msg = await message.reply_text(
            "⏳ **Sorting Files...**\nPlease wait a moment.", quote=True
        )
        batch_status_msgs[user_id] = msg

    old_task = batch_tasks.pop(user_id, None)
    if old_task:
        old_task.cancel()

    if user_id not in batch_sessions:
        return

    batch_id = batch_sessions[user_id]["batch_id"]
    item_id = str(uuid.uuid4())

    quality_priority = {"480p": 0, "720p": 1, "1080p": 2, "2160p": 3}

    sort_key = (
        (0, season, episode[0] if isinstance(episode, list) else episode)
        if session_data.get("type") == "series"
        else (1, quality_priority.get(quality, 4), 0)
    )
    display_name = (
        f"S{season:02d}{format_episode_str(episode)}"
        if session_data.get("type") == "series"
        else f"{quality}"
    )

    update_data(user_id, "batch_id", batch_id)

    queue_manager.add_to_batch(batch_id, item_id, sort_key, display_name, message.id, is_priority=is_priority)

    metadata = analyze_filename(file_name)
    data = {
        "file_message": message,
        "file_chat_id": message.chat.id,
        "file_message_id": message.id,
        "quality": quality,
        "episode": episode,
        "season": season,
        "original_name": file_name,
        "language": lang,
        "type": session_data.get("type"),
        "is_auto": False,
        "dumb_channel": session_data.get("dumb_channel"),
        "batch_id": batch_id,
        "item_id": item_id,
        "specials": metadata.get("specials", []),
        "codec": metadata.get("codec", ""),
        "audio": metadata.get("audio", ""),
        # Per-category detector output (PR B) — seeds the split pickers
        # with what the regex scanner saw in the filename. Users can
        # override via the confirm screen; ``process.py:_source_vars``
        # picks user input over detector output.
        "source": (metadata.get("detected_groups") or {}).get("source") or "",
        "hdr": (metadata.get("detected_groups") or {}).get("hdr") or "",
        "edition": [(metadata.get("detected_groups") or {}).get("edition")] if (metadata.get("detected_groups") or {}).get("edition") else [],
        "release": list((metadata.get("detected_groups") or {}).get("release") or []),
        "extras": list((metadata.get("detected_groups") or {}).get("extras") or []),
        "has_batch_pro": has_batch_pro,
    }
    batch_sessions[user_id]["items"].append({"message": message, "data": data})

    async def wait_and_process():
        try:
            # Batch Pro users get faster collection, priority users fastest
            delay = 1.0 if is_priority else (3.0 if has_batch_pro else 5.0)
            await asyncio.sleep(delay)
            if batch_tasks.get(user_id) == asyncio.current_task():
                batch_tasks.pop(user_id, None)
            await process_batch(client, user_id)
        except asyncio.CancelledError:
            pass

    batch_tasks[user_id] = _spawn_task(
        wait_and_process(), user_id=user_id, label="batch_wait_and_process"
    )
