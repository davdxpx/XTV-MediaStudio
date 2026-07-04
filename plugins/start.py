# --- Imports ---
from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from utils.state import clear_session
from utils.telegram.log import get_logger

logger = get_logger("plugins.start")
logger.info("Loading plugins.start...")

import contextlib

from db import db
from plugins.force_sub_handler import send_starter_setup_message
from plugins.user_setup import perform_smart_swap_if_needed, send_user_tool_preferences_setup, track_tool_usage
from utils.auth import check_force_sub
from utils.auth.gate import check_and_send_welcome, send_force_sub_gate


@Client.on_message(filters.regex(r"^/(start|new)") & filters.private, group=0)

# --- Handlers ---
async def handle_start_command_unique(client, message):
    user_id = message.from_user.id
    logger.debug(f"CMD received: {message.text} from {user_id}")

    command_parts = message.text.split() if message.text else []
    if len(command_parts) > 1:
        param = command_parts[1]

        if param.startswith("share_"):
            # MyFiles share token → resolve from myfiles_shares.
            from pyrogram import StopPropagation
            token = param.replace("share_", "", 1)
            try:
                share = await db.myfiles_resolve_share(token)
                if not share:
                    await message.reply_text(
                        "This share link has expired or is invalid."
                    )
                    raise StopPropagation
                # Expiry / view caps.
                import datetime as _dt
                if share.get("expires_at") and _dt.datetime.utcnow() > share["expires_at"]:
                    await db.myfiles_revoke_share(token)
                    await message.reply_text("This share link has expired.")
                    raise StopPropagation
                max_views = int(share.get("max_views", 0) or 0)
                if max_views and int(share.get("views", 0) or 0) >= max_views:
                    await message.reply_text(
                        "Share link view limit reached."
                    )
                    raise StopPropagation
                # Password check.
                if share.get("password_hash"):
                    # For password-protected links we accept the password as
                    # the third /start argument: "/start share_<token> <pw>".
                    import hashlib
                    parts = (message.text or "").split(None, 2)
                    supplied = parts[2] if len(parts) >= 3 else ""
                    sent_hash = (
                        hashlib.sha256(supplied.encode("utf-8")).hexdigest()
                        if supplied
                        else None
                    )
                    if sent_hash != share["password_hash"]:
                        # Plain text — avoid markdown entity parsing since
                        # the token and prompt-placeholder can trip Telegram's
                        # bounds validator (ENTITY_BOUNDS_INVALID).
                        await message.reply_text(
                            "This share link is password protected.\n\n"
                            "Send the password as part of the link:\n"
                            f"/start share_{token} YOUR_PASSWORD",
                            parse_mode=None,
                        )
                        raise StopPropagation
                # Deliver.
                from bson.objectid import ObjectId
                from pyrogram.errors import PeerIdInvalid
                delivered = 0
                for fid in share.get("target_ids", []):
                    try:
                        oid = ObjectId(fid)
                    except Exception:
                        continue
                    f = await db.files.find_one({"_id": oid})
                    if not f or f.get("is_deleted"):
                        continue
                    try:
                        await client.copy_message(
                            chat_id=user_id,
                            from_chat_id=f["channel_id"],
                            message_id=f["message_id"],
                            protect_content=(share.get("access_mode") == "read"),
                        )
                        delivered += 1
                    except PeerIdInvalid:
                        try:
                            await client.get_chat(f["channel_id"])
                            await client.copy_message(
                                chat_id=user_id,
                                from_chat_id=f["channel_id"],
                                message_id=f["message_id"],
                                protect_content=(share.get("access_mode") == "read"),
                            )
                            delivered += 1
                        except Exception as e:
                            logger.error(f"share: peer fallback failed: {e}")
                    except Exception as e:
                        logger.error(f"share: copy_message failed: {e}")
                # Bump view count + audit.
                try:
                    await db.myfiles_shares.update_one(
                        {"token": token}, {"$inc": {"views": 1}}
                    )
                    await db.log_myfiles_activity(
                        share.get("owner_id"), "shared"
                    )
                except Exception:
                    pass
                await message.reply_text(
                    f"Delivered {delivered} file(s)."
                )
                raise StopPropagation
            except Exception as e:
                if isinstance(e, StopPropagation):
                    raise
                logger.error(f"share_ deep link failed: {e}")
                await message.reply_text(
                    "Could not process this share link."
                )
                raise StopPropagation from e

        if param.startswith("group_"):
            from pyrogram import StopPropagation
            group_id = param.replace("group_", "")
            from bson.objectid import ObjectId
            try:
                group_doc = await db.file_groups.find_one({"group_id": group_id})
                if group_doc:
                    # Check if link has expired
                    import datetime as _dt
                    expires_at = group_doc.get("expires_at")
                    if expires_at and _dt.datetime.utcnow() > expires_at:
                        await message.reply_text("⏳ **Link Expired**\n\nThis share link has expired and is no longer available.")
                        raise StopPropagation

                    if not Config.PUBLIC_MODE:
                        if user_id != Config.CEO_ID and user_id not in Config.ADMIN_IDS:
                            await message.reply_text("❌ Access Denied.")
                            raise StopPropagation
                    else:
                        config = await db.get_public_config()
                        if not await check_force_sub(client, user_id):
                            await send_force_sub_gate(client, message, config)
                            raise StopPropagation

                    file_ids = group_doc.get("files", [])
                    owner_id = group_doc.get("user_id")

                    owner_name = "A user"
                    is_owner_premium = False
                    share_display_name = True

                    if owner_id:
                        owner_doc = await db.get_user(owner_id)
                        if owner_doc:
                            is_owner_premium = owner_doc.get("is_premium", False)

                        owner_settings = await db.get_settings(owner_id)
                        if owner_settings and "share_display_name" in owner_settings:
                            share_display_name = owner_settings["share_display_name"]
                        elif is_owner_premium:
                            # For premium users, disabled by default for privacy reasons
                            share_display_name = False

                        # privacy_hide_username overrides display name
                        if owner_settings and owner_settings.get("privacy_hide_username", False):
                            share_display_name = False

                        if share_display_name and owner_doc:
                            owner_name = owner_doc.get("first_name", "A user")

                        protect = False
                        if owner_settings and "hide_forward_tags" in owner_settings:
                            protect = owner_settings["hide_forward_tags"]

                    else:
                        protect = not is_owner_premium

                    await message.reply_text(f"📦 **Batch File Delivery**\n\nReceiving {len(file_ids)} files shared by: `{owner_name if share_display_name else 'Anonymous'}`")

                    # We could queue this or send them slowly
                    import asyncio

                    from pyrogram.errors import PeerIdInvalid
                    count = 0
                    for fid_str in file_ids:
                        f = await db.files.find_one({"_id": ObjectId(fid_str)})
                        if f:
                            try:
                                await client.copy_message(
                                    chat_id=user_id,
                                    from_chat_id=f["channel_id"],
                                    message_id=f["message_id"],
                                    protect_content=protect
                                )
                                count += 1
                                await asyncio.sleep(0.5) # Anti-flood delay
                            except PeerIdInvalid:
                                try:
                                    await client.get_chat(f["channel_id"])
                                    await client.copy_message(
                                        chat_id=user_id,
                                        from_chat_id=f["channel_id"],
                                        message_id=f["message_id"],
                                        protect_content=protect
                                    )
                                    count += 1
                                    await asyncio.sleep(0.5)
                                except Exception as inner_e:
                                    logger.error(f"Failed to copy group file {fid_str} (Peer fallback failed): {inner_e}")
                            except Exception as e:
                                logger.error(f"Failed to copy group file {fid_str}: {e}")

                    with contextlib.suppress(Exception):
                        await client.send_sticker(user_id, "CAACAgIAAxkBAAEQa0xpgkMvycmQypya3zZxS5rU8tuKBQACwJ0AAjP9EEgYhDgLPnTykDgE")
                    await message.reply_text(f"✅ Delivered {count} files successfully.")

                    raise StopPropagation
            except Exception as e:
                if isinstance(e, StopPropagation):
                    raise
                logger.error(f"Error handling group deep link: {e}")
                pass

        if param.startswith("file_"):
            from pyrogram import StopPropagation
            file_id_str = param.replace("file_", "")
            from bson.objectid import ObjectId
            try:
                f = await db.files.find_one({"_id": ObjectId(file_id_str)})
                if f:
                    if not Config.PUBLIC_MODE:
                        if user_id != Config.CEO_ID and user_id not in Config.ADMIN_IDS:
                            await message.reply_text("❌ Access Denied.")
                            raise StopPropagation
                    else:
                        config = await db.get_public_config()
                        if not await check_force_sub(client, user_id):
                            await send_force_sub_gate(client, message, config)
                            raise StopPropagation

                    owner_id = f.get("user_id")
                    owner_name = "A user"
                    is_owner_premium = False
                    share_display_name = True
                    protect = False

                    if owner_id:
                        owner_doc = await db.get_user(owner_id)
                        if owner_doc:
                            is_owner_premium = owner_doc.get("is_premium", False)
                            owner_name = owner_doc.get("first_name", "A user")

                        owner_settings = await db.get_settings(owner_id)
                        if owner_settings and "share_display_name" in owner_settings:
                            share_display_name = owner_settings["share_display_name"]
                        if owner_settings and owner_settings.get("privacy_hide_username", False):
                            share_display_name = False
                        if owner_settings and "hide_forward_tags" in owner_settings:
                            protect = owner_settings["hide_forward_tags"]

                    if share_display_name and owner_name != "A user":
                        share_text = f"> **{owner_name}** has shared this file with you."
                    else:
                        share_text = "> A file has been shared with you."

                    await message.reply_text(f"📁 **File Received**\n\n{share_text}")

                    from pyrogram.errors import PeerIdInvalid
                    try:
                        await client.copy_message(
                            chat_id=user_id,
                            from_chat_id=f["channel_id"],
                            message_id=f["message_id"],
                            protect_content=protect
                        )
                    except PeerIdInvalid:
                        try:
                            await client.get_chat(f["channel_id"])
                            await client.copy_message(
                                chat_id=user_id,
                                from_chat_id=f["channel_id"],
                                message_id=f["message_id"],
                                protect_content=protect
                            )
                        except Exception as inner_e:
                            logger.error(f"Error serving shared file (Peer fallback failed): {inner_e}")
                            await message.reply_text("❌ The file is currently unavailable because the database channel is not accessible.")
                            raise StopPropagation from inner_e

                    await client.send_sticker(chat_id=user_id, sticker="CAACAgIAAxkBAAEQa0xpgkMvycmQypya3zZxS5rU8tuKBQACwJ0AAjP9EEgYhDgLPnTykDgE")

                    if not is_owner_premium:
                        ad_text = (
                            "> **Rename. Convert. Organize.**\n"
                            "> Process your own media with 𝕏TV MediaStudio™ today!"
                        )
                        await message.reply_text(
                            ad_text,
                            reply_markup=InlineKeyboardMarkup(
                                [[InlineKeyboardButton("🚀 Start Processing", callback_data="start_renaming")]]
                            )
                        )

                    raise StopPropagation
                else:
                    await message.reply_text("❌ File not found.")
                    raise StopPropagation
            except StopPropagation:
                raise
            except Exception as e:
                logger.error(f"Error serving shared file: {e}")
                await message.reply_text("❌ Invalid link or file not found.")
                raise StopPropagation from e

        if param.startswith("pro_setup_"):
            parts = param.split("_")
            tunnel_id_str = parts[2]

            try:
                tunnel_id = int(tunnel_id_str)
                user_settings = await db.get_settings(user_id)
                user_settings["temp_pro_tunnel_id"] = tunnel_id
                await db.settings.update_one({"_id": f"user_{user_id}"}, {"$set": {"temp_pro_tunnel_id": tunnel_id}}, upsert=True)
                await message.reply_text("✅ Detected Pro Setup Tunnel link. Proceed to connect your Userbot using /setup_pro.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Proceed", callback_data="pro_setup_start")]]))
                return
            except Exception:
                pass

    if not Config.PUBLIC_MODE:
        if not (user_id == Config.CEO_ID or user_id in Config.ADMIN_IDS):
            logger.warning(f"Unauthorized access by {user_id}")
            return
        bot_name = "**𝕏TV MediaStudio™**"
        community_name = "official XTV"
    else:
        config = await db.get_public_config()
        if not await check_force_sub(client, user_id):
            await send_force_sub_gate(client, message, config)
            return

        await check_and_send_welcome(client, message, config)

        bot_name = f"**{config.get('bot_name', '𝕏TV MediaStudio™')}**"
        community_name = config.get("community_name", "Our Community")

    await db.get_user_usage(user_id)

    if Config.PUBLIC_MODE:
        has_setup = await db.has_completed_setup(user_id)
        if not has_setup:
            await db.ensure_user(user_id=message.from_user.id, first_name=message.from_user.first_name, username=message.from_user.username, last_name=message.from_user.last_name, language_code=message.from_user.language_code, is_bot=message.from_user.is_bot)
            await send_starter_setup_message(client, user_id, message.from_user.first_name)
            return

        has_completed_preferences = await db.get_setting("has_completed_preferences", default=False, user_id=user_id)
        if not has_completed_preferences:
            await send_user_tool_preferences_setup(client, user_id, message)
            return

    # Trigger smart swap check before rendering menu
    await perform_smart_swap_if_needed(user_id)

    await db.ensure_user(
        user_id=message.from_user.id,
        first_name=message.from_user.first_name,
        username=message.from_user.username,
        last_name=message.from_user.last_name,
        language_code=message.from_user.language_code,
        is_bot=message.from_user.is_bot
    )

    await render_start_menu(client, user_id, first_name=message.from_user.first_name, bot_name=bot_name, community_name=community_name)


