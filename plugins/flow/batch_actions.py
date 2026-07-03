# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""Batch Actions — apply one change to every file of an upload batch.

After ``process_batch`` has sent the per-file confirmation messages for a
multi-file upload, it sends one extra *temporary* "Batch Actions" message.
From there the user can, in a single tap, apply a change to ALL files at
once instead of opening ten identical pickers:

 * Quality       — 480p / 720p / 1080p / 2160p for every file
 * Season        — set the season number on every series file
 * Specials      — toggle a specials tag on/off for every file
 * Audio / Codec — set the audio / codec tag on every file
 * Accept All    — confirm every file and start processing
 * Cancel All    — discard every file (quota reservations are released)

The message deletes itself when the batch is done: after Accept All /
Cancel All / Close, or once the last file has been confirmed or cancelled
individually (``note_file_closed`` is called from confirmation_screen).

Callback namespace: everything here starts with ``ba_`` — no other
plugin uses that prefix.
"""

import asyncio
import contextlib

from pyrogram import Client, filters
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import db
from plugins.flow.sessions import (
    _expiry_warnings,
    _file_session_timestamps,
    batch_action_sessions,
    file_sessions,
)
from utils.auth import auth_filter
from utils.state import get_data, get_state, set_state
from utils.tasks import spawn as _spawn_task
from utils.telegram.log import get_logger
from utils.ui_pagination import paginate_kb

logger = get_logger("plugins.flow.batch_actions")

_QUALITIES = ("480p", "720p", "1080p", "2160p")
_PICKER_PER_PAGE = 9
_PICKER_PER_ROW = 3


# -- Session helpers ----------------------------------------------------------
def _entry(user_id):
    return batch_action_sessions.get(user_id)


def _live_msg_ids(user_id) -> list[int]:
    """Return the batch's confirm-message ids that still have an open
    file session, pruning dead ones from the entry as a side effect."""
    entry = batch_action_sessions.get(user_id)
    if not entry:
        return []
    ids = [m for m in entry.get("msg_ids", []) if m in file_sessions]
    entry["msg_ids"] = ids
    return ids


def _apply_to_all(user_id, mutate) -> int:
    """Run ``mutate(fs)`` over every live file session of the batch.
    Returns how many sessions were touched."""
    count = 0
    for mid in _live_msg_ids(user_id):
        fs = file_sessions.get(mid)
        if fs is None:
            continue
        mutate(fs)
        count += 1
    return count


async def _rerender_all(client, user_id):
    """Re-draw every per-file confirmation message after a batch change.
    Sequential with a small delay so ten edits don't trip FloodWait."""
    from plugins.flow.confirmation_screen import update_confirmation_message

    for mid in _live_msg_ids(user_id):
        try:
            await update_confirmation_message(client, mid, user_id)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            with contextlib.suppress(Exception):
                await update_confirmation_message(client, mid, user_id)
        except Exception as e:
            logger.debug(f"batch rerender of {mid} failed: {e}")
        await asyncio.sleep(0.25)


async def _delete_menu(client, user_id):
    entry = batch_action_sessions.pop(user_id, None)
    if entry and entry.get("menu_msg_id"):
        with contextlib.suppress(Exception):
            await client.delete_messages(user_id, entry["menu_msg_id"])


# -- Menu rendering -----------------------------------------------------------
def _root_text(user_id) -> str:
    n = len(_live_msg_ids(user_id))
    return (
        "🗂 **Batch Actions**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"**{n}** file(s) waiting in this batch.\n\n"
        "Apply a change to **all of them at once**, or accept everything "
        "as-is. This message disappears when the batch is done."
    )


def _root_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📺 Quality", callback_data="ba_qual"),
                InlineKeyboardButton("🗓 Season", callback_data="ba_season"),
            ],
            [
                InlineKeyboardButton("🎞 Specials", callback_data="ba_spc"),
                InlineKeyboardButton("🔊 Audio", callback_data="ba_audio"),
            ],
            [InlineKeyboardButton("📼 Codec", callback_data="ba_codec")],
            [InlineKeyboardButton("✅ Accept All", callback_data="ba_accept_all")],
            [
                InlineKeyboardButton("🗑 Cancel All", callback_data="ba_cancel_all"),
                InlineKeyboardButton("✖ Close", callback_data="ba_close"),
            ],
        ]
    )


