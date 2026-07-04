# --- Imports ---
import contextlib
import io
import json
import time
from datetime import datetime

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Config
from db import db
from plugins.admin.core import admin_sessions, edit_or_reply
from utils.telegram.log import get_logger

logger = get_logger("plugins.admin.users")

# === Helper Functions ===
def is_admin(user_id):
    return user_id == Config.CEO_ID or user_id in Config.ADMIN_IDS

SEPARATOR = "━━━━━━━━━━━━━━━━━━━━"


async def show_users_menu(client, update):
    text = (
        "**👤 Global User Management**\n"
        f"{SEPARATOR}\n\n"
        "Manage all users across the network.\n"
        "Search, filter, ban, and view detailed profiles.\n\n"
        "> Tip: use 🔍 Search to find by name, username or numeric ID."
    )

    buttons = [
        [InlineKeyboardButton("🔍 Search User", callback_data="admin_user_search_start")],
        [
            InlineKeyboardButton("👥 All Users", callback_data="list_users|all|0"),
            InlineKeyboardButton("🚫 Banned", callback_data="list_users|banned|0")
        ]
    ]

    if Config.PUBLIC_MODE:
        buttons[1].append(InlineKeyboardButton("💎 Premium", callback_data="list_users|premium|0"))

    buttons.append([InlineKeyboardButton("🕒 Recent", callback_data="list_users|recent|0")])
    buttons.append([InlineKeyboardButton("← Back to Admin Panel", callback_data="admin_main")])

    markup = InlineKeyboardMarkup(buttons)

    if isinstance(update, Message):
        await update.reply(text, reply_markup=markup)
    else:

        try:
            await update.edit_message_text(text, reply_markup=markup)
        except Exception:

            await client.send_message(update.from_user.id, text, reply_markup=markup)

@Client.on_callback_query(filters.regex(r"^admin_users_menu$"))

# --- Handlers ---
async def admin_users_menu(client, callback):
    if not is_admin(callback.from_user.id):
        return

    await show_users_menu(client, callback)

@Client.on_callback_query(filters.regex(r"^list_users\|"))
async def list_users(client, callback):

    if not is_admin(callback.from_user.id):
        return

    try:
        _, mode, page = callback.data.split("|")
        page = int(page)
    except Exception:
        mode = "all"
        page = 0

    limit = 10
    skip = page * limit

    filter_dict = {}
    sort_by = "joined_at"

    if mode == "premium":
        filter_dict = {"is_premium": True}
    elif mode == "banned":
        filter_dict = {"banned": True}
    elif mode == "recent":
        sort_by = "updated_at"

    users = await db.get_users_paginated(filter_dict, skip, limit, sort_by)
    total = await db.count_users(filter_dict)

    mode_meta = {
        "all":     ("👥 All Users",   "users"),
        "banned":  ("🚫 Banned",      "users"),
        "premium": ("💎 Premium",     "users"),
        "recent":  ("🕒 Recent",      "users"),
    }
    title, noun = mode_meta.get(mode, ("👤 Users", "users"))
    total_pages = max(1, (total + limit - 1) // limit) if total else 1
    header_suffix = (
        f"Page {page + 1}/{total_pages}" if total > 0 else f"0 {noun}"
    )

    text_lines = [
        f"**{title} — {header_suffix}**",
        SEPARATOR,
        "",
    ]
    if total > 0:
        text_lines.append(f"__Total {noun}: `{total}`__")
        text_lines.append("")

    markup = []
    if not users:
        text_lines.append("__No users found.__")
    else:
        text_lines.append("> Tap a row to view profile.")
        for u in users:
            uid = u.get("user_id")
            name = u.get("first_name") or "Unknown"
            name = name[:15]
            uname = f"(@{u.get('username')})" if u.get("username") else ""
            status = "🚫" if u.get("banned") else ("💎" if u.get("is_premium") else "👤")

            label = f"{status} {name} {uname}".rstrip()
            # Carry the originating list (mode + page) through the profile
            # callbacks so "Back" returns to THIS page, not page 1.
            markup.append([InlineKeyboardButton(label, callback_data=f"view_user|{uid}|{mode}|{page}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("‹ Prev", callback_data=f"list_users|{mode}|{page-1}"))
    nav.append(InlineKeyboardButton(f"Page {page + 1}/{total_pages}", callback_data="noop"))
    if (skip + limit) < total:
        nav.append(InlineKeyboardButton("Next ›", callback_data=f"list_users|{mode}|{page+1}"))

    if len(nav) > 1:
        markup.append(nav)

    markup.append([InlineKeyboardButton("← Back to User Management", callback_data="admin_users_menu")])

    await callback.edit_message_text("\n".join(text_lines), reply_markup=InlineKeyboardMarkup(markup))

@Client.on_callback_query(filters.regex(r"^admin_user_search_start$"))
async def start_user_search(client, callback):
    if not is_admin(callback.from_user.id):
        return
    admin_sessions[callback.from_user.id] = "wait_search_query"
    with contextlib.suppress(Exception):
        await callback.message.edit_text(
            "**🔍 Find User**\n"
            f"{SEPARATOR}\n\n"
            "Send one of the following as plain text:\n\n"
            "• `@username` or just `username`\n"
            "• Full or partial first/last name\n"
            "• Numeric Telegram ID\n\n"
            "> Results are paginated. Tap any row to open the profile.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_users_menu")]])
        )

