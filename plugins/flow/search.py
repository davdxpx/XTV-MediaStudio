# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""Text-input router + TMDb search + manual title entry.

Every free-form text message in the rename flow lands in
``handle_text_input`` which dispatches by ``state``. The actual search
helpers (``search_handler``) and the manual-title path
(``manual_title_handler``) live alongside because they're only called
from this router. States handled here:

 * awaiting_dest_folder_name
 * awaiting_search_{movie,series}
 * awaiting_manual_title
 * awaiting_general_name (with template validation)
 * awaiting_audio_* (delegated to AudioMetadataEditor)
 * awaiting_watermark_text
 * awaiting_language_custom
 * awaiting_episode_correction_{msg_id}
 * awaiting_season_correction_{msg_id}
 * awaiting_search_correction_{msg_id}

Everything else falls through via ``ContinuePropagation`` so other
plugins can handle their own awaiting_* states (admin Templates, user
Templates, etc.).
"""

import asyncio
import contextlib
import datetime
import re

from bson.objectid import ObjectId
from pyrogram import Client, ContinuePropagation, StopPropagation, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from db import db
from plugins.flow.sessions import (
    _GENERAL_RENAME_FIELDS,
    _persist_session_to_db,
    file_sessions,
)
from tools.AudioMetadataEditor import render_audio_menu
from utils.state import clear_session, get_data, get_state, set_state, update_data
from utils.tasks import spawn as _spawn_task
from utils.telegram.log import get_logger
from utils.template import validate_template
from utils.tmdb import tmdb

logger = get_logger("plugins.flow.search")


async def manual_title_handler(client, message):
    user_id = message.from_user.id
    text = message.text.strip()

    match = re.search(r"^(.*?)(?:\s*\((\d{4})\))?$", text)
    title = match.group(1).strip() if match else text
    year = match.group(2) if match and match.group(2) else ""

    update_data(user_id, "title", title)
    update_data(user_id, "year", year)
    update_data(user_id, "poster", None)

    data = get_data(user_id)
    media_type = data.get("type")

    # Late imports to dodge the destinations ↔ search circular dependency
    # without jumping through forward-ref hoops — both sides use runtime
    # lookups anyway.
    from plugins.flow.destinations import (
        initiate_language_selection,
        prompt_destination_folder,
    )

    if media_type == "series":
        if data.get("is_subtitle"):
            await initiate_language_selection(client, user_id, message)
        else:
            await prompt_destination_folder(client, user_id, message, is_edit=False)
        raise StopPropagation
    elif data.get("personal_type") == "photo":
        set_state(user_id, "awaiting_send_as")
        await message.reply_text(
            f"📸 **Photo Selected**\n\n**Title:** {title}\n**Year:** {year}\n\n"
            "How would you like to receive the output?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🖼 Send as Photo", callback_data="send_as_photo"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "📁 Send as Document (File)",
                            callback_data="send_as_document",
                        )
                    ],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel_rename")],
                ]
            ),
        )
    else:
        await prompt_destination_folder(client, user_id, message, is_edit=False)
        raise StopPropagation


async def search_handler(client, message, media_type):
    from utils.tmdb.gate import ensure_tmdb

    if not await ensure_tmdb(client, message, feature="Manual TMDb search"):
        return

    user_id = message.from_user.id
    query = message.text
    logger.debug(f"Searching {media_type} for: {query}")
    msg = await message.reply_text(f"🔍 Searching for '{query}'...")

    try:
        lang = await db.get_preferred_language(user_id)
        if media_type == "movie":
            results = await tmdb.search_movie(query, language=lang)
        else:
            results = await tmdb.search_tv(query, language=lang)
    except Exception as e:
        logger.error(f"TMDb search failed: {e}")
        with contextlib.suppress(MessageNotModified):
            await msg.edit_text(f"❌ Search Error: {e}")
        return

    if not results:
        with contextlib.suppress(MessageNotModified):
            await msg.edit_text(
                "❌ **No results found.**\n\n"
                "This could be a personal file, home video, or a regional/unknown series not listed on TMDb.\n"
                "You can enter the details manually by clicking below.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✍️ Skip / Enter Manually", callback_data="manual_entry"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "❌ Cancel", callback_data="cancel_rename"
                            )
                        ],
                    ]
                ),
            )
        return

    buttons = []
    for item in results:
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{item['title']} ({item['year']})",
                    callback_data=f"sel_tmdb_{media_type}_{item['id']}",
                )
            ]
        )

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_rename")])

    with contextlib.suppress(MessageNotModified):
        await msg.edit_text(
            f"**Select {media_type.capitalize()}**\n\n"
            f"Found {len(results)} results for '{query}':",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


@Client.on_message(filters.text & filters.private & ~filters.regex(r"^/"), group=5)
async def handle_text_input(client, message):
    user_id = message.from_user.id

    if not Config.PUBLIC_MODE and not (user_id == Config.CEO_ID or user_id in Config.ADMIN_IDS):
        raise ContinuePropagation

    state = get_state(user_id)
    logger.debug(f"Text input from {user_id}: {message.text} | State: {state}")

    if not state:
        raise ContinuePropagation

    # Late imports for the destinations / confirmation-screen modules —
    # both sit downstream of this router but need to be callable from
    # several of the awaiting_* branches below.
    from plugins.flow.destinations import (
        prompt_destination_folder,
        prompt_dumb_channel,
    )

    if state == "awaiting_dest_folder_name":
        folder_name = message.text.strip()
        user_doc = await db.get_user(user_id)
        if Config.PUBLIC_MODE:
            plan = user_doc.get("premium_plan", "standard") if user_doc and user_doc.get("is_premium") else "free"
        else:
            plan = "global"

        config = await db.get_public_config() if Config.PUBLIC_MODE else await db.settings.find_one({"_id": "global_settings"})
        limits = config.get("myfiles_limits", {}).get(plan, {})
        folder_limit = limits.get("folder_limit", 5)

        if folder_limit != -1:
            query_filter = {"user_id": user_id, "type": "custom"} if Config.PUBLIC_MODE else {"type": "custom"}
            count = await db.folders.count_documents(query_filter)
            if count >= folder_limit:
                with contextlib.suppress(Exception):
                    await message.delete()
                msg_id = get_data(user_id).get("dest_msg_id")
                if msg_id:
                    with contextlib.suppress(Exception):
                        await client.edit_message_text(message.chat.id, msg_id, f"❌ You have reached your custom folder limit ({folder_limit}).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back to Options", callback_data="sel_dest_page_1")]]))
                else:
                    await message.reply_text(f"❌ You have reached your custom folder limit ({folder_limit}).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back to Options", callback_data="sel_dest_page_1")]]))
                set_state(user_id, "awaiting_destination_selection")
                return

        folder_id = ObjectId()
        await db.folders.insert_one({
            "_id": folder_id,
            "user_id": user_id,
            "name": folder_name,
            "type": "custom",
            "created_at": datetime.datetime.utcnow()
        })

        with contextlib.suppress(Exception):
            await message.delete()

        msg_id = get_data(user_id).get("dest_msg_id")

        # update destination selection and proceed to dumb channel selection
        update_data(user_id, "dest_folder", str(folder_id))

        if msg_id:
            with contextlib.suppress(Exception):
                await client.edit_message_text(message.chat.id, msg_id, f"✅ Folder **{folder_name}** created successfully and selected!")

            # Wait briefly then show dumb channel selection
            await asyncio.sleep(1.5)

            # Use a dummy object with `.edit_text`
            class DummyMessage:
                async def edit_text(self, text, reply_markup=None):
                    await client.edit_message_text(message.chat.id, msg_id, text, reply_markup=reply_markup)

            await prompt_dumb_channel(client, user_id, DummyMessage(), is_edit=True)
        else:
            msg = await message.reply_text(f"✅ Folder **{folder_name}** created successfully and selected!")
            await asyncio.sleep(1.5)
            await prompt_dumb_channel(client, user_id, msg, is_edit=True)
        raise StopPropagation

    if state == "awaiting_search_movie":
        await search_handler(client, message, "movie")
        raise StopPropagation
    elif state == "awaiting_search_series":
        await search_handler(client, message, "series")
        raise StopPropagation
    elif state == "awaiting_manual_title":
        await manual_title_handler(client, message)
        raise StopPropagation
    elif state == "awaiting_general_name":
        user_id = message.from_user.id
        session_data = get_data(user_id)
        file_msg_id = session_data.get("file_message_id")
        prompt_msg_id = session_data.get("rename_prompt_msg_id")

        valid_reply_ids = [file_msg_id, prompt_msg_id]

        if file_msg_id and (not message.reply_to_message or message.reply_to_message.id not in valid_reply_ids):
            warning_msg = await message.reply_text("⚠️ **Please reply directly to my prompt message** when sending the new name, so I know which file you are renaming.", quote=True)

            async def delete_warning():
                await asyncio.sleep(5)
                try:
                    await warning_msg.delete()
                    await message.delete()
                except Exception:
                    pass
            _spawn_task(delete_warning(), user_id=user_id, label="search_warn_delete")
            return

        new_name = message.text.strip()

        if "{" in new_name or "}" in new_name:
            ok, err = validate_template(new_name, allowed_fields=_GENERAL_RENAME_FIELDS)
            if not ok:
                await message.reply_text(
                    f"❌ **Invalid filename template**\n\n{err}\n\n"
                    f"You sent:\n`{new_name}`\n\n"
                    "Please fix the braces (every `{` needs a matching `}`) "
                    "and send the name again.",
                    quote=True,
                )
                return

        update_data(user_id, "general_name", new_name)

        async def delayed_cleanup():
            await asyncio.sleep(1)
            with contextlib.suppress(Exception):
                await message.delete()
            if prompt_msg_id:
                with contextlib.suppress(Exception):
                    await client.delete_messages(chat_id=user_id, message_ids=prompt_msg_id)
        _spawn_task(delayed_cleanup(), user_id=user_id, label="search_prompt_cleanup")

        await prompt_destination_folder(client, user_id, message, is_edit=False)
        raise StopPropagation

    elif state and state.startswith("awaiting_audio_"):
        action = state.replace("awaiting_audio_", "")

        val = message.text.strip() if getattr(message, "text", None) else ""
        if action == "thumb":
            if val == "-":
                update_data(user_id, "audio_thumb_id", None)
            else:
                await message.reply_text(
                    "Please send a photo for the cover art, or send '-' to clear it."
                )
                return
        else:
            if val == "-":
                val = ""
            update_data(user_id, f"audio_{action}", val)

        set_state(user_id, "awaiting_audio_menu")
        await render_audio_menu(client, message, user_id)
        raise StopPropagation

    elif state == "awaiting_watermark_text":
        user_id = message.from_user.id
        text = message.text.strip()
        update_data(user_id, "watermark_content", text)
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

    elif state == "awaiting_language_custom":
        lang = message.text.strip().lower()
        if len(lang) > 10 or not lang.replace("-", "").isalnum():
            await message.reply_text(
                "Invalid language code. Keep it short (e.g. 'en', 'pt-br')."
            )
            return

        update_data(user_id, "language", lang)
        await prompt_destination_folder(client, user_id, message, is_edit=False)
        raise StopPropagation

    elif state == "awaiting_batch_season":
        from plugins.flow.batch_actions import handle_batch_season_input
        await handle_batch_season_input(client, message)
        raise StopPropagation

    elif state.startswith("awaiting_episode_correction_"):
        from plugins.flow.confirmation_screen import update_confirmation_message
        msg_id = int(state.split("_")[-1])
        if msg_id not in file_sessions:
            await message.reply_text("Session expired. Please start a new session.")
            clear_session(user_id)
            raise StopPropagation
        if message.text.isdigit():
            file_sessions[msg_id]["episode"] = int(message.text)
            set_state(user_id, "awaiting_file_upload")
            _spawn_task(_persist_session_to_db(user_id), user_id=user_id, label="persist_flow_session")
            await update_confirmation_message(client, msg_id, user_id)
            await message.delete()
        else:
            await message.reply_text("Invalid number. Try again.")
        raise StopPropagation

    elif state.startswith("awaiting_season_correction_"):
        from plugins.flow.confirmation_screen import update_confirmation_message
        msg_id = int(state.split("_")[-1])
        if msg_id not in file_sessions:
            await message.reply_text("Session expired. Please start a new session.")
            clear_session(user_id)
            raise StopPropagation
        if message.text.isdigit():
            file_sessions[msg_id]["season"] = int(message.text)
            set_state(user_id, "awaiting_file_upload")
            _spawn_task(_persist_session_to_db(user_id), user_id=user_id, label="persist_flow_session")
            await update_confirmation_message(client, msg_id, user_id)
            await message.delete()
        else:
            await message.reply_text("Invalid number. Try again.")
        raise StopPropagation

    elif state.startswith("awaiting_search_correction_"):
        msg_id = int(state.split("_")[-1])
        if msg_id not in file_sessions:
            await message.reply_text("Session expired. Please start a new session.")
            clear_session(user_id)
            raise StopPropagation
        else:
            fs = file_sessions[msg_id]
            query = message.text
            mtype = fs["type"]

            msg = await message.reply_text(f"🔍 Searching {mtype} for '{query}'...")

            try:
                lang = await db.get_preferred_language(user_id)
                if mtype == "series":
                    results = await tmdb.search_tv(query, language=lang)
                else:
                    results = await tmdb.search_movie(query, language=lang)
            except Exception as e:
                await msg.edit_text(f"Error: {e}")
                raise StopPropagation from e

            if not results:
                with contextlib.suppress(MessageNotModified):
                    await msg.edit_text(
                        "No results found.",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "Back", callback_data=f"back_confirm_{msg_id}"
                                    )
                                ]
                            ]
                        ),
                    )
                raise StopPropagation

            buttons = []
            for item in results:
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"{item['title']} ({item['year']})",
                            callback_data=f"correct_tmdb_{msg_id}_{item['id']}",
                        )
                    ]
                )
            buttons.append(
                [InlineKeyboardButton("Cancel", callback_data=f"back_confirm_{msg_id}")]
            )

            with contextlib.suppress(MessageNotModified):
                await msg.edit_text(
                    f"Select correct {mtype}:",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
            raise StopPropagation