async def render_start_menu(client, user_id, message_to_edit=None, first_name="User", bot_name=None, community_name=None):
    """Render the personalized start menu. Edits message_to_edit if provided, otherwise sends new."""
    if not bot_name:
        if Config.PUBLIC_MODE:
            config = await db.get_public_config()
            bot_name = f"**{config.get('bot_name', '𝕏TV MediaStudio™')}**"
            community_name = config.get("community_name", "Our Community")
        else:
            bot_name = "**𝕏TV MediaStudio™**"
            community_name = "official XTV"

    toggles = await db.get_feature_toggles()
    show_other = toggles.get("audio_editor", True) or toggles.get("file_converter", True) or toggles.get("watermarker", True) or toggles.get("subtitle_extractor", True) or toggles.get("youtube_tool", True)

    is_premium_user = False
    plan_display = "Standard"
    status_emoji = "⭐"
    pf = {}

    if Config.PUBLIC_MODE:
        user_doc = await db.get_user(user_id)
        if user_doc and user_doc.get("is_premium"):
            is_premium_user = True
            plan_name = user_doc.get("premium_plan", "standard")
            plan_display = "Deluxe" if plan_name == "deluxe" else "Standard"
            status_emoji = "💎" if plan_name == "deluxe" else "⭐"

            config = await db.get_public_config()
            if config.get("premium_system_enabled", False):
                plan_settings = config.get(f"premium_{plan_name}", {})
                pf = plan_settings.get("features", {})

                if not show_other and (
                    pf.get("audio_editor", True)
                    or pf.get("file_converter", True)
                    or pf.get("watermarker", True)
                    or pf.get("subtitle_extractor", True)
                    or pf.get("youtube_tool", True)
                ):
                    show_other = True

    tool_map = {
        "rename": ("📁 Rename / Tag Media", "start_renaming"),
        "audio_editor": ("🎵 Audio Editor", "audio_editor_menu"),
        "file_converter": ("🔀 File Converter", "file_converter_menu"),
        "watermarker": ("©️ Watermarker", "watermarker_menu"),
        "subtitle_extractor": ("📝 Subtitle Extractor", "subtitle_extractor_menu"),
        "video_trimmer": ("✂️ Video Trimmer", "video_trimmer_menu"),
        "media_info": ("ℹ️ Media Info", "media_info_menu"),
        "voice_converter": ("🎙️ Voice Converter", "voice_converter_menu"),
        "video_note_converter": ("⭕ Video Note Converter", "video_note_menu"),
        "youtube_tool": ("▶️ YouTube Tool", "youtube_tool_menu")
    }

    user_settings = await db.get_settings(user_id)
    selected_tools = user_settings.get("start_menu_tools", ["rename"]) if user_settings else ["rename"]

    buttons = []
    for tool_id in selected_tools:
        if tool_id in tool_map:
            is_avail = tool_id == "rename"
            if not is_avail:
                is_avail = toggles.get(tool_id, True)
                if Config.PUBLIC_MODE and is_premium_user:
                    is_avail = is_avail or pf.get(tool_id, False)
            if is_avail:
                buttons.append([InlineKeyboardButton(tool_map[tool_id][0], callback_data=tool_map[tool_id][1])])

    all_avail_ids = ["rename"]
    for t_id in tool_map:
        if t_id == "rename":
            continue
        if toggles.get(t_id, True) and (pf.get(t_id, True) if pf else True):
            all_avail_ids.append(t_id)

    unselected_tools = [t for t in all_avail_ids if t not in selected_tools]
    if unselected_tools:
        buttons.append([InlineKeyboardButton("✨ Other Features", callback_data="other_features_menu")])

    # MyFiles is the bot's library — surface it right on the start menu
    # instead of hiding it behind /myfiles. Admins always see it.
    try:
        _mf_on = await db.get_setting("myfiles_enabled", default=False)
        if _mf_on or user_id == Config.CEO_ID or user_id in Config.ADMIN_IDS:
            buttons.append([InlineKeyboardButton("📂 MyFiles Library", callback_data="myfiles_main")])
    except Exception:
        pass

    if Config.PUBLIC_MODE and is_premium_user:
        buttons.append([InlineKeyboardButton("💎 Premium Dashboard", callback_data="user_premium_menu")])
    buttons.append([InlineKeyboardButton("📖 Help & Guide", callback_data="help_guide")])

    if is_premium_user:
        text = (
            f"{status_emoji} **Welcome back, {first_name}!** {status_emoji}\n\n"
            f"> Your **Premium {plan_display}** status is Active ✅\n\n"
            f"I am {bot_name}, your advanced media processing engine by the {community_name}.\n\n"
            f"**Quick Actions:**\n"
            f"• Send me any media file to begin priority processing\n"
            f"• Explore your premium tools in the dashboard below\n\n"
            f"Thank you for being a valued Premium member!"
        )
    else:
        text = (
            f"{bot_name}\n\n"
            f"Welcome to the {community_name} media processing and management bot.\n"
            f"This bot provides professional tools to organize and modify your files.\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 **Tip:** You don't need to click anything to begin!\n"
            f"Simply send or forward a file directly to me, and I will auto-detect the details.\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Click below to start manually or to view the guide."
        )

    markup = InlineKeyboardMarkup(buttons)
    if message_to_edit:
        try:
            await message_to_edit.edit_text(text, reply_markup=markup)
        except MessageNotModified:
            pass
        except Exception:
            await client.send_message(user_id, text, reply_markup=markup)
    else:
        await client.send_message(user_id, text, reply_markup=markup)