def _parse_origin(callback_data: str) -> tuple[str, str]:
    """Extract the originating list (mode, page) from a profile/action
    callback. Every callback in this flow is `<verb>|<uid>[|<mode>|<page>]`,
    so parts[2:4] is the origin regardless of the verb. Returns
    ("", "") when the profile was opened without list context (search)."""
    parts = callback_data.split("|")
    if len(parts) >= 4 and parts[3].isdigit():
        return parts[2], parts[3]
    return "", ""


@Client.on_callback_query(filters.regex(r"^view_user\|"))
async def view_user_profile(client, callback):
    if not is_admin(callback.from_user.id):
        return

    try:
        target_id = int(callback.data.split("|")[1])
    except Exception:
        await callback.answer("Invalid User ID", show_alert=True)
        return

    origin_mode, origin_page = _parse_origin(callback.data)
    origin_suffix = f"|{origin_mode}|{origin_page}" if origin_mode else ""

    user = await db.get_user(target_id)
    if not user:
        await callback.answer("User not found in DB.", show_alert=True)
        return

    username = f"@{user.get('username')}" if user.get("username") else "N/A"
    joined_ts = user.get("joined_at", 0)
    joined_date = datetime.fromtimestamp(joined_ts).strftime('%Y-%m-%d') if joined_ts else "Unknown"

    last_active_ts = user.get("last_active", 0)
    last_active = datetime.fromtimestamp(last_active_ts).strftime('%Y-%m-%d %H:%M') if last_active_ts else "Unknown"

    is_prem = user.get("is_premium", False)
    prem_exp = user.get("premium_expiry", 0)
    prem_plan = user.get("premium_plan", "standard")

    prem_status = "🆓 Free Plan"
    if is_prem:
        plan_display = "⭐ Standard" if prem_plan == "standard" else "💎 Deluxe"

        if not prem_exp:
            prem_status = f"{plan_display} (Lifetime)"
        elif prem_exp > time.time():
            dt = datetime.fromtimestamp(prem_exp).strftime('%Y-%m-%d')
            prem_status = f"{plan_display} (Exp: {dt})"
        else:
            prem_status = f"🔴 Expired (was {plan_display})"

    banned = user.get("banned", False)
    status_emoji = "🔴 BANNED" if banned else "🟢 Active"

    usage = await db.get_user_usage(target_id) or {}
    import datetime as dt_module
    current_utc_date = dt_module.datetime.utcnow().strftime('%Y-%m-%d')
    files_today = usage.get("file_count", 0) if usage.get("date") == current_utc_date else 0
    files_alltime = usage.get("file_count_alltime", 0)

    name_display = user.get("first_name") or "Unknown"
    text = (
        f"**👤 User Profile**\n"
        f"{SEPARATOR}\n\n"
        f"<blockquote>"
        f"🆔 ID: `{target_id}`\n"
        f"📛 Name: `{name_display}`\n"
        f"🔗 Username: `{username}`\n"
        f"📅 Joined: `{joined_date}`\n"
        f"⏱ Last active: `{last_active}`\n"
        f"📊 Status: {status_emoji}\n"
        f"💎 Plan: {prem_status}\n"
        f"📈 Files today: `{files_today}` · all-time: `{files_alltime}`"
        f"</blockquote>\n\n"
        f"> Choose an action below."
    )

    markup = []

    # Every action carries the origin suffix so the post-action profile
    # re-render (which reuses callback.data) keeps the list context.
    if banned:
        markup.append([InlineKeyboardButton("🟢 Unban User", callback_data=f"act_unban|{target_id}{origin_suffix}")])
    else:
        markup.append([InlineKeyboardButton("🔴 Ban User", callback_data=f"act_ban|{target_id}{origin_suffix}")])

    if Config.PUBLIC_MODE:
        if is_prem:
            markup.append([
                InlineKeyboardButton("🔄 Extend Plan", callback_data=f"act_add_prem_ask|{target_id}{origin_suffix}"),
                InlineKeyboardButton("❌ Remove Premium", callback_data=f"act_reset_prem|{target_id}{origin_suffix}")
            ])
        else:
            markup.append([
                InlineKeyboardButton("➕ Add Standard", callback_data=f"act_add_prem_ask_standard|{target_id}{origin_suffix}"),
                InlineKeyboardButton("➕ Add Deluxe", callback_data=f"act_add_prem_ask_deluxe|{target_id}{origin_suffix}")
            ])

    markup.append([
        InlineKeyboardButton("🗑️ Reset Today's Quota", callback_data=f"act_reset_quota|{target_id}{origin_suffix}")
    ])

    markup.append([
        InlineKeyboardButton("🗑 Delete Data", callback_data=f"act_del_data_ask|{target_id}{origin_suffix}"),
        InlineKeyboardButton("📄 Export JSON", callback_data=f"act_export_json|{target_id}{origin_suffix}")
    ])

    if origin_mode:
        markup.append([
            InlineKeyboardButton(
                f"← Back to Page {int(origin_page) + 1}",
                callback_data=f"list_users|{origin_mode}|{origin_page}",
            )
        ])
    else:
        markup.append([
            InlineKeyboardButton("← Back to User Management", callback_data="admin_users_menu")
        ])

    await callback.edit_message_text(text, reply_markup=InlineKeyboardMarkup(markup))