async def _show_root(client, user_id, message=None):
    """Edit the menu message back to the root screen."""
    entry = _entry(user_id)
    if not entry:
        return
    target_id = entry.get("menu_msg_id")
    if message is not None and target_id is None:
        target_id = message.id
    if target_id is None:
        return
    try:
        await client.edit_message_text(
            chat_id=user_id,
            message_id=target_id,
            text=_root_text(user_id),
            reply_markup=_root_keyboard(),
        )
    except MessageNotModified:
        pass
    except Exception as e:
        logger.debug(f"batch menu redraw failed: {e}")


async def send_batch_actions_message(client, user_id, msg_ids):
    """Called by ``process_batch`` after all per-file messages exist."""
    # A new batch supersedes any stale hub from a previous one.
    old = batch_action_sessions.pop(user_id, None)
    if old and old.get("menu_msg_id"):
        with contextlib.suppress(Exception):
            await client.delete_messages(user_id, old["menu_msg_id"])

    entry = {
        "msg_ids": list(msg_ids),
        "menu_msg_id": None,
        "prev_state": None,
        "spc_page": 0,
        "aud_page": 0,
        "cod_page": 0,
    }
    batch_action_sessions[user_id] = entry
    try:
        menu = await client.send_message(
            user_id, _root_text(user_id), reply_markup=_root_keyboard()
        )
        entry["menu_msg_id"] = menu.id
    except Exception as e:
        logger.warning(f"Could not send batch actions message: {e}")
        batch_action_sessions.pop(user_id, None)


async def note_file_closed(client, user_id, msg_id):
    """Called after a file was confirmed or cancelled individually.
    Removes it from the hub and deletes the hub when the batch is empty."""
    entry = _entry(user_id)
    if not entry:
        return
    if msg_id in entry.get("msg_ids", []):
        entry["msg_ids"].remove(msg_id)
    if not _live_msg_ids(user_id):
        await _delete_menu(client, user_id)
    else:
        await _show_root(client, user_id)


# -- Root / navigation callbacks ----------------------------------------------
@Client.on_callback_query(filters.regex(r"^ba_close$") & auth_filter)
async def handle_ba_close(client, callback_query):
    await callback_query.answer()
    await _delete_menu(client, callback_query.from_user.id)


@Client.on_callback_query(filters.regex(r"^ba_back$") & auth_filter)
async def handle_ba_back(client, callback_query):
    await callback_query.answer()
    user_id = callback_query.from_user.id
    entry = _entry(user_id)
    if not entry:
        with contextlib.suppress(Exception):
            await callback_query.message.delete()
        return
    # Leaving the season prompt: restore the state we hijacked.
    if get_state(user_id) == "awaiting_batch_season":
        set_state(user_id, entry.get("prev_state"))
    # Leaving a multi-toggle picker with pending changes: re-draw files.
    if entry.pop("dirty", False):
        await _rerender_all(client, user_id)
    await _show_root(client, user_id, callback_query.message)


# -- Quality -------------------------------------------------------------------
@Client.on_callback_query(filters.regex(r"^ba_qual$") & auth_filter)
async def handle_ba_quality_menu(client, callback_query):
    if not _entry(callback_query.from_user.id):
        await callback_query.answer("Batch expired.", show_alert=True)
        return
    await callback_query.answer()
    rows = [
        [
            InlineKeyboardButton("480p", callback_data="ba_setq_480p"),
            InlineKeyboardButton("720p", callback_data="ba_setq_720p"),
        ],
        [
            InlineKeyboardButton("1080p", callback_data="ba_setq_1080p"),
            InlineKeyboardButton("2160p", callback_data="ba_setq_2160p"),
        ],
        [InlineKeyboardButton("← Back", callback_data="ba_back")],
    ]
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            "📺 **Batch Quality**\n\nSelect the quality to apply to **every** file:",
            reply_markup=InlineKeyboardMarkup(rows),
        )