@Client.on_message(filters.command(["r", "rename"]) & filters.private, group=0)
async def handle_rename_command(client, message):
    user_id = message.from_user.id
    from plugins.flow import handle_start_renaming
    await track_tool_usage(user_id, "rename")

    class MockCallbackQuery:
        def __init__(self, message):
            self.message = message
            self.from_user = message.from_user
            self.data = "start_renaming"

        async def answer(self, *args, **kwargs):
            pass

    mock_cb = MockCallbackQuery(message)
    msg = await message.reply_text("Loading menu...")
    mock_cb.message = msg
    await handle_start_renaming(client, mock_cb)

@Client.on_message(filters.command(["g", "general"]) & filters.private, group=0)
async def handle_general_command(client, message):
    from plugins.flow import handle_type_general

    class MockCallbackQuery:
        def __init__(self, message):
            self.message = message
            self.from_user = message.from_user
            self.data = "type_general"

        async def answer(self, *args, **kwargs):
            pass

    mock_cb = MockCallbackQuery(message)
    msg = await message.reply_text("Loading general mode...")
    mock_cb.message = msg
    await handle_type_general(client, mock_cb)

@Client.on_message(filters.command(["a", "audio"]) & filters.private, group=0)
async def handle_audio_command(client, message):
    user_id = message.from_user.id

    toggles = await db.get_feature_toggles()
    allowed = toggles.get("audio_editor", True)

    if Config.PUBLIC_MODE and not allowed:
        user_doc = await db.get_user(user_id)
        if user_doc and user_doc.get("is_premium"):
            plan_name = user_doc.get("premium_plan", "standard")
            config = await db.get_public_config()
            if config.get("premium_system_enabled", False):
                plan_settings = config.get(f"premium_{plan_name}", {})
                if plan_settings.get("features", {}).get("audio_editor", False):
                    allowed = True

    if not allowed:
        await message.reply_text("❌ This feature is currently disabled by the Admin.")
        return

    from tools.AudioMetadataEditor import handle_audio_editor_menu
    await track_tool_usage(user_id, "audio_editor")

    class MockCallbackQuery:
        def __init__(self, message):
            self.message = message
            self.from_user = message.from_user
            self.data = "audio_editor_menu"

        async def answer(self, *args, **kwargs):
            pass

    mock_cb = MockCallbackQuery(message)
    msg = await message.reply_text("Loading audio editor...")
    mock_cb.message = msg
    await handle_audio_editor_menu(client, mock_cb)


