# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""Shared session / timer / debounce state for the rename flow.

This module owns every dict that the confirm-screen, picker, upload,
and archive submodules read and write. Keeping it in one place means
there is exactly one ``file_sessions`` across the process — before the
split, all of this lived as module-level globals in ``plugins/flow.py``
and callers that did ``from plugins.flow import file_sessions`` now go
through ``plugins.flow.__init__``'s re-exports and end up here.

Nothing in this module registers Pyrogram handlers; it only provides
state and tiny helpers that operate on that state.
"""

import asyncio
import contextlib
import time as _time

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from utils.state import (
    clear_session,
    get_state,
    mark_for_db_persist,
    register_expire_callback,
)
from utils.tasks import spawn as _spawn_task
from utils.telegram.log import get_logger

logger = get_logger("plugins.flow.sessions")

# -- In-memory session dicts --------------------------------------------------
file_sessions: dict = {}
_file_session_timestamps: dict = {}

batch_sessions: dict = {}
batch_tasks: dict = {}
batch_status_msgs: dict = {}

# Batch-Actions hub state: one entry per user while a "apply to all files"
# menu is live. Shape: {"msg_ids": [confirm_msg_id, ...],
# "menu_msg_id": int | None, "prev_state": str | None,
# "spc_page": int, "aud_page": int, "cod_page": int, "dirty": bool}
batch_action_sessions: dict = {}

_processing_callbacks: dict = {}
_expiry_warnings: dict = {}


# -- File-session housekeeping -----------------------------------------------
def _touch_file_session(msg_id):
    """Track when a file_sessions entry was last accessed."""
    _file_session_timestamps[msg_id] = _time.time()


def cleanup_stale_file_sessions(max_age_seconds: int = 7200):
    """Remove file_sessions entries older than max_age_seconds (default 2 hours)."""
    now = _time.time()
    stale = [mid for mid, ts in _file_session_timestamps.items() if now - ts > max_age_seconds]
    for mid in stale:
        file_sessions.pop(mid, None)
        _file_session_timestamps.pop(mid, None)
    return len(stale)


def cleanup_stale_debounce_entries(max_age_seconds: int = 300):
    """Remove debounce entries older than max_age_seconds."""
    now = _time.time()
    stale_keys = [k for k, v in _processing_callbacks.items() if now - v > max_age_seconds]
    for k in stale_keys:
        _processing_callbacks.pop(k, None)
    return len(stale_keys)


def _on_session_expired(user_id):
    """Called by state.py when a session naturally expires."""
    task = _expiry_warnings.pop(user_id, None)
    if task:
        task.cancel()
    batch_sessions.pop(user_id, None)
    task = batch_tasks.pop(user_id, None)
    if task:
        task.cancel()
    batch_status_msgs.pop(user_id, None)
    batch_action_sessions.pop(user_id, None)


register_expire_callback(_on_session_expired)


# -- DB persistence ----------------------------------------------------------
async def _persist_session_to_db(user_id: int):
    """Save critical session data to DB for crash recovery."""
    from db import db as _db
    from utils.state import get_data
    data = get_data(user_id)
    if not data:
        return
    persist_data = {}
    for key in (
        "state", "type", "title", "year", "season", "episode", "quality",
        "tmdb_id", "poster", "language", "is_subtitle", "dumb_channel",
        "dest_folder", "send_as", "general_name", "original_name",
    ):
        if key in data:
            persist_data[key] = data[key]
    if persist_data:
        await _db.save_flow_session(user_id, persist_data)
        mark_for_db_persist(user_id)


async def _clear_persisted_session(user_id: int):
    """Clear persisted session from DB."""
    from db import db as _db
    await _db.clear_flow_session(user_id)


# -- Expiry warning timer ----------------------------------------------------
async def _schedule_expiry_warning(client, user_id: int, delay_seconds: int = 3300):
    """Warn user 5 minutes before session expiry, then confirm cancellation on actual expiry."""
    try:
        await asyncio.sleep(delay_seconds)
        state = get_state(user_id)
        if not state:
            _expiry_warnings.pop(user_id, None)
            return

        warning_msg = await client.send_message(
            user_id,
            "⚠️ Your renaming session will expire in **5 minutes** due to inactivity.\n"
            "Send a file or press Cancel to avoid losing your progress.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel Session", callback_data="cancel_rename")]
            ])
        )

        # Wait the remaining 5 minutes
        await asyncio.sleep(300)

        # Check if session is still active (user may have interacted)
        state = get_state(user_id)
        if not state:
            with contextlib.suppress(Exception):
                await warning_msg.edit_text(
                    "✅ Your session was already ended.",
                    reply_markup=None
                )
            _expiry_warnings.pop(user_id, None)
            return

        # Session is still active — expire it now
        clear_session(user_id)
        await _clear_persisted_session(user_id)
        _expiry_warnings.pop(user_id, None)

        with contextlib.suppress(Exception):
            await warning_msg.edit_text(
                "❌ **Session Expired**\n\n"
                "Your renaming session has been cancelled due to inactivity.\n"
                "Send a file or use /start to begin a new session.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Start New Session", callback_data="force_start_renaming")]
                ])
            )

    except asyncio.CancelledError:
        _expiry_warnings.pop(user_id, None)
    except Exception as e:
        logger.debug(f"Expiry warning error for {user_id}: {e}")
        _expiry_warnings.pop(user_id, None)


def _start_expiry_timer(client, user_id: int):
    """Start or restart the expiry warning timer."""
    old_task = _expiry_warnings.pop(user_id, None)
    if old_task:
        old_task.cancel()
    _expiry_warnings[user_id] = _spawn_task(
        _schedule_expiry_warning(client, user_id),
        user_id=user_id,
        label="expiry_warning",
    )


def _debounce_callback(user_id: int, callback_id: str) -> bool:
    """Returns True if this callback should be skipped (duplicate rapid-fire)."""
    key = f"{user_id}:{callback_id}"
    now = _time.time()
    last = _processing_callbacks.get(key, 0)
    if now - last < 0.5:
        return True
    _processing_callbacks[key] = now
    # Periodic inline cleanup: prune entries older than 60s when dict gets large
    if len(_processing_callbacks) > 500:
        cleanup_stale_debounce_entries(60)
    return False


# -- Small formatting helpers ------------------------------------------------
def format_episode_str(episode):
    if isinstance(episode, list):
        return "".join([f"E{int(e):02d}" for e in episode])
    elif episode is not None:
        return f"E{int(episode):02d}"
    return ""


# Placeholders accepted in the "enter new filename" general-rename flow.
# Kept next to the session state because both text_input.py and
# confirmation_screen.py need it and it's part of the flow's public
# contract for validation.
_GENERAL_RENAME_FIELDS = {
    "Title", "Year", "Quality", "Season", "Episode",
    "Season_Episode", "Language", "Channel", "Specials", "Codec", "Audio",
    "filename",
}