@Client.on_callback_query(filters.regex(r"^ba_setq_(.+)$") & auth_filter)
async def handle_ba_set_quality(client, callback_query):
    user_id = callback_query.from_user.id
    qual = callback_query.data.split("_")[-1]
    if qual not in _QUALITIES or not _entry(user_id):
        await callback_query.answer("Batch expired.", show_alert=True)
        return

    def mutate(fs):
        if not fs.get("is_subtitle"):
            fs["quality"] = qual

    count = _apply_to_all(user_id, mutate)
    await callback_query.answer(f"Quality {qual} applied to {count} file(s).")
    await _show_root(client, user_id, callback_query.message)
    await _rerender_all(client, user_id)


# -- Season ---------------------------------------------------------------------
@Client.on_callback_query(filters.regex(r"^ba_season$") & auth_filter)
async def handle_ba_season_prompt(client, callback_query):
    user_id = callback_query.from_user.id
    entry = _entry(user_id)
    if not entry:
        await callback_query.answer("Batch expired.", show_alert=True)
        return
    series_count = sum(
        1
        for mid in _live_msg_ids(user_id)
        if (file_sessions.get(mid) or {}).get("type") == "series"
    )
    if not series_count:
        await callback_query.answer(
            "No series files in this batch — season doesn't apply.",
            show_alert=True,
        )
        return
    await callback_query.answer()
    entry["prev_state"] = get_state(user_id)
    set_state(user_id, "awaiting_batch_season")
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            "🗓 **Batch Season**\n\n"
            f"Send the season number to apply to all **{series_count}** "
            "series file(s) (e.g. `2`).",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Back", callback_data="ba_back")]]
            ),
        )


async def handle_batch_season_input(client, message):
    """Text-input hook, dispatched from plugins.flow.search when the user
    state is ``awaiting_batch_season``."""
    user_id = message.from_user.id
    entry = _entry(user_id)
    if not entry:
        set_state(user_id, None)
        await message.reply_text("Batch expired. Please start a new session.")
        return

    text = (message.text or "").strip()
    if not text.isdigit():
        await message.reply_text("Invalid number. Send a season number like `2`.")
        return

    season = int(text)

    def mutate(fs):
        if fs.get("type") == "series":
            fs["season"] = season

    count = _apply_to_all(user_id, mutate)
    set_state(user_id, entry.get("prev_state"))
    with contextlib.suppress(Exception):
        await message.delete()
    await _show_root(client, user_id)
    await _rerender_all(client, user_id)
    logger.info(f"[batch] user={user_id} season={season} applied to {count} files")


# -- Specials (multi-toggle across all files) -----------------------------------
def _specials_selected_everywhere(user_id) -> set[str]:
    """Labels that are currently present on EVERY live file — those show
    as selected in the batch picker."""
    ids = _live_msg_ids(user_id)
    if not ids:
        return set()
    common = None
    for mid in ids:
        current = set((file_sessions.get(mid) or {}).get("specials") or [])
        common = current if common is None else (common & current)
    return common or set()


def _build_batch_specials_keyboard(user_id) -> InlineKeyboardMarkup:
    from plugins.flow.pickers import _SPECIALS_OPTIONS

    entry = _entry(user_id) or {}
    page = int(entry.get("spc_page", 0))
    selected_labels = _specials_selected_everywhere(user_id)
    items = [(str(i), label) for i, label in enumerate(_SPECIALS_OPTIONS)]
    selected_keys = {
        str(i) for i, label in enumerate(_SPECIALS_OPTIONS) if label in selected_labels
    }
    extras = [
        [InlineKeyboardButton("❌ Clear All", callback_data="ba_spc_clear")],
        [InlineKeyboardButton("✅ Done", callback_data="ba_back")],
    ]
    rows = paginate_kb(
        items=items,
        page=page,
        per_page=_PICKER_PER_PAGE,
        per_row=_PICKER_PER_ROW,
        selected=selected_keys,
        cb_template=lambda idx: f"ba_tspc_i{idx}",
        page_cb_template=lambda p: f"ba_spg_{p}",
        extra_rows=extras,
    )
    return InlineKeyboardMarkup(rows)