@Client.on_message(filters.command(["yt", "youtube"]) & filters.private, group=0)
async def handle_youtube_command(client, message):
    user_id = message.from_user.id

    toggles = await db.get_feature_toggles()
    allowed = toggles.get("youtube_tool", True)

    if Config.PUBLIC_MODE and not allowed:
        user_doc = await db.get_user(user_id)
        if user_doc and user_doc.get("is_premium"):
            plan_name = user_doc.get("premium_plan", "standard")
            config = await db.get_public_config()
            if config.get("premium_system_enabled", False):
                plan_settings = config.get(f"premium_{plan_name}", {})
                if plan_settings.get("features", {}).get("youtube_tool", False):
                    allowed = True

    if not allowed:
        await message.reply_text("❌ This feature is currently disabled by the Admin.")
        return

    from tools.YouTubeTool import handle_youtube_tool_menu
    await track_tool_usage(user_id, "youtube_tool")

    class MockCallbackQuery:
        def __init__(self, message):
            self.message = message
            self.from_user = message.from_user
            self.data = "youtube_tool_menu"

        async def answer(self, *args, **kwargs):
            pass

    mock_cb = MockCallbackQuery(message)
    msg = await message.reply_text("Loading YouTube tool...")
    mock_cb.message = msg
    await handle_youtube_tool_menu(client, mock_cb)

