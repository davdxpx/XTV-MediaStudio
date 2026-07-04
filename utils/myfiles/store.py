# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""Shared "save this message into MyFiles" helper.

The rename pipeline has always archived its outputs into MyFiles inside
``plugins/process.py``. Every OTHER producer of files (YouTube tool,
future tools, ad-hoc saves) had no way to do the same — their outputs
just scrolled away in the chat. This module gives the whole bot one
entry point:

    from utils.myfiles.store import save_message_to_myfiles
    doc_id = await save_message_to_myfiles(client, user_id, media_msg)

It mirrors the process.py semantics: copy into the plan's DB channel
when configured (falling back to referencing the chat message itself),
honour ``myfiles_enabled`` + auto-permanent + plan expiry, bump the
per-user quota counters, and log an activity entry.
"""

from __future__ import annotations

import contextlib
import datetime

from config import Config
from db import db
from utils.telegram.log import get_logger

logger = get_logger("utils.myfiles.store")


def _extract_media(message):
    """Return (media_obj, file_name, kind) for any media message."""
    if message is None:
        return None, None, None
    if getattr(message, "document", None):
        m = message.document
        return m, m.file_name or "file.bin", "file"
    if getattr(message, "video", None):
        m = message.video
        return m, m.file_name or f"video_{message.id}.mp4", "video"
    if getattr(message, "audio", None):
        m = message.audio
        return m, m.file_name or f"audio_{message.id}.mp3", "audio"
    if getattr(message, "photo", None):
        return message.photo, f"photo_{message.id}.jpg", "photo"
    if getattr(message, "voice", None):
        return message.voice, f"voice_{message.id}.ogg", "audio"
    if getattr(message, "video_note", None):
        return message.video_note, f"videonote_{message.id}.mp4", "video"
    return None, None, None


async def _resolve_plan(user_id: int) -> str:
    if not Config.PUBLIC_MODE:
        return "global"
    user_doc = await db.get_user(user_id)
    if user_doc and user_doc.get("is_premium"):
        return user_doc.get("premium_plan", "standard")
    return "free"


async def save_message_to_myfiles(
    client,
    user_id: int,
    message,
    *,
    file_name: str | None = None,
    media_type: str | None = None,
    folder_id=None,
    tool_name: str | None = None,
):
    """Archive ``message`` (a media message the bot can read) into the
    user's MyFiles. Returns the inserted file doc's ObjectId, or None
    when MyFiles is disabled / the message has no media / a hard error
    occurred. Never raises — callers treat this as best-effort.
    """
    try:
        media, detected_name, kind = _extract_media(message)
        if media is None:
            return None

        # Global gate (admins bypass, same rule as process.py).
        myfiles_enabled = await db.get_setting("myfiles_enabled", default=False)
        is_admin = user_id == Config.CEO_ID or user_id in Config.ADMIN_IDS
        if not myfiles_enabled and not is_admin:
            return None

        plan = await _resolve_plan(user_id)
        db_channel_id = await db.get_db_channel(plan)

        storage_channel = None
        saved_msg_id = None
        if db_channel_id:
            from pyrogram.errors import PeerIdInvalid

            try:
                db_msg = await client.copy_message(
                    chat_id=db_channel_id,
                    from_chat_id=message.chat.id,
                    message_id=message.id,
                )
            except PeerIdInvalid:
                await client.get_chat(db_channel_id)
                db_msg = await client.copy_message(
                    chat_id=db_channel_id,
                    from_chat_id=message.chat.id,
                    message_id=message.id,
                )
            storage_channel = db_channel_id
            saved_msg_id = db_msg.id
        else:
            # No DB channel: reference the chat message itself (same
            # out-of-the-box fallback the rename pipeline uses).
            storage_channel = message.chat.id
            saved_msg_id = message.id

        # Status / expiry — mirror the plan limits used by process.py.
        config = (
            await db.get_public_config()
            if Config.PUBLIC_MODE
            else await db.settings.find_one({"_id": "global_settings"})
        ) or {}
        limits = config.get("myfiles_limits", {}).get(plan, {})
        perm_limit = limits.get("permanent_limit", 50)
        expiry_days = limits.get("expiry_days", 10)

        auto_perm = True
        user_settings = await db.get_settings(user_id)
        if user_settings and "myfiles_auto_permanent" in user_settings:
            auto_perm = user_settings["myfiles_auto_permanent"]

        perm_count = await db.files.count_documents(
            {"user_id": user_id, "status": "permanent"}
        )
        status = "temporary"
        if auto_perm and (perm_limit == -1 or perm_count < perm_limit):
            status = "permanent"

        expiry_date = None
        if status == "temporary" and expiry_days != -1:
            expiry_date = datetime.datetime.utcnow() + datetime.timedelta(
                days=expiry_days
            )

        file_size = int(getattr(media, "file_size", 0) or 0)
        doc = {
            "user_id": user_id,
            "file_name": file_name or detected_name,
            "message_id": saved_msg_id,
            "channel_id": storage_channel,
            "status": status,
            "folder_id": folder_id,
            "created_at": datetime.datetime.utcnow(),
            "expires_at": expiry_date,
            "tmdb_id": None,
            "poster_url": None,
            "media_type": media_type or kind,
            "season": None,
            "episode": None,
            "file_size": file_size,
        }
        if tool_name:
            doc["source_tool"] = tool_name

        res = await db.files.insert_one(doc)

        with contextlib.suppress(Exception):
            await db.myfiles_incr_quota(
                user_id, bytes_delta=file_size, file_delta=1
            )
        with contextlib.suppress(Exception):
            await db.log_myfiles_activity(
                user_id, f"saved:{tool_name or 'chat'}", file_id=res.inserted_id
            )
        return res.inserted_id
    except Exception as e:
        logger.warning(f"save_message_to_myfiles failed for {user_id}: {e}")
        return None
