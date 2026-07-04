# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""MyFiles Insights + Tools bridge.

Two feature clusters that push MyFiles from "a list of files" to the
bot's central library:

**Insights** (`feature_toggles.myfiles_insights`)
 * 📈 Stats dashboard — totals, storage bytes, per-type breakdown,
   biggest files, expiring-soon counter.
 * ⏳ Expiring Soon — temp files closest to deletion, with a one-tap
   📌 rescue button per file.
 * 📌 Pinned — every pinned file in one list.
 * ♻️ Duplicates — groups of same-named files, with "keep newest,
   delete the rest" cleanup per group.
 * 🧩 Series completeness — per-season episode coverage of a series
   folder including the exact missing episode numbers.

**Tools bridge** (`feature_toggles.myfiles_tools_bridge`)
 * 🛠 Open in Tools on the file detail screen: pipes a STORED file
   straight into any tool flow (re-rename via auto-detect, converter,
   trimmer, audio editor, voice/video-note converter, media info) by
   copying it back into the chat and re-entering the normal upload
   pipeline — no manual re-upload needed. This is what makes MyFiles
   usable from everywhere instead of being an island.

Callback namespace: ``mf_ins_*``, ``mf_keep_*``, ``mf_dupe_*``,
``mf_complete_*``, ``mf_tools_*``, ``mf_tool_*`` — checked against the
existing grammar (handlers.py owns ``myfiles_*`` and a fixed ``mf_*``
prefix list; extras.py owns ``mf_trash/tag/ver/search/share/activity/
bulk/smart``). No overlaps.
"""

from __future__ import annotations

import contextlib
import datetime
import re

from bson import ObjectId
from pyrogram import Client, StopPropagation, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from db import db
from tools.mirror_leech.UIChrome import format_bytes
from tools.mirror_leech.UIChrome import frame_plain as frame
from utils.auth.feature_gate import feature_enabled
from utils.myfiles.insights_logic import build_completeness_report, dupe_key
from utils.telegram.log import get_logger

logger = get_logger("plugins.myfiles.insights")

_NOT_TRASHED = {"is_deleted": {"$ne": True}}


def _scope(user_id: int) -> dict:
    """Base filter: per-user in public mode, global otherwise, and never
    show trashed files."""
    q = dict(_NOT_TRASHED)
    if Config.PUBLIC_MODE:
        q["user_id"] = user_id
    return q


def _short(name: str, n: int = 34) -> str:
    return name if len(name) <= n else name[: n - 3] + "..."


async def _edit(cq: CallbackQuery, text: str, markup: InlineKeyboardMarkup):
    with contextlib.suppress(MessageNotModified):
        await cq.message.edit_text(text, reply_markup=markup)


# ---------------------------------------------------------------------------
# 📈 Stats dashboard
# ---------------------------------------------------------------------------

@Client.on_callback_query(filters.regex(r"^mf_ins_stats$"))
async def insights_stats(client: Client, cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    if not await feature_enabled("myfiles_insights", user_id):
        await cq.answer("Insights is disabled.", show_alert=True)
        return
    await cq.answer()

    base = _scope(user_id)

    total = await db.files.count_documents(base)
    perm = await db.files.count_documents({**base, "status": "permanent"})
    temp = await db.files.count_documents({**base, "status": "temporary"})
    pinned = await db.files.count_documents({**base, "pinned": True})

    soon = datetime.datetime.utcnow() + datetime.timedelta(hours=72)
    expiring = await db.files.count_documents(
        {**base, "status": "temporary", "expires_at": {"$lte": soon, "$ne": None}}
    )

    # Storage + per-type breakdown in one aggregation pass.
    by_type: list[str] = []
    total_bytes = 0
    try:
        pipeline = [
            {"$match": base},
            {
                "$group": {
                    "_id": {"$ifNull": ["$media_type", "other"]},
                    "count": {"$sum": 1},
                    "bytes": {"$sum": {"$ifNull": ["$file_size", 0]}},
                }
            },
            {"$sort": {"count": -1}},
        ]
        async for row in db.files.aggregate(pipeline):
            total_bytes += int(row.get("bytes") or 0)
            label = str(row["_id"]).capitalize()
            size_part = (
                f" · {format_bytes(row['bytes'])}" if row.get("bytes") else ""
            )
            by_type.append(f"  `{label}`: **{row['count']}**{size_part}")
    except Exception as e:
        logger.debug(f"stats aggregation failed: {e}")

    # Top 3 biggest files (only docs that carry file_size).
    biggest: list[str] = []
    with contextlib.suppress(Exception):
        cursor = (
            db.files.find({**base, "file_size": {"$gt": 0}})
            .sort("file_size", -1)
            .limit(3)
        )
        async for f in cursor:
            biggest.append(
                f"  `{_short(f.get('file_name', '?'), 30)}` — "
                f"{format_bytes(f.get('file_size', 0))}"
            )

    folders = await db.folders.count_documents(
        {"user_id": user_id} if Config.PUBLIC_MODE else {}
    )

    lines = [
        f"**Files:** `{total}`  (📌 {perm} permanent · ⏳ {temp} temporary)",
        f"**Folders:** `{folders}` · **Pinned:** `{pinned}`",
    ]
    if total_bytes:
        lines.append(f"**Tracked storage:** `{format_bytes(total_bytes)}`")
    if expiring:
        lines.append(f"⚠️ **{expiring}** file(s) expire within 72h!")
    if by_type:
        lines.append("")
        lines.append("**By type:**")
        lines.extend(by_type)
    if biggest:
        lines.append("")
        lines.append("**Biggest files:**")
        lines.extend(biggest)

    text = frame("📈 MyFiles Insights", "\n".join(lines))
    buttons = [
        [
            InlineKeyboardButton(f"⏳ Expiring ({expiring})", callback_data="mf_ins_expiring"),
            InlineKeyboardButton(f"📌 Pinned ({pinned})", callback_data="mf_ins_pinned"),
        ],
        [InlineKeyboardButton("♻️ Find Duplicates", callback_data="mf_ins_dupes")],
        [InlineKeyboardButton("← Back", callback_data="myfiles_main")],
    ]
    await _edit(cq, text, InlineKeyboardMarkup(buttons))


# ---------------------------------------------------------------------------
# ⏳ Expiring soon (+ one-tap rescue)
# ---------------------------------------------------------------------------

@Client.on_callback_query(filters.regex(r"^mf_ins_expiring$"))
async def insights_expiring(client: Client, cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    if not await feature_enabled("myfiles_insights", user_id):
        await cq.answer("Insights is disabled.", show_alert=True)
        return
    await cq.answer()

    base = _scope(user_id)
    cursor = (
        db.files.find(
            {**base, "status": "temporary", "expires_at": {"$ne": None}}
        )
        .sort("expires_at", 1)
        .limit(10)
    )
    files = await cursor.to_list(length=10)

    if not files:
        text = frame(
            "⏳ Expiring Soon",
            "Nothing is about to expire. All quiet. ✨",
        )
        await _edit(
            cq,
            text,
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Back", callback_data="mf_ins_stats")]]
            ),
        )
        return

    now = datetime.datetime.utcnow()
    buttons: list[list[InlineKeyboardButton]] = []
    for f in files:
        fid = str(f["_id"])
        left = f.get("expires_at") - now
        hours = max(0, int(left.total_seconds() // 3600))
        left_str = f"{hours}h" if hours < 48 else f"{hours // 24}d"
        buttons.append(
            [
                InlineKeyboardButton(
                    f"⏳{left_str} · {_short(f.get('file_name', '?'), 26)}",
                    callback_data=f"myfiles_file_{fid}",
                ),
                InlineKeyboardButton("📌 Keep", callback_data=f"mf_keep_{fid}"),
            ]
        )
    buttons.append([InlineKeyboardButton("← Back", callback_data="mf_ins_stats")])

    text = frame(
        "⏳ Expiring Soon",
        "These temporary files are closest to deletion.\n"
        "Tap **📌 Keep** to make one permanent.",
    )
    await _edit(cq, text, InlineKeyboardMarkup(buttons))


@Client.on_callback_query(filters.regex(r"^mf_keep_([0-9a-f]{24})$"))
async def insights_keep(client: Client, cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    if not await feature_enabled("myfiles_insights", user_id):
        await cq.answer("Insights is disabled.", show_alert=True)
        return
    fid = ObjectId(cq.data.rsplit("_", 1)[-1])
    q: dict = {"_id": fid}
    if Config.PUBLIC_MODE:
        q["user_id"] = user_id
    res = await db.files.update_one(
        q, {"$set": {"status": "permanent"}, "$unset": {"expires_at": ""}}
    )
    if res.modified_count:
        await cq.answer("📌 File is now permanent.")
        with contextlib.suppress(Exception):
            await db.log_myfiles_activity(user_id, "kept", file_id=fid)
    else:
        await cq.answer("File not found.", show_alert=True)
    await insights_expiring(client, cq)


# ---------------------------------------------------------------------------
# 📌 Pinned
# ---------------------------------------------------------------------------

@Client.on_callback_query(filters.regex(r"^mf_ins_pinned$"))
async def insights_pinned(client: Client, cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    if not await feature_enabled("myfiles_insights", user_id):
        await cq.answer("Insights is disabled.", show_alert=True)
        return
    await cq.answer()

    base = _scope(user_id)
    cursor = db.files.find({**base, "pinned": True}).sort("created_at", -1).limit(25)
    files = await cursor.to_list(length=25)

    body = (
        "Your pinned files, newest first."
        if files
        else "No pinned files yet. Pin files via multi-select → 📌 Pin."
    )
    buttons: list[list[InlineKeyboardButton]] = []
    for f in files:
        buttons.append(
            [
                InlineKeyboardButton(
                    f"📌 {_short(f.get('file_name', '?'))}",
                    callback_data=f"myfiles_file_{str(f['_id'])}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton("← Back", callback_data="mf_ins_stats")])
    await _edit(cq, frame("📌 Pinned Files", body), InlineKeyboardMarkup(buttons))


# ---------------------------------------------------------------------------
# ♻️ Duplicate finder
# ---------------------------------------------------------------------------



async def _dupe_groups(user_id: int) -> list[dict]:
    """Return duplicate groups: [{key, files: [docs newest-first]}]."""
    base = _scope(user_id)
    groups: dict[str, list[dict]] = {}
    cursor = db.files.find(base).sort("created_at", -1)
    async for f in cursor:
        groups.setdefault(dupe_key(f.get("file_name", "")), []).append(f)
    out = [
        {"key": k, "files": v}
        for k, v in groups.items()
        if k and len(v) > 1
    ]
    out.sort(key=lambda g: len(g["files"]), reverse=True)
    return out[:15]


@Client.on_callback_query(filters.regex(r"^mf_ins_dupes$"))
async def insights_dupes(client: Client, cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    if not await feature_enabled("myfiles_insights", user_id):
        await cq.answer("Insights is disabled.", show_alert=True)
        return
    await cq.answer()

    groups = await _dupe_groups(user_id)
    if not groups:
        await _edit(
            cq,
            frame("♻️ Duplicates", "No duplicates found — your library is clean. ✨"),
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Back", callback_data="mf_ins_stats")]]
            ),
        )
        return

    # Stash the group keys in myfiles_state so cleanup callbacks can
    # reference a group by index (24-char ObjectIds don't fit for a
    # whole group in 64-byte callback data).
    from plugins.myfiles.core import get_myfiles_state, set_myfiles_state

    state = await get_myfiles_state(user_id)
    state["dupe_keys"] = [g["key"] for g in groups]
    await set_myfiles_state(user_id, state)

    buttons: list[list[InlineKeyboardButton]] = []
    for idx, g in enumerate(groups):
        sample = g["files"][0].get("file_name", "?")
        buttons.append(
            [
                InlineKeyboardButton(
                    f"×{len(g['files'])} · {_short(sample, 28)}",
                    callback_data=f"mf_dupe_clean_{idx}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton("← Back", callback_data="mf_ins_stats")])

    text = frame(
        "♻️ Duplicates",
        f"**{len(groups)}** duplicate group(s) found (grouped by name).\n"
        "Tapping a group keeps the **newest** copy and deletes the rest.",
    )
    await _edit(cq, text, InlineKeyboardMarkup(buttons))


@Client.on_callback_query(filters.regex(r"^mf_dupe_clean_(\d+)$"))
async def insights_dupe_clean(client: Client, cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    if not await feature_enabled("myfiles_insights", user_id):
        await cq.answer("Insights is disabled.", show_alert=True)
        return

    from plugins.myfiles.core import get_myfiles_state

    idx = int(cq.data.rsplit("_", 1)[-1])
    state = await get_myfiles_state(user_id)
    keys = state.get("dupe_keys") or []
    if idx >= len(keys):
        await cq.answer("This list is stale — reopening.", show_alert=True)
        await insights_dupes(client, cq)
        return

    target_key = keys[idx]
    base = _scope(user_id)
    matches: list[dict] = []
    cursor = db.files.find(base).sort("created_at", -1)
    async for f in cursor:
        if dupe_key(f.get("file_name", "")) == target_key:
            matches.append(f)

    if len(matches) < 2:
        await cq.answer("Already clean.")
        await insights_dupes(client, cq)
        return

    to_delete = matches[1:]  # newest-first sort → keep matches[0]
    ids = [f["_id"] for f in to_delete]
    res = await db.files.delete_many({"_id": {"$in": ids}})
    freed = sum(int(f.get("file_size") or 0) for f in to_delete)
    with contextlib.suppress(Exception):
        await db.myfiles_incr_quota(
            user_id, bytes_delta=-freed, file_delta=-res.deleted_count
        )
    with contextlib.suppress(Exception):
        await db.audit_myfiles(
            user_id, "dupe_clean", meta={"deleted": res.deleted_count, "key": target_key}
        )
    await cq.answer(
        f"🧹 Removed {res.deleted_count} duplicate(s), kept the newest.",
        show_alert=True,
    )
    await insights_dupes(client, cq)


# ---------------------------------------------------------------------------
# 🧩 Series completeness
# ---------------------------------------------------------------------------



@Client.on_callback_query(filters.regex(r"^mf_complete_([0-9a-f]{24})$"))
async def insights_completeness(client: Client, cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    if not await feature_enabled("myfiles_insights", user_id):
        await cq.answer("Insights is disabled.", show_alert=True)
        return
    await cq.answer()

    folder_id = ObjectId(cq.data.rsplit("_", 1)[-1])
    folder = await db.folders.find_one({"_id": folder_id})
    if not folder:
        await cq.answer("Folder not found.", show_alert=True)
        return

    q = _scope(user_id)
    q["folder_id"] = folder_id
    files = await db.files.find(q).to_list(length=None)

    lines = build_completeness_report(files)
    body = (
        "\n".join(lines)
        if lines
        else "No season/episode data found in this folder yet."
    )
    text = frame(f"🧩 {folder.get('name', 'Series')} — Completeness", body)
    await _edit(
        cq,
        text,
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("← Back", callback_data=f"myfiles_folder_{folder_id}")]]
        ),
    )


# ---------------------------------------------------------------------------
# 🛠 Tools bridge — pipe a stored file into any tool flow
# ---------------------------------------------------------------------------

# (callback key, button label, awaiting-state or "" for auto-detect)
_TOOL_TARGETS: list[tuple[str, str, str]] = [
    ("rename", "🔁 Re-Rename (Auto-Detect)", ""),
    ("convert", "🔄 File Converter", "awaiting_convert_file"),
    ("trim", "✂️ Video Trimmer", "awaiting_trim_file"),
    ("audio", "🎧 Audio Editor", "awaiting_audio_file"),
    ("voice", "🎙 Voice Note", "awaiting_voice_file"),
    ("vnote", "⭕ Video Note", "awaiting_videonote_file"),
    ("minfo", "ℹ️ Media Info", "awaiting_mediainfo_file"),
]
_TOOL_BY_KEY = {k: (label, state) for k, label, state in _TOOL_TARGETS}


@Client.on_callback_query(filters.regex(r"^mf_tools_([0-9a-f]{24})$"))
async def tools_menu(client: Client, cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    if not await feature_enabled("myfiles_tools_bridge", user_id):
        await cq.answer("Tools bridge is disabled.", show_alert=True)
        return
    await cq.answer()

    file_id = cq.data.rsplit("_", 1)[-1]
    f = await db.files.find_one({"_id": ObjectId(file_id)})
    if not f:
        await cq.answer("File not found.", show_alert=True)
        return

    rows = [
        [InlineKeyboardButton(label, callback_data=f"mf_tool_{key}_{file_id}")]
        for key, label, _state in _TOOL_TARGETS
    ]
    rows.append(
        [InlineKeyboardButton("← Back", callback_data=f"myfiles_file_{file_id}")]
    )
    text = frame(
        "🛠 Open in Tools",
        f"`{_short(f.get('file_name', '?'), 40)}`\n\n"
        "The stored file is copied back into this chat and fed straight "
        "into the tool you pick — no re-upload needed.",
    )
    await _edit(cq, text, InlineKeyboardMarkup(rows))


@Client.on_callback_query(filters.regex(r"^mf_tool_([a-z]+)_([0-9a-f]{24})$"))
async def tools_dispatch(client: Client, cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    if not await feature_enabled("myfiles_tools_bridge", user_id):
        await cq.answer("Tools bridge is disabled.", show_alert=True)
        return

    m = re.match(r"^mf_tool_([a-z]+)_([0-9a-f]{24})$", cq.data)
    tool_key, file_id = m.group(1), m.group(2)
    if tool_key not in _TOOL_BY_KEY:
        await cq.answer("Unknown tool.", show_alert=True)
        return

    f = await db.files.find_one({"_id": ObjectId(file_id)})
    if not f:
        await cq.answer("File not found.", show_alert=True)
        return

    await cq.answer("📥 Fetching the stored file…")

    # Copy the stored message back into the user's chat.
    try:
        copied = await client.copy_message(
            chat_id=user_id,
            from_chat_id=f["channel_id"],
            message_id=f["message_id"],
        )
    except Exception as e:
        logger.warning(f"tools bridge copy failed for {file_id}: {e}")
        await cq.answer(
            "❌ Could not fetch the stored file (source message gone?).",
            show_alert=True,
        )
        return

    # The copy is an outgoing bot message; the upload pipeline derives
    # the acting user from message.from_user, so stamp the real user on
    # it before re-entering the flow.
    copied.from_user = cq.from_user

    from utils.state import clear_session, set_state

    _label, awaiting_state = _TOOL_BY_KEY[tool_key]
    clear_session(user_id)
    if awaiting_state:
        set_state(user_id, awaiting_state)

    from plugins.flow.upload import handle_file_upload

    try:
        await handle_file_upload(client, copied)
    except StopPropagation:
        pass
    except Exception as e:
        logger.error(f"tools bridge dispatch failed ({tool_key}): {e}")
        with contextlib.suppress(Exception):
            await client.send_message(
                user_id, f"❌ Could not start the tool: `{e}`"
            )
        return

    with contextlib.suppress(Exception):
        await db.log_myfiles_activity(
            user_id, f"tool:{tool_key}", file_id=ObjectId(file_id)
        )