@Client.on_message(filters.command(["p", "personal"]) & filters.private, group=0)
async def handle_personal_command(client, message):
    from plugins.flow import handle_type_personal

    class MockCallbackQuery:
        def __init__(self, message):
            self.message = message
            self.from_user = message.from_user
            self.data = "type_personal_file"

        async def answer(self, *args, **kwargs):
            pass

    mock_cb = MockCallbackQuery(message)
    msg = await message.reply_text("Loading personal mode...")
    mock_cb.message = msg
    await handle_type_personal(client, mock_cb)

@Client.on_message(filters.command(["c", "convert"]) & filters.private, group=0)
async def handle_convert_command(client, message):
    user_id = message.from_user.id

    toggles = await db.get_feature_toggles()
    allowed = toggles.get("file_converter", True)

    if Config.PUBLIC_MODE and not allowed:
        user_doc = await db.get_user(user_id)
        if user_doc and user_doc.get("is_premium"):
            plan_name = user_doc.get("premium_plan", "standard")
            config = await db.get_public_config()
            if config.get("premium_system_enabled", False):
                plan_settings = config.get(f"premium_{plan_name}", {})
                if plan_settings.get("features", {}).get("file_converter", False):
                    allowed = True

    if not allowed:
        await message.reply_text("❌ This feature is currently disabled by the Admin.")
        return

    from tools.FileConverter import handle_file_converter_menu
    await track_tool_usage(user_id, "file_converter")

    class MockCallbackQuery:
        def __init__(self, message):
            self.message = message
            self.from_user = message.from_user
            self.data = "file_converter_menu"

        async def answer(self, *args, **kwargs):
            pass

    mock_cb = MockCallbackQuery(message)
    msg = await message.reply_text("Loading converter...")
    mock_cb.message = msg
    await handle_file_converter_menu(client, mock_cb)

