# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""MyFiles → Dumb Channel sender.

Pushes STORED files into any configured dumb channel — the same
one-tap distribution the rename pipeline does at processing time, but
available retroactively for anything already in the library:

 * file detail  → "📡 Send to Channel"  → channel picker → sent
 * multi-select → "📡 Send Selected to Channel" → picker → batch send

The send is a plain ``copy_message`` from the storage channel, exactly
like ``plugins/process.py`` copies fresh outputs into dumb channels, so
captions and media stay byte-identical to the normal pipeline.

Mode handling mirrors the rest of MyFiles: dumb channels come from
``db.get_dumb_channels(user_id)`` — per-user in PUBLIC_MODE, the global
single-tenant set otherwise.

Callback namespace: ``mf_send_*`` (checked against handlers.py's prefix
list ``mf_mov_/mf_df_/mf_pg_/mf_st/mf_ms/mf_sea_/mf_sa`` — no overlap).
"""

from __future__ import annotations

import asyncio
import contextlib

from bson import ObjectId
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, MessageNotModified, PeerIdInvalid
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from db import db
from tools.mirror_leech.UIChrome import frame_plain as frame
from utils.telegram.log import get_logger

logger = get_logger("plugins.myfiles.send_channel")


def _short(name: str, n: int = 34) -> str:
    return name if len(name) <= n else name[: n - 3] + "..."


async def _edit(cq: CallbackQuery, text: str, markup: InlineKeyboardMarkup):
    with contextlib.suppress(MessageNotModified):
        await cq.message.edit_text(text, reply_markup=markup)


async def _channel_options(user_id: int) -> list[tuple[str, str]]:
    """Return [(channel_id, display_name)] of every configured dumb
    channel, with the default/movie/series roles marked."""
    channels = await db.get_dumb_channels(user_id) or {}
    default_ch = await db.get_default_dumb_channel(user_id)
    movie_ch = await db.get_movie_dumb_channel(user_id)
    series_ch = await db.get_series_dumb_channel(user_id)

    out: list[tuple[str, str]] = []
    for ch_id, name in channels.items():
        marks = ""
        if str(ch_id) == str(default_ch):
            marks += " ⭐"
        if str(ch_id) == str(movie_ch):
            marks += " 🎬"
        if str(ch_id) == str(series_ch):
            marks += " 📺"
        out.append((str(ch_id), f"{name}{marks}"))
    return out


async def _copy_to_channel(client: Client, f: dict, channel_id: int) -> None:
    """Copy a stored file into ``channel_id``; retries once after warming
    the peer cache. Raises on final failure so callers can report it."""
    try:
        await client.copy_message(
            chat_id=channel_id,
            from_chat_id=f["channel_id"],
            message_id=f["message_id"],
        )
    except PeerIdInvalid:
        await client.get_chat(channel_id)
        await client.copy_message(
            chat_id=channel_id,
            from_chat_id=f["channel_id"],
            message_id=f["message_id"],
        )


def _picker_rows(
    options: list[tuple[str, str]], go_prefix: str, back_cb: str
) -> list[list[InlineKeyboardButton]]:
    rows = [
        [InlineKeyboardButton(f"📡 {label}", callback_data=f"{go_prefix}{ch_id}")]
        for ch_id, label in options
    ]
    rows.append([InlineKeyboardButton("← Back", callback_data=back_cb)])
    return rows


# ---------------------------------------------------------------------------
# Single file
# ---------------------------------------------------------------------------

@Client.on_callback_query(filters.regex(r"^mf_send_ch_([0-9a-f]{24})$"))
async def send_channel_picker(client: Client, cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    file_id = cq.data.rsplit("_", 1)[-1]

    f = await db.files.find_one({"_id": ObjectId(file_id)})
    if not f:
        await cq.answer("File not found.", show_alert=True)
        return

    options = await _channel_options(user_id)
    if not options:
        await cq.answer(
            "No dumb channels configured yet. Add one first.", show_alert=True
        )
        return
    await cq.answer()

    text = frame(
        "📡 Send to Channel",
        f"`{_short(f.get('file_name', '?'), 40)}`\n\n"
        "Pick the channel to post this file to.\n"
        "⭐ default · 🎬 movies · 📺 series",
    )
    rows = _picker_rows(
        options, f"mf_send_go_{file_id}_", f"myfiles_file_{file_id}"
    )
    await _edit(cq, text, InlineKeyboardMarkup(rows))


@Client.on_callback_query(filters.regex(r"^mf_send_go_([0-9a-f]{24})_(-?\d+)$"))
async def send_channel_go(client: Client, cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    parts = cq.data.replace("mf_send_go_", "").split("_")
    file_id, channel_id = parts[0], int(parts[1])

    f = await db.files.find_one({"_id": ObjectId(file_id)})
    if not f:
        await cq.answer("File not found.", show_alert=True)
        return

    # The channel must still be one of the user's configured channels —
    # defends against replayed/stale callback data.
    channels = await db.get_dumb_channels(user_id) or {}
    if str(channel_id) not in {str(c) for c in channels}:
        await cq.answer("That channel is no longer configured.", show_alert=True)
        return

    try:
        await _copy_to_channel(client, f, channel_id)
    except Exception as e:
        logger.warning(f"MyFiles→channel send failed ({file_id}→{channel_id}): {e}")
        await cq.answer(f"❌ Send failed: {e}", show_alert=True)
        return

    with contextlib.suppress(Exception):
        await db.log_myfiles_activity(
            user_id, f"sent_channel:{channel_id}", file_id=ObjectId(file_id)
        )
    await cq.answer(
        f"✅ Posted to {channels.get(str(channel_id), 'channel')}.", show_alert=True
    )
    # Return to the file detail screen.
    from plugins.myfiles.handlers import myfiles_callback

    cq.data = f"myfiles_file_{file_id}"
    await myfiles_callback(client, cq)


# ---------------------------------------------------------------------------
# Multi-select
# ---------------------------------------------------------------------------

@Client.on_callback_query(filters.regex(r"^mf_send_ch_multi$"))
async def send_channel_picker_multi(client: Client, cq: CallbackQuery) -> None:
    from plugins.myfiles.core import get_myfiles_state

    user_id = cq.from_user.id
    state = await get_myfiles_state(user_id)
    selected = state.get("selected_files", [])
    if not selected:
        await cq.answer("No files selected.", show_alert=True)
        return

    options = await _channel_options(user_id)
    if not options:
        await cq.answer(
            "No dumb channels configured yet. Add one first.", show_alert=True
        )
        return
    await cq.answer()

    back_cb = state.get("current_view", "myfiles_main")
    text = frame(
        "📡 Send Selected to Channel",
        f"**{len(selected)}** file(s) selected.\n\n"
        "Pick the channel to post them to (order follows your list).\n"
        "⭐ default · 🎬 movies · 📺 series",
    )
    rows = _picker_rows(options, "mf_send_go_multi_", back_cb)
    await _edit(cq, text, InlineKeyboardMarkup(rows))


@Client.on_callback_query(filters.regex(r"^mf_send_go_multi_(-?\d+)$"))
async def send_channel_go_multi(client: Client, cq: CallbackQuery) -> None:
    from plugins.myfiles.core import get_myfiles_state, set_myfiles_state

    user_id = cq.from_user.id
    channel_id = int(cq.data.rsplit("_", 1)[-1])

    channels = await db.get_dumb_channels(user_id) or {}
    if str(channel_id) not in {str(c) for c in channels}:
        await cq.answer("That channel is no longer configured.", show_alert=True)
        return

    state = await get_myfiles_state(user_id)
    selected = state.get("selected_files", [])
    if not selected:
        await cq.answer("No files selected.", show_alert=True)
        return

    ch_name = channels.get(str(channel_id), "channel")
    await cq.answer(f"📡 Sending {len(selected)} file(s) to {ch_name}…")
    with contextlib.suppress(Exception):
        await cq.message.edit_text(
            frame(
                "📡 Send Selected to Channel",
                f"Posting **{len(selected)}** file(s) to **{ch_name}**…",
            ),
            reply_markup=None,
        )

    sent = 0
    failed = 0
    for fid in selected:
        try:
            f = await db.files.find_one({"_id": ObjectId(fid)})
        except Exception:
            f = None
        if not f:
            failed += 1
            continue
        try:
            await _copy_to_channel(client, f, channel_id)
            sent += 1
            with contextlib.suppress(Exception):
                await db.log_myfiles_activity(
                    user_id, f"sent_channel:{channel_id}", file_id=f["_id"]
                )
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            try:
                await _copy_to_channel(client, f, channel_id)
                sent += 1
            except Exception as e2:
                logger.warning(f"multi channel send failed for {fid}: {e2}")
                failed += 1
        except Exception as e:
            logger.warning(f"multi channel send failed for {fid}: {e}")
            failed += 1
        # Pace the copies so a 20-file batch doesn't trip FloodWait.
        await asyncio.sleep(1.0)

    # Leave multi-select mode; the selection has been consumed.
    state = await get_myfiles_state(user_id)
    state["multi_select"] = False
    state["selected_files"] = []
    await set_myfiles_state(user_id, state)

    summary = f"✅ Posted **{sent}** file(s) to **{ch_name}**."
    if failed:
        summary += f"\n⚠️ **{failed}** failed — see logs."
    back_cb = state.get("current_view", "myfiles_main")
    await _edit(
        cq,
        frame("📡 Send Selected to Channel", summary),
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("← Back to Files", callback_data=back_cb)]]
        ),
    )