@Client.on_callback_query(filters.regex(r"^act_reset_quota\|"))
async def action_reset_quota(client, callback):
    if not is_admin(callback.from_user.id):
        return
    try:
        uid = int(callback.data.split("|")[1])
    except Exception:
        await callback.answer("Invalid User ID", show_alert=True)
        return
    await db.reset_user_quota(uid)
    await db.add_log("reset_quota", callback.from_user.id, f"Reset today's quota for {uid}")
    await callback.answer(f"✅ Today's quota reset for {uid}.", show_alert=True)
    await view_user_profile(client, callback)

@Client.on_callback_query(filters.regex(r"^act_ban\|"))
async def action_ban_user(client, callback):
    uid = int(callback.data.split("|")[1])

    if uid == Config.CEO_ID or uid in Config.ADMIN_IDS:
        await callback.answer("⛔ Cannot ban Admins.", show_alert=True)
        return

    await db.block_user(uid)
    if db.users is not None:
        await db.users.update_one({"user_id": uid}, {"$set": {"banned": True}})
    await db.add_log("ban_user", callback.from_user.id, f"Banned User {uid}")
    await callback.answer(f"🚫 User {uid} BANNED globally.", show_alert=True)
    await view_user_profile(client, callback)

@Client.on_callback_query(filters.regex(r"^act_unban\|"))
async def action_unban_user(client, callback):
    uid = int(callback.data.split("|")[1])
    await db.unblock_user(uid)
    if db.users is not None:
        await db.users.update_one({"user_id": uid}, {"$set": {"banned": False}})
    await db.add_log("unban_user", callback.from_user.id, f"Unbanned User {uid}")
    await callback.answer(f"✅ User {uid} Unbanned.", show_alert=True)
    await view_user_profile(client, callback)

@Client.on_callback_query(filters.regex(r"^act_reset_prem\|"))
async def action_reset_prem(client, callback):
    uid = int(callback.data.split("|")[1])
    await db.reset_user_premium(uid)
    await db.add_log("reset_premium", callback.from_user.id, f"Reset Premium for {uid}")
    await callback.answer("✅ Premium reset to Free.", show_alert=True)
    await view_user_profile(client, callback)