@Client.on_message(filters.command(["w", "watermark"]) & filters.private, group=0)
async def handle_watermark_command(client, message):
    user_id = message.from_user.id

    toggles = await db.get_feature_toggles()
    allowed = toggles.get("watermarker", True)

    if Config.PUBLIC_MODE and not allowed:
        user_doc = await db.get_user(user_id)
        if user_doc and user_doc.get("is_premium"):
            plan_name = user_doc.get("premium_plan", "standard")
            config = await db.get_public_config()
            if config.get("premium_system_enabled", False):
                plan_settings = config.get(f"premium_{plan_name}", {})
                if plan_settings.get("features", {}).get("watermarker", False):
                    allowed = True

    if not allowed:
        await message.reply_text("❌ This feature is currently disabled by the Admin.")
        return

    from tools.ImageWatermarker import handle_watermarker_menu
    await track_tool_usage(user_id, "watermarker")

    class MockCallbackQuery:
        def __init__(self, message):
            self.message = message
            self.from_user = message.from_user
            self.data = "watermarker_menu"

        async def answer(self, *args, **kwargs):
            pass

    mock_cb = MockCallbackQuery(message)
    msg = await message.reply_text("Loading watermarker...")
    mock_cb.message = msg
    await handle_watermarker_menu(client, mock_cb)

@Client.on_message(filters.command(["s", "subtitle"]) & filters.private, group=0)
async def handle_subtitle_command(client, message):
    user_id = message.from_user.id

    toggles = await db.get_feature_toggles()
    allowed = toggles.get("subtitle_extractor", True)

    if Config.PUBLIC_MODE and not allowed:
        user_doc = await db.get_user(user_id)
        if user_doc and user_doc.get("is_premium"):
            plan_name = user_doc.get("premium_plan", "standard")
            config = await db.get_public_config()
            if config.get("premium_system_enabled", False):
                plan_settings = config.get(f"premium_{plan_name}", {})
                if plan_settings.get("features", {}).get("subtitle_extractor", False):
                    allowed = True

    if not allowed:
        await message.reply_text("❌ This feature is currently disabled by the Admin.")
        return

    from tools.SubtitleExtractor import handle_subtitle_extractor_menu
    await track_tool_usage(user_id, "subtitle_extractor")

    class MockCallbackQuery:
        def __init__(self, message):
            self.message = message
            self.from_user = message.from_user
            self.data = "subtitle_extractor_menu"

        async def answer(self, *args, **kwargs):
            pass

    mock_cb = MockCallbackQuery(message)
    msg = await message.reply_text("Loading subtitle extractor...")
    mock_cb.message = msg
    await handle_subtitle_extractor_menu(client, mock_cb)