def _batch_specials_text(user_id) -> str:
    n = len(_live_msg_ids(user_id))
    return (
        "🎞 **Batch Specials**\n\n"
        f"Toggle tags for all **{n}** file(s). A ✅ means every file "
        "already carries the tag; tapping adds it to (or removes it from) "
        "**all** files. Only one source tag can be active at a time."
    )


@Client.on_callback_query(filters.regex(r"^ba_spc$") & auth_filter)
async def handle_ba_specials_menu(client, callback_query):
    user_id = callback_query.from_user.id
    entry = _entry(user_id)
    if not entry:
        await callback_query.answer("Batch expired.", show_alert=True)
        return
    await callback_query.answer()
    entry["spc_page"] = 0
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            _batch_specials_text(user_id),
            reply_markup=_build_batch_specials_keyboard(user_id),
        )


@Client.on_callback_query(filters.regex(r"^ba_spg_(\d+)$") & auth_filter)
async def handle_ba_specials_page(client, callback_query):
    await callback_query.answer()
    user_id = callback_query.from_user.id
    entry = _entry(user_id)
    if not entry:
        return
    entry["spc_page"] = int(callback_query.data.split("_")[-1])
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            _batch_specials_text(user_id),
            reply_markup=_build_batch_specials_keyboard(user_id),
        )


@Client.on_callback_query(filters.regex(r"^ba_tspc_i(\d+)$") & auth_filter)
async def handle_ba_toggle_special(client, callback_query):
    from plugins.flow.pickers import _SOURCE_LABELS, _SPECIALS_OPTIONS

    user_id = callback_query.from_user.id
    entry = _entry(user_id)
    if not entry:
        await callback_query.answer("Batch expired.", show_alert=True)
        return
    idx = int(callback_query.data.rsplit("i", 1)[-1])
    if not (0 <= idx < len(_SPECIALS_OPTIONS)):
        await callback_query.answer("Unknown special.", show_alert=True)
        return
    target = _SPECIALS_OPTIONS[idx]
    remove = target in _specials_selected_everywhere(user_id)

    def mutate(fs):
        current = list(fs.get("specials") or [])
        if remove:
            current = [s for s in current if s != target]
        else:
            if target in _SOURCE_LABELS:
                current = [s for s in current if s not in _SOURCE_LABELS]
            if target not in current:
                current.append(target)
        fs["specials"] = current
        fs["specials_locked"] = False

    count = _apply_to_all(user_id, mutate)
    entry["dirty"] = True  # re-render the file messages on Done
    verb = "removed from" if remove else "added to"
    await callback_query.answer(f"{target} {verb} {count} file(s).")
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_reply_markup(
            _build_batch_specials_keyboard(user_id)
        )


@Client.on_callback_query(filters.regex(r"^ba_spc_clear$") & auth_filter)
async def handle_ba_clear_specials(client, callback_query):
    user_id = callback_query.from_user.id
    entry = _entry(user_id)
    if not entry:
        await callback_query.answer("Batch expired.", show_alert=True)
        return

    def mutate(fs):
        fs["specials"] = []

    count = _apply_to_all(user_id, mutate)
    entry["dirty"] = True
    await callback_query.answer(f"Specials cleared on {count} file(s).")
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_reply_markup(
            _build_batch_specials_keyboard(user_id)
        )


# -- Audio / Codec (single-select across all files) ------------------------------
def _build_batch_single_keyboard(
    user_id, options: list[str], page_key: str, set_prefix: str, page_prefix: str
) -> InlineKeyboardMarkup:
    entry = _entry(user_id) or {}
    page = int(entry.get(page_key, 0))
    items = [(str(i), label) for i, label in enumerate(options)]
    extras = [
        [InlineKeyboardButton("🚫 None", callback_data=f"{set_prefix}_none")],
        [InlineKeyboardButton("← Back", callback_data="ba_back")],
    ]
    rows = paginate_kb(
        items=items,
        page=page,
        per_page=_PICKER_PER_PAGE,
        per_row=_PICKER_PER_ROW,
        selected=set(),
        cb_template=lambda idx: f"{set_prefix}_i{idx}",
        page_cb_template=lambda p: f"{page_prefix}_{p}",
        extra_rows=extras,
    )
    return InlineKeyboardMarkup(rows)