@Client.on_callback_query(filters.regex(r"^act_add_prem_ask(_standard|_deluxe)?\|"))
async def action_add_prem_ask(client, callback):
    if not is_admin(callback.from_user.id):
        await callback.answer("Not authorised.", show_alert=True)
        return

    data = callback.data.split("|")[0]
    try:
        uid = int(callback.data.split("|")[1])
    except (ValueError, IndexError):
        await callback.answer("Invalid target.", show_alert=True)
        return

    # Guard against targeting a user that isn't in the DB yet — the DB-level
    # add_premium_user() silently no-ops on missing docs, which manifests as
    # "bot doesn't react" after the admin types the duration.
    target_doc = await db.get_user(uid)
    if not target_doc:
        await callback.answer(
            "User not found in DB. Ask them to /start the bot first.",
            show_alert=True,
        )
        return

    if data.endswith("_standard"):
        plan = "standard"
    elif data.endswith("_deluxe"):
        plan = "deluxe"
    else:
        plan = target_doc.get("premium_plan", "standard") or "standard"

    origin_mode, origin_page = _parse_origin(callback.data)
    origin_suffix = f"|{origin_mode}|{origin_page}" if origin_mode else ""

    admin_sessions[callback.from_user.id] = {
        "state": "wait_add_prem_days",
        "target_id": uid,
        "plan": plan,
        "msg_id": callback.message.id,
        "origin_suffix": origin_suffix,
    }

    with contextlib.suppress(Exception):
        await callback.message.edit_text(
            f"**➕ Add Premium ({plan.capitalize()}) for User {uid}**\n\n"
            "Enter the duration in **DAYS** (e.g. `30`).\n"
            "__Fractional values like `7.5` are accepted; negatives are rejected.__",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"view_user|{uid}{origin_suffix}")]])
        )
    with contextlib.suppress(Exception):
        await callback.answer()

@Client.on_callback_query(filters.regex(r"^act_del_data_ask\|"))
async def action_del_data_ask(client, callback):
    uid = int(callback.data.split("|")[1])
    origin_mode, origin_page = _parse_origin(callback.data)
    origin_suffix = f"|{origin_mode}|{origin_page}" if origin_mode else ""
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ CONFIRM DELETE", callback_data=f"act_del_data_exec|{uid}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"view_user|{uid}{origin_suffix}")]
    ])
    await callback.edit_message_text(
        f"**⚠️ DELETE USER DATA: {uid}**\n\n"
        "Are you sure? This will wipe history, requests, and settings.\n"
        "It cannot be undone.",
        reply_markup=markup
    )

@Client.on_callback_query(filters.regex(r"^act_del_data_exec\|"))
async def action_del_data_exec(client, callback):
    uid = int(callback.data.split("|")[1])
    if uid == Config.CEO_ID or uid in Config.ADMIN_IDS:
        await callback.answer("⛔ Cannot delete Admin data.", show_alert=True)
        return

    await db.delete_user_data(uid)
    await db.add_log("delete_user", callback.from_user.id, f"Deleted User {uid}")
    await callback.answer("User Deleted.", show_alert=True)
    await admin_users_menu(client, callback)

@Client.on_callback_query(filters.regex(r"^act_export_json\|"))
async def action_export_json(client, callback):
    uid = int(callback.data.split("|")[1])
    user = await db.get_user(uid)

    if not user:
        await callback.answer("User not found.", show_alert=True)
        return

    usage = await db.get_user_usage(uid)
    user['usage_stats'] = usage

    def default_serializer(obj):
        if hasattr(obj, 'isoformat'):
            return obj.isoformat()
        return str(obj)

    json_str = json.dumps(user, indent=4, default=default_serializer)

    bio = io.BytesIO(json_str.encode("utf-8"))
    bio.name = f"user_{uid}.json"

    await callback.message.delete()
    await client.send_document(
        callback.from_user.id,
        document=bio,
        caption=f"📄 **User Data Export:** `{uid}`"
    )

    await show_users_menu(client, callback)


