# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""plugins.myfiles.core — shared MyFiles helpers, state, and UI builders.

Mode: BOTH (public + non-public). Functions branch on ``Config.PUBLIC_MODE``
internally where behavior differs.

Everything in this module is I/O-agnostic from Pyrogram's perspective: no
``@Client.on_*`` decorators live here, so it can be imported freely from
``handlers.py``, ``extras.py``, and other plugins without triggering
handler registration side effects.
"""

import contextlib
import math

from bson.objectid import ObjectId
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from db import db
from utils.telegram.log import get_logger

logger = get_logger("plugins.myfiles.core")

_mf_debounce: dict[str, float] = {}


def _debounce_mf(user_id: int, callback_id: str) -> bool:
    """Returns True if this callback should be skipped (rapid-fire duplicate)."""
    import time as _t
    key = f"{user_id}:{callback_id}"
    now = _t.time()
    last = _mf_debounce.get(key, 0)
    if now - last < 0.5:
        return True
    _mf_debounce[key] = now
    return False


# === Helper Functions ===

async def safe_edit_or_send(client, callback_query, text, markup, photo=None):
    """
    Handles transition between media messages and text messages gracefully.
    If it's a transition between text <-> media, or different aspect ratio medias,
    it uses a Send-Then-Delete approach.
    """
    try:
        if photo:
            # We are transitioning TO a photo message
            if callback_query.message.photo:
                # Editing photo to photo - wait, actually we are sending photo
                from pyrogram.types import InputMediaPhoto
                try:
                    await callback_query.message.edit_media(
                        media=InputMediaPhoto(photo, caption=text),
                        reply_markup=markup
                    )
                except MessageNotModified:
                    pass
                except Exception:
                    # Send then delete
                    await client.send_photo(chat_id=callback_query.message.chat.id, photo=photo, caption=text, reply_markup=markup)
                    with contextlib.suppress(Exception):
                        await callback_query.message.delete()
            else:
                # Text to Photo: Send new, then delete old
                await client.send_photo(chat_id=callback_query.message.chat.id, photo=photo, caption=text, reply_markup=markup)
                with contextlib.suppress(Exception):
                    await callback_query.message.delete()
        else:
            # We are transitioning TO a text message
            if callback_query.message.photo:
                # Photo to Text: Send new, then delete old
                await client.send_message(chat_id=callback_query.message.chat.id, text=text, reply_markup=markup)
                with contextlib.suppress(Exception):
                    await callback_query.message.delete()
            else:
                # Text to Text: Just edit
                with contextlib.suppress(MessageNotModified):
                    await callback_query.message.edit_text(text, reply_markup=markup)
    except Exception as e:
        logger.error(f"Error in safe_edit_or_send: {e}")


async def get_query_and_title(user_id: int, back_data: str):
    """
    Parses structural view callbacks and returns (filter_query, menu_title).
    Used to DRY up pagination and send-all logic.
    """
    if back_data == "myfiles_main":
        filter_query = {"user_id": user_id} if Config.PUBLIC_MODE else {}
        title = "🕒 **Recent Files**"
    elif back_data.startswith("myfiles_cat_"):
        folder_type = back_data.replace("myfiles_cat_", "")
        filter_query = {"user_id": user_id} if Config.PUBLIC_MODE else {}
        title = f"📁 **{folder_type.capitalize()} Folders**"
    elif back_data.startswith("myfiles_folder_"):
        folder_id = back_data.replace("myfiles_folder_", "")
        filter_query = {"user_id": user_id, "folder_id": ObjectId(folder_id)} if Config.PUBLIC_MODE else {"folder_id": ObjectId(folder_id)}
        folder = await db.folders.find_one({"_id": ObjectId(folder_id)})
        title = f"📁 **{folder['name'] if folder else 'Folder'}**"
    elif back_data.startswith("mf_sea_"):
        parts = back_data.replace("mf_sea_", "").split("_")
        folder_id = parts[0]
        season = parts[1]
        filter_query = {"user_id": user_id, "folder_id": ObjectId(folder_id)} if Config.PUBLIC_MODE else {"folder_id": ObjectId(folder_id)}
        folder = await db.folders.find_one({"_id": ObjectId(folder_id)})

        if season.isdigit():
            season_val = int(season)
            str_season_val = str(season_val)
            zfill_season_val = str_season_val.zfill(2)
            filter_query["$or"] = [
                {"season": season_val},
                {"season": str_season_val},
                {"season": zfill_season_val},
                {"season": f"S{str_season_val}"},
                {"season": f"S{zfill_season_val}"},
                {"season": f"s{str_season_val}"},
                {"season": f"s{zfill_season_val}"},
                {"guess_data.season": season_val},
                {"guess_data.season": {"$in": [season_val]}},
                {"tmdb_data.season": season_val},
                {"tmdb_data.season": str_season_val},
                {"tmdb_data.season": zfill_season_val},
                {"file_name": {"$regex": f"[sS]0?{season_val}\\b"}},
            ]
        else:
            filter_query["$and"] = [
                {"season": {"$exists": False}},
                {"guess_data.season": {"$exists": False}},
                {"tmdb_data.season": {"$exists": False}},
                {"file_name": {"$not": {"$regex": r"[sS]\d{1,2}"}}}
            ]

        title = f"📁 **{folder['name'] if folder else 'Folder'} - Season {season}**"
    else:
        filter_query = {"user_id": user_id} if Config.PUBLIC_MODE else {}
        title = "🕒 **Files**"

    return filter_query, title


async def set_myfiles_state(user_id: int, state_dict: dict):
    if not state_dict:
        await db.users.update_one({"user_id": user_id}, {"$unset": {"myfiles_state": ""}})
    else:
        await db.users.update_one({"user_id": user_id}, {"$set": {"myfiles_state": state_dict}}, upsert=True)


async def get_myfiles_state(user_id: int) -> dict:
    doc = await db.users.find_one({"user_id": user_id})
    if doc and "myfiles_state" in doc:
        return doc["myfiles_state"]
    return {}


async def get_myfiles_main_menu(user_id: int):
    config = await db.get_public_config() if Config.PUBLIC_MODE else await db.settings.find_one({"_id": "global_settings"})

    if Config.PUBLIC_MODE:
        perm_count = await db.files.count_documents({"user_id": user_id, "status": "permanent"})
        temp_count = await db.files.count_documents({"user_id": user_id, "status": "temporary"})

        user_doc = await db.get_user(user_id)
        plan = user_doc.get("premium_plan", "standard") if user_doc and user_doc.get("is_premium") else "free"

        limits = config.get("myfiles_limits", {}).get(plan, {})
        perm_limit = limits.get("permanent_limit", 50)

        limit_str = str(perm_limit) if perm_limit != -1 else "Unlimited"

        plan_emoji = {"free": "🆓", "standard": "⭐", "deluxe": "💎"}.get(plan, "")
        plan_label = f"{plan_emoji} {plan.capitalize()}"

        text = (
            "📁 **MyFiles**\n"
            "__Your personal media vault. Permanent files stick around — temp files vanish after use.__\n\n"
            f"**Plan** · `{plan_label}`\n"
            f"**Permanent Storage** · `{perm_count} / {limit_str}` files\n"
            f"**Temporary Storage** · `{temp_count}` files\n\n"
            "What are you looking for?"
        )
    else:
        perm_count = await db.files.count_documents({"status": "permanent"})
        temp_count = await db.files.count_documents({"status": "temporary"})

        limits = config.get("myfiles_limits", {}).get("global", {})
        perm_limit = limits.get("permanent_limit", -1)

        limit_str = str(perm_limit) if perm_limit != -1 else "Unlimited"

        text = (
            "📁 **Team MyFiles**\n"
            "__Global storage across all users.__\n\n"
            f"**Permanent Storage** · `{perm_count} / {limit_str}` files\n"
            f"**Temporary Storage** · `{temp_count}` files\n\n"
            "Browse by category:"
        )

    has_movies = await db.folders.count_documents({"user_id": user_id, "type": "movies"} if Config.PUBLIC_MODE else {"type": "movies"}) > 0
    has_series = await db.folders.count_documents({"user_id": user_id, "type": "series"} if Config.PUBLIC_MODE else {"type": "series"}) > 0
    has_music = await db.folders.count_documents({"user_id": user_id, "type": "music"} if Config.PUBLIC_MODE else {"type": "music"}) > 0

    buttons = [
        [InlineKeyboardButton("🕒 Recent Files", callback_data="myfiles_cat_recent")],
    ]

    media_row = []
    if has_movies:
        media_row.append(InlineKeyboardButton("🎬 Movies", callback_data="myfiles_cat_movies"))
    if has_series:
        media_row.append(InlineKeyboardButton("📺 Series", callback_data="myfiles_cat_series"))

    if media_row:
        buttons.append(media_row)

    if has_music:
        buttons.append([InlineKeyboardButton("🎵 Music", callback_data="myfiles_cat_music")])

    buttons.append([InlineKeyboardButton("📁 Custom Folders", callback_data="myfiles_cat_custom")])

    # Extras entrypoints — each gated by its own feature toggle, so
    # rows silently vanish when admin disables the feature.
    try:
        from utils.auth.feature_gate import feature_many as _fm
        ent = await _fm(
            [
                "myfiles_trash",
                "myfiles_tags",
                "myfiles_search",
                "myfiles_activity",
                "myfiles_smart",
                "myfiles_insights",
            ],
            user_id,
        )
    except Exception:
        ent = {}

    ent_row: list[InlineKeyboardButton] = []
    if ent.get("myfiles_search"):
        ent_row.append(InlineKeyboardButton("🔎 Search", callback_data="mf_search_start"))
    if ent.get("myfiles_tags"):
        ent_row.append(InlineKeyboardButton("#️⃣ Tags", callback_data="mf_tag_list"))
    if ent_row:
        buttons.append(ent_row)

    ent_row2: list[InlineKeyboardButton] = []
    if ent.get("myfiles_trash"):
        ent_row2.append(InlineKeyboardButton("🗑 Trash", callback_data="mf_trash_list"))
    if ent.get("myfiles_activity"):
        ent_row2.append(InlineKeyboardButton("📊 Activity", callback_data="mf_activity_list"))
    if ent_row2:
        buttons.append(ent_row2)

    if ent.get("myfiles_insights"):
        buttons.append([InlineKeyboardButton("📈 Insights", callback_data="mf_ins_stats")])

    if ent.get("myfiles_smart"):
        buttons.append([InlineKeyboardButton("🧠 Smart Collections", callback_data="mf_smart_list")])

    buttons.append([InlineKeyboardButton("⚙️ Settings", callback_data="myfiles_settings")])

    # Quota header (prepended). Disabled when feature is off for this user.
    try:
        from plugins.myfiles.extras import render_quota_header
        q = await render_quota_header(user_id)
        if q:
            text = text.rstrip() + "\n\n" + q
    except Exception:
        pass

    return text, InlineKeyboardMarkup(buttons)


async def build_files_list_keyboard(user_id: int, filter_query: dict, page: int, limit: int = 10, back_data: str = "myfiles_main"):
    skip = page * limit

    # Check multi-select mode and sorting preference
    state_dict = await get_myfiles_state(user_id)
    multi_select = state_dict.get("multi_select", False)
    selected_files = state_dict.get("selected_files", [])
    sort_order = state_dict.get("sort_order", "newest")

    if sort_order == "oldest":
        sort_tuple = [("created_at", 1)]
    elif sort_order == "a-z":
        sort_tuple = [("file_name", 1)]
    else:
        sort_tuple = [("created_at", -1)]

    cursor = db.files.find(filter_query).sort(sort_tuple).skip(skip).limit(limit)
    files = await cursor.to_list(length=limit)
    total_files = await db.files.count_documents(filter_query)

    buttons = []

    state_dict["current_view"] = back_data
    state_dict["current_page"] = page
    await set_myfiles_state(user_id, state_dict)

    # Sort toggle and multi-select toggle
    sort_label = "↕️ Sort: Newest"
    if sort_order == "oldest":
        sort_label = "↕️ Sort: Oldest"
    elif sort_order == "a-z":
        sort_label = "↕️ Sort: A-Z"

    ms_label = "✅ Multi-Select: ON" if multi_select else "☑️ Multi-Select: OFF"

    buttons.append([
        InlineKeyboardButton(sort_label, callback_data="mf_st"),
        InlineKeyboardButton(ms_label, callback_data="mf_ms")
    ])

    for f in files:
        f_id_str = str(f['_id'])
        name = f.get("file_name", "Unknown File")
        if len(name) > 30:
            name = name[:27] + "..."
        status_emoji = "📌" if f.get("status") == "permanent" else "⏳"

        if multi_select:
            prefix = "🔘 " if f_id_str in selected_files else "⚪️ "
            btn_text = f"{prefix}{name}"
            callback = f"mf_ms_sel_{f_id_str}"
        else:
            btn_text = f"{status_emoji} {name}"
            callback = f"myfiles_file_{f_id_str}"

        buttons.append([InlineKeyboardButton(btn_text, callback_data=callback)])

    nav_row = []
    total_pages = math.ceil(total_files / limit) if total_files > 0 else 1

    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"mf_pg_{page-1}"))
    else:
        nav_row.append(InlineKeyboardButton(" ", callback_data="noop"))

    nav_row.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))

    if skip + limit < total_files:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"mf_pg_{page+1}"))
    else:
        nav_row.append(InlineKeyboardButton(" ", callback_data="noop"))

    buttons.append(nav_row)

    # Action buttons for the folder/category itself
    if multi_select and selected_files:
        buttons.append([
            InlineKeyboardButton(f"📂 Move Selected ({len(selected_files)})", callback_data="mf_ms_mov"),
            InlineKeyboardButton(f"🗑 Delete Selected ({len(selected_files)})", callback_data="mf_ms_del")
        ])
        buttons.append([
            InlineKeyboardButton(f"🔗 Generate Share Link ({len(selected_files)})", callback_data="mf_ms_sha")
        ])
        # MyFiles → Dumb Channel batch send. Only rendered when the user
        # actually has channels configured.
        with contextlib.suppress(Exception):
            if await db.get_dumb_channels(user_id):
                buttons.append([
                    InlineKeyboardButton(
                        f"📡 Send Selected to Channel ({len(selected_files)})",
                        callback_data="mf_send_ch_multi",
                    )
                ])
        # Bulk ops row — vanishes when the toggle is off.
        try:
            from utils.auth.feature_gate import feature_enabled as _fe
            _bulk_on = await _fe("myfiles_bulk", user_id)
        except Exception:
            _bulk_on = False
        if _bulk_on:
            buttons.append([
                InlineKeyboardButton("#️⃣ Tag", callback_data="mf_bulk_tag_start"),
                InlineKeyboardButton("📌 Pin", callback_data="mf_bulk_pin"),
                InlineKeyboardButton("📌🚫 Unpin", callback_data="mf_bulk_unpin"),
            ])
        # Mirror-Leech batch entry — gated on feature_toggles.mirror_leech.
        # We avoid an async DB read inside this keyboard builder for latency
        # and surface the gate in the ml_opt_multi handler itself instead,
        # which simply short-circuits with a friendly alert when disabled.
        buttons.append([
            InlineKeyboardButton(
                f"☁️ Mirror-Leech Selected ({len(selected_files)})",
                callback_data="ml_opt_multi",
            )
        ])

    buttons.append([
        InlineKeyboardButton("📤 Send All", callback_data="mf_sa")
    ])

    buttons.append([InlineKeyboardButton("← Back", callback_data=f"myfiles_leave_{back_data}")])
    return buttons, total_files