@Client.on_callback_query(filters.regex(r"^ba_audio$") & auth_filter)
async def handle_ba_audio_menu(client, callback_query):
    from plugins.flow.pickers import _AUDIO_OPTIONS

    user_id = callback_query.from_user.id
    entry = _entry(user_id)
    if not entry:
        await callback_query.answer("Batch expired.", show_alert=True)
        return
    await callback_query.answer()
    entry["aud_page"] = 0
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            "🔊 **Batch Audio**\n\nSelect the audio tag to apply to **every** file:",
            reply_markup=_build_batch_single_keyboard(
                user_id, _AUDIO_OPTIONS, "aud_page", "ba_saud", "ba_apg"
            ),
        )


@Client.on_callback_query(filters.regex(r"^ba_apg_(\d+)$") & auth_filter)
async def handle_ba_audio_page(client, callback_query):
    from plugins.flow.pickers import _AUDIO_OPTIONS

    await callback_query.answer()
    user_id = callback_query.from_user.id
    entry = _entry(user_id)
    if not entry:
        return
    entry["aud_page"] = int(callback_query.data.split("_")[-1])
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_reply_markup(
            _build_batch_single_keyboard(
                user_id, _AUDIO_OPTIONS, "aud_page", "ba_saud", "ba_apg"
            )
        )


@Client.on_callback_query(filters.regex(r"^ba_saud_(none|i\d+)$") & auth_filter)
async def handle_ba_set_audio(client, callback_query):
    from plugins.flow.pickers import _AUDIO_OPTIONS

    user_id = callback_query.from_user.id
    if not _entry(user_id):
        await callback_query.answer("Batch expired.", show_alert=True)
        return
    payload = callback_query.data.rsplit("_", 1)[-1]
    if payload == "none":
        value, locked = "", True
    else:
        idx = int(payload[1:])
        if not (0 <= idx < len(_AUDIO_OPTIONS)):
            await callback_query.answer("Unknown audio option.", show_alert=True)
            return
        value, locked = _AUDIO_OPTIONS[idx], False

    def mutate(fs):
        fs["audio"] = value
        fs["audio_locked"] = locked

    count = _apply_to_all(user_id, mutate)
    label = value or "None (locked)"
    await callback_query.answer(f"Audio {label} applied to {count} file(s).")
    await _show_root(client, user_id, callback_query.message)
    await _rerender_all(client, user_id)


@Client.on_callback_query(filters.regex(r"^ba_codec$") & auth_filter)
async def handle_ba_codec_menu(client, callback_query):
    from plugins.flow.pickers import _CODEC_OPTIONS

    user_id = callback_query.from_user.id
    entry = _entry(user_id)
    if not entry:
        await callback_query.answer("Batch expired.", show_alert=True)
        return
    await callback_query.answer()
    entry["cod_page"] = 0
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            "📼 **Batch Codec**\n\nSelect the codec tag to apply to **every** file:",
            reply_markup=_build_batch_single_keyboard(
                user_id, _CODEC_OPTIONS, "cod_page", "ba_scod", "ba_cpg"
            ),
        )


@Client.on_callback_query(filters.regex(r"^ba_cpg_(\d+)$") & auth_filter)
async def handle_ba_codec_page(client, callback_query):
    from plugins.flow.pickers import _CODEC_OPTIONS

    await callback_query.answer()
    user_id = callback_query.from_user.id
    entry = _entry(user_id)
    if not entry:
        return
    entry["cod_page"] = int(callback_query.data.split("_")[-1])
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_reply_markup(
            _build_batch_single_keyboard(
                user_id, _CODEC_OPTIONS, "cod_page", "ba_scod", "ba_cpg"
            )
        )