# ---------------------------------------------------------------------------
# Text-input state handlers (registered with text_dispatcher)
# ---------------------------------------------------------------------------
async def _handle_search_query(client, message, state, state_obj, msg_id):
    """Handle wait_search_query state."""
    user_id = message.from_user.id
    query = message.text.strip()
    results = await db.search_users(query)

    if not results:
        await message.reply("❌ No users found.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Try Again", callback_data="admin_user_search_start")],
            [InlineKeyboardButton("❌ Cancel", callback_data="admin_users_menu")]
        ]))
        admin_sessions.pop(user_id, None)
        return

    text = f"**🔍 Search Results: '{query}'**\n\n"
    markup = []
    for u in results[:10]:
        uid = u.get("user_id")
        name = u.get("first_name") or "Unknown"
        name = name[:15]
        uname = f"(@{u.get('username')})" if u.get("username") else ""
        markup.append([InlineKeyboardButton(f"{name} {uname} ({uid})", callback_data=f"view_user|{uid}")])

    markup.append([InlineKeyboardButton("← Back to User Management", callback_data="admin_users_menu")])
    await message.reply(text, reply_markup=InlineKeyboardMarkup(markup))
    admin_sessions.pop(user_id, None)


async def _handle_add_prem_days(client, message, state, state_obj, msg_id):
    """Handle wait_add_prem_days dict state.

    Mirrors the premium-extend / awaiting_premium_* flow: parses the value,
    validates it, runs the DB op, edits the original anchor prompt with the
    result, and always clears the admin session so a follow-up chat message
    doesn't get misrouted to this handler.
    """
    user_id = message.from_user.id
    uid = state_obj.get("target_id")
    plan = state_obj.get("plan", "standard")
    origin_suffix = state_obj.get("origin_suffix", "")
    raw = (message.text or "").strip().replace(",", ".")

    cancel_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data=f"view_user|{uid}{origin_suffix}")]]
    )

    try:
        days = float(raw)
    except ValueError:
        await edit_or_reply(
            client, message, msg_id,
            "❌ Invalid number. Enter days (e.g. `30` or `7.5`).",
            reply_markup=cancel_markup,
        )
        return  # keep session open so they can retry

    if days <= 0:
        await edit_or_reply(
            client, message, msg_id,
            "❌ Duration must be greater than zero. Enter days (e.g. `30`).",
            reply_markup=cancel_markup,
        )
        return

    # Cap at ~100 years to catch fat-finger input that would otherwise push
    # premium_expiry so far into the future it overflows time_t math on
    # hosts with 32-bit time.
    if days > 36500:
        await edit_or_reply(
            client, message, msg_id,
            "❌ Duration too large. Use up to `36500` days.",
            reply_markup=cancel_markup,
        )
        return

    ok = False
    try:
        ok = bool(await db.add_premium_user(uid, days, plan=plan))
    except Exception as exc:
        logger.exception("add_premium_user failed for uid=%s plan=%s", uid, plan)
        await edit_or_reply(
            client, message, msg_id,
            f"❌ Database error while granting premium: `{exc}`",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Back to Profile", callback_data=f"view_user|{uid}{origin_suffix}")]]
            ),
        )
        admin_sessions.pop(user_id, None)
        return

    if not ok:
        await edit_or_reply(
            client, message, msg_id,
            f"❌ User `{uid}` not found in DB. Ask them to /start the bot, "
            "then try again.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Back to Profile", callback_data=f"view_user|{uid}{origin_suffix}")]]
            ),
        )
        admin_sessions.pop(user_id, None)
        return

    with contextlib.suppress(Exception):
        await db.add_log(
            "add_premium", user_id, f"Added {days} days premium ({plan}) to {uid}"
        )

    await edit_or_reply(
        client, message, msg_id,
        f"✅ **Success!**\nUser `{uid}` has received `{days}` days of Premium ({plan}).",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("← Back to Profile", callback_data=f"view_user|{uid}{origin_suffix}")]]
        ),
    )
    admin_sessions.pop(user_id, None)


def _check_add_prem_days(state, state_obj):
    return isinstance(state_obj, dict) and state_obj.get("state") == "wait_add_prem_days"


from plugins.admin.text_dispatcher import register as _register

_register("wait_search_query", _handle_search_query)
_register(_check_add_prem_days, _handle_add_prem_days)


# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# Telegram Channel @XTVbots
# Developed for the 𝕏TV Network @XTVglobal
# Backup Channel @XTVhome
# Contact on Telegram @davdxpx
# --------------------------------------------------------------------------