@Client.on_message(filters.command(["t", "trim"]) & filters.private, group=0)
async def handle_trim_command(client, message):
    user_id = message.from_user.id

    toggles = await db.get_feature_toggles()
    allowed = toggles.get("video_trimmer", True)

    if Config.PUBLIC_MODE and not allowed:
        user_doc = await db.get_user(user_id)
        if user_doc and user_doc.get("is_premium"):
            plan_name = user_doc.get("premium_plan", "standard")
            config = await db.get_public_config()
            if config.get("premium_system_enabled", False):
                plan_settings = config.get(f"premium_{plan_name}", {})
                if plan_settings.get("features", {}).get("video_trimmer", False):
                    allowed = True

    if not allowed:
        await message.reply_text("❌ This feature is currently disabled by the Admin.")
        return

    from tools.VideoTrimmer import handle_video_trimmer_menu
    await track_tool_usage(user_id, "video_trimmer")

    class MockCallbackQuery:
        def __init__(self, message):
            self.message = message
            self.from_user = message.from_user
            self.data = "video_trimmer_menu"

        async def answer(self, *args, **kwargs):
            pass

    mock_cb = MockCallbackQuery(message)
    msg = await message.reply_text("Loading video trimmer...")
    mock_cb.message = msg
    await handle_video_trimmer_menu(client, mock_cb)

@Client.on_message(filters.command(["mi", "mediainfo"]) & filters.private, group=0)
async def handle_mediainfo_command(client, message):
    user_id = message.from_user.id

    toggles = await db.get_feature_toggles()
    allowed = toggles.get("media_info", True)

    if Config.PUBLIC_MODE and not allowed:
        user_doc = await db.get_user(user_id)
        if user_doc and user_doc.get("is_premium"):
            plan_name = user_doc.get("premium_plan", "standard")
            config = await db.get_public_config()
            if config.get("premium_system_enabled", False):
                plan_settings = config.get(f"premium_{plan_name}", {})
                if plan_settings.get("features", {}).get("media_info", False):
                    allowed = True

    if not allowed:
        await message.reply_text("❌ This feature is currently disabled by the Admin.")
        return

    from tools.MediaInfo import handle_media_info_menu
    await track_tool_usage(user_id, "media_info")

    class MockCallbackQuery:
        def __init__(self, message):
            self.message = message
            self.from_user = message.from_user
            self.data = "media_info_menu"

        async def answer(self, *args, **kwargs):
            pass

    mock_cb = MockCallbackQuery(message)
    msg = await message.reply_text("Loading media info...")
    mock_cb.message = msg
    await handle_media_info_menu(client, mock_cb)

@Client.on_message(filters.command(["v", "voice"]) & filters.private, group=0)
async def handle_voice_command(client, message):
    user_id = message.from_user.id

    toggles = await db.get_feature_toggles()
    allowed = toggles.get("voice_converter", True)

    if Config.PUBLIC_MODE and not allowed:
        user_doc = await db.get_user(user_id)
        if user_doc and user_doc.get("is_premium"):
            plan_name = user_doc.get("premium_plan", "standard")
            config = await db.get_public_config()
            if config.get("premium_system_enabled", False):
                plan_settings = config.get(f"premium_{plan_name}", {})
                if plan_settings.get("features", {}).get("voice_converter", False):
                    allowed = True

    if not allowed:
        await message.reply_text("❌ This feature is currently disabled by the Admin.")
        return

    from tools.VoiceNoteConverter import handle_voice_converter_menu
    await track_tool_usage(user_id, "voice_converter")

    class MockCallbackQuery:
        def __init__(self, message):
            self.message = message
            self.from_user = message.from_user
            self.data = "voice_converter_menu"

        async def answer(self, *args, **kwargs):
            pass

    mock_cb = MockCallbackQuery(message)
    msg = await message.reply_text("Loading voice converter...")
    mock_cb.message = msg
    await handle_voice_converter_menu(client, mock_cb)

@Client.on_message(filters.command(["vn", "videonote"]) & filters.private, group=0)
async def handle_videonote_command(client, message):
    user_id = message.from_user.id

    toggles = await db.get_feature_toggles()
    allowed = toggles.get("video_note_converter", True)

    if Config.PUBLIC_MODE and not allowed:
        user_doc = await db.get_user(user_id)
        if user_doc and user_doc.get("is_premium"):
            plan_name = user_doc.get("premium_plan", "standard")
            config = await db.get_public_config()
            if config.get("premium_system_enabled", False):
                plan_settings = config.get(f"premium_{plan_name}", {})
                if plan_settings.get("features", {}).get("video_note_converter", False):
                    allowed = True

    if not allowed:
        await message.reply_text("❌ This feature is currently disabled by the Admin.")
        return

    from tools.VideoNoteConverter import handle_video_note_menu
    await track_tool_usage(user_id, "video_note_converter")

    class MockCallbackQuery:
        def __init__(self, message):
            self.message = message
            self.from_user = message.from_user
            self.data = "video_note_menu"

        async def answer(self, *args, **kwargs):
            pass

    mock_cb = MockCallbackQuery(message)
    msg = await message.reply_text("Loading video note converter...")
    mock_cb.message = msg
    await handle_video_note_menu(client, mock_cb)