@Client.on_callback_query(filters.regex(r"^ba_scod_(none|i\d+)$") & auth_filter)
async def handle_ba_set_codec(client, callback_query):
    from plugins.flow.pickers import _CODEC_OPTIONS

    user_id = callback_query.from_user.id
    if not _entry(user_id):
        await callback_query.answer("Batch expired.", show_alert=True)
        return
    payload = callback_query.data.rsplit("_", 1)[-1]
    if payload == "none":
        value, locked = "", True
    else:
        idx = int(payload[1:])
        if not (0 <= idx < len(_CODEC_OPTIONS)):
            await callback_query.answer("Unknown codec option.", show_alert=True)
            return
        value, locked = _CODEC_OPTIONS[idx], False

    def mutate(fs):
        fs["codec"] = value
        fs["codec_locked"] = locked

    count = _apply_to_all(user_id, mutate)
    label = value or "None (locked)"
    await callback_query.answer(f"Codec {label} applied to {count} file(s).")
    await _show_root(client, user_id, callback_query.message)
    await _rerender_all(client, user_id)


# -- Accept All / Cancel All ------------------------------------------------------
@Client.on_callback_query(filters.regex(r"^ba_accept_all$") & auth_filter)
async def handle_ba_accept_all(client, callback_query):
    from plugins.process import process_file

    user_id = callback_query.from_user.id
    ids = _live_msg_ids(user_id)
    if not ids:
        await callback_query.answer("Batch expired.", show_alert=True)
        await _delete_menu(client, user_id)
        return

    await callback_query.answer(f"Starting {len(ids)} file(s)…")

    # Cancel the session-expiry warning once — same as a manual confirm.
    task = _expiry_warnings.pop(user_id, None)
    if task:
        task.cancel()

    sd = get_data(user_id) or {}
    started = 0
    for mid in ids:
        fs = file_sessions.pop(mid, None)
        _file_session_timestamps.pop(mid, None)
        if fs is None:
            continue
        if fs.get("is_auto"):
            full_data = fs
        else:
            if not sd.get("type"):
                logger.warning(f"[batch] accept-all: no session data for {mid}")
                continue
            full_data = sd.copy()
            full_data.update(fs)
        try:
            conf_msg = await client.get_messages(user_id, mid)
        except Exception as e:
            logger.warning(f"[batch] accept-all: cannot fetch msg {mid}: {e}")
            continue
        _spawn_task(
            process_file(client, conf_msg, full_data),
            user_id=user_id,
            label=f"process_file:batch:{user_id}",
            key=mid,
        )
        started += 1
        # Stagger the launches slightly so ten simultaneous status edits
        # don't hammer the API; the real concurrency limit is enforced by
        # process.py's per-user semaphores.
        await asyncio.sleep(0.5)

    logger.info(f"[batch] user={user_id} accept-all started {started} file(s)")
    await _delete_menu(client, user_id)


@Client.on_callback_query(filters.regex(r"^ba_cancel_all$") & auth_filter)
async def handle_ba_cancel_all(client, callback_query):
    user_id = callback_query.from_user.id
    ids = _live_msg_ids(user_id)
    if not ids:
        await callback_query.answer("Batch expired.", show_alert=True)
        await _delete_menu(client, user_id)
        return

    cancelled = 0
    for mid in ids:
        fs = file_sessions.pop(mid, None)
        _file_session_timestamps.pop(mid, None)
        if fs is None:
            continue
        if "file_message" in fs:
            media = (
                fs["file_message"].document
                or fs["file_message"].video
                or fs["file_message"].audio
                or fs["file_message"].photo
            )
            file_size = getattr(media, "file_size", 0) if media else 0
            if file_size > 0:
                with contextlib.suppress(Exception):
                    await db.release_quota(user_id, file_size)
        with contextlib.suppress(Exception):
            await client.delete_messages(user_id, mid)
        cancelled += 1

    await callback_query.answer(f"Cancelled {cancelled} file(s).")
    await _delete_menu(client, user_id)