@Client.on_message(filters.command("end") & filters.private, group=0)
async def handle_end_command_unique(client, message):
    user_id = message.from_user.id
    logger.debug(f"CMD received: {message.text} from {user_id}")
    clear_session(user_id)
    toggles = await db.get_feature_toggles()
    show_other = toggles.get("audio_editor", True) or toggles.get("file_converter", True) or toggles.get("watermarker", True) or toggles.get("subtitle_extractor", True) or toggles.get("youtube_tool", True)

    pf = {}
    if Config.PUBLIC_MODE:
        user_doc = await db.get_user(user_id)
        if user_doc and user_doc.get("is_premium"):
            plan_name = user_doc.get("premium_plan", "standard")
            config = await db.get_public_config()
            if config.get("premium_system_enabled", False):
                plan_settings = config.get(f"premium_{plan_name}", {})
                pf = plan_settings.get("features", {})
                if not show_other and (
                    pf.get("audio_editor", True)
                    or pf.get("file_converter", True)
                    or pf.get("watermarker", True)
                    or pf.get("subtitle_extractor", True)
                    or pf.get("youtube_tool", True)
                ):
                    show_other = True

    buttons = [
        [InlineKeyboardButton("🎬 Start Renaming Manually", callback_data="start_renaming")]
    ]
    if show_other:
        buttons.append([InlineKeyboardButton("✨ Other Features", callback_data="other_features_menu")])
    buttons.append([InlineKeyboardButton("📖 Help & Guide", callback_data="help_guide")])

    await message.reply_text(
        "**Current Task Cancelled** ❌\n\n"
        "Your progress has been cleared.\n"
        "You can simply send me a file anytime to start over, or use the buttons below.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@Client.on_callback_query(filters.regex(r"^other_features_menu$"))
async def handle_other_features_menu(client, callback_query):
    await callback_query.answer()
    toggles = await db.get_feature_toggles()
    user_id = callback_query.from_user.id

    pf = {}
    if Config.PUBLIC_MODE:
        user_doc = await db.get_user(user_id)
        if user_doc and user_doc.get("is_premium"):
            plan_name = user_doc.get("premium_plan", "standard")
            config = await db.get_public_config()
            if config.get("premium_system_enabled", False):
                plan_settings = config.get(f"premium_{plan_name}", {})
                pf = plan_settings.get("features", {})

    user_settings = await db.get_settings(user_id)
    selected_tools = user_settings.get("start_menu_tools", ["rename"]) if user_settings else ["rename"]

    tool_map = {
        "rename": ("📁 Rename / Tag Media", "start_renaming"),
        "audio_editor": ("🎵 Audio Editor", "audio_editor_menu"),
        "file_converter": ("🔀 File Converter", "file_converter_menu"),
        "watermarker": ("©️ Watermarker", "watermarker_menu"),
        "subtitle_extractor": ("📝 Subtitle Extractor", "subtitle_extractor_menu"),
        "video_trimmer": ("✂️ Video Trimmer", "video_trimmer_menu"),
        "media_info": ("ℹ️ Media Info", "media_info_menu"),
        "voice_converter": ("🎙️ Voice Converter", "voice_converter_menu"),
        "video_note_converter": ("⭕ Video Note Converter", "video_note_menu"),
        "youtube_tool": ("▶️ YouTube Tool", "youtube_tool_menu")
    }

    buttons = []

    # Render unselected tools in Other Features
    for t_id in tool_map:
        if t_id in selected_tools:
            continue  # These are on the main page

        is_avail = t_id == "rename"
        if not is_avail:
            is_avail = toggles.get(t_id, True) and (pf.get(t_id, True) if pf else True)

        if is_avail:
            buttons.append([InlineKeyboardButton(tool_map[t_id][0], callback_data=tool_map[t_id][1])])


    buttons.append([InlineKeyboardButton("❌ Close", callback_data="help_close")])

    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            "✨ **Media Tools**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "> Select a tool from the list below:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# Telegram Channel @XTVbots
# Developed for the 𝕏TV Network @XTVglobal
# Backup Channel @XTVhome
# Contact on Telegram @davdxpx
# --------------------------------------------------------------------------
