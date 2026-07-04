"""
╔══════════════════════════════════════════════════════════════════════════╗
║                    Developed by 𝕏0L0™ (@davdxpx)                         ║
║     © 2026 XTV Network Global. All Rights Reserved.                      ║
║                                                                          ║
║  Project: 𝕏TV MediaStudio™                                                 ║
║  Author: 𝕏0L0™                                                           ║
║  Telegram: @davdxpx                                                      ║
║  Channel: @XTVbots                                                       ║
║  Network: @XTVglobal                                                     ║
║  Backup: @XTVhome                                                        ║
║                                                                          ║
║  WARNING: This code is the intellectual property of XTV Network.         ║
║  Unauthorized modification, redistribution, or removal of this credit    ║
║  is strictly prohibited. Forking and simple usage is allowed under       ║
║  the terms of the license.                                               ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

# --- Imports ---
import asyncio
import datetime
import os
import time

from pyrogram import Client, idle

from config import Config
from utils.tasks import spawn
from utils.telegram.log import get_logger

logger = get_logger("main")

app = Client(
    "xtv_mediastudio",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN,
    workers=50,
    max_concurrent_transmissions=10,
    plugins=dict(root="plugins"),
)

# Load additional tools explicitly since they are in a different directory
# The plugins dict usually only loads from one root. To ensure tools are registered,
# we import them here before the app starts.
import contextlib

import tools.AudioMetadataEditor
import tools.FileConverter
import tools.ImageWatermarker
import tools.MediaInfo

# Mirror-Leech registers its downloaders / uploaders on import so the
# registries are populated before the plugin's handlers fire.
import tools.mirror_leech  # noqa: F401
import tools.SubtitleExtractor
import tools.VideoNoteConverter
import tools.VideoTrimmer
import tools.VoiceNoteConverter
import tools.YouTubeTool


def register_tool_handlers(client, module):
    for name in dir(module):
        obj = getattr(module, name)
        # Pyrogram stores handlers as a list of tuples (handler, group) on the decorated function itself.
        if callable(obj) and hasattr(obj, "handlers") and isinstance(obj.handlers, list):
            for item in obj.handlers:
                if isinstance(item, tuple) and len(item) == 2:
                    handler, group = item
                    client.add_handler(handler, group)

register_tool_handlers(app, tools.FileConverter)
register_tool_handlers(app, tools.AudioMetadataEditor)
register_tool_handlers(app, tools.ImageWatermarker)
register_tool_handlers(app, tools.SubtitleExtractor)
register_tool_handlers(app, tools.VideoTrimmer)
register_tool_handlers(app, tools.MediaInfo)
register_tool_handlers(app, tools.VoiceNoteConverter)
register_tool_handlers(app, tools.VideoNoteConverter)
register_tool_handlers(app, tools.YouTubeTool)

user_bot = None


def _sync_cleanup_orphaned_files():
    """Synchronous file cleanup — run via asyncio.to_thread to avoid blocking."""
    download_dir = Config.DOWNLOAD_DIR
    if not os.path.exists(download_dir):
        return 0, 0

    now = time.time()
    cutoff = now - (24 * 3600)  # 24 hours
    cleaned_count = 0
    freed_space = 0

    for root, _, files in os.walk(download_dir):
        for f in files:
            if f == "thumb.jpg":
                continue
            file_path = os.path.join(root, f)
            try:
                mtime = os.path.getmtime(file_path)
                if mtime < cutoff:
                    size = os.path.getsize(file_path)
                    os.remove(file_path)
                    cleaned_count += 1
                    freed_space += size
            except OSError as e:
                logger.warning(f"Error cleaning file {file_path}: {e}")

    return cleaned_count, freed_space


if __name__ == "__main__":
    if not Config.BOT_TOKEN:
        logger.error("BOT_TOKEN is not set!")
        exit(1)

    logger.info("Starting 𝕏TV MediaStudio™...")
    app.start()

    # --- Database migrations ---
    # mediastudio_layout is idempotent and self-advisory-locked. Failure
    # here must be fatal: booting half-migrated would let the shim route
    # writes into an incomplete layout and silently corrupt state.
    try:
        from db import db
        from db.migrations.mediastudio_layout import run_mediastudio_layout_migration

        logger.info("Running DB migrations (mediastudio_layout)...")
        app.loop.run_until_complete(
            run_mediastudio_layout_migration(
                db.db, public_mode=Config.PUBLIC_MODE, ceo_id=Config.CEO_ID
            )
        )
    except Exception as e:
        logger.exception("Fatal: mediastudio_layout migration failed; aborting startup")
        raise SystemExit(1) from e

    # --- Database indexes ---
    # Indexes are also re-ensured by the migration, but we run once more
    # here so a fresh deployment against an already-migrated DB still
    # provisions them on boot.
    try:
        from db import db
        from db.migrations.mediastudio_layout import ensure_indexes_v2

        logger.info("Ensuring database indexes...")
        app.loop.run_until_complete(ensure_indexes_v2(db.db))
    except Exception as e:
        logger.warning(f"Error creating indexes: {e}")

    # --- MyFiles extras v1 migration (idempotent) ---
    # Creates audit / activity / quotas / shares collections + indexes,
    # backfills is_deleted/tags/parent_folder_id defaults, and recomputes
    # user quotas. Safe to run on every boot.
    try:
        from db import db
        from db.migrations.myfiles_extras_v1 import run_myfiles_extras_v1

        logger.info("Running DB migration: myfiles_extras_v1 ...")
        app.loop.run_until_complete(run_myfiles_extras_v1(db))
    except Exception as e:
        logger.warning(f"myfiles_extras_v1 migration issue: {e}")

    # --- Consolidate stray user_* settings docs into global (non-public only) ---
    # Fixes admin panel showing an empty list of dumb channels while the
    # rename flow still has them: without this, two different docs coexist.
    try:
        from db import db
        from db.migrations.consolidate_nonpublic_settings import (
            run_consolidate_nonpublic_settings,
        )

        logger.info("Running DB migration: consolidate_nonpublic_settings ...")
        app.loop.run_until_complete(run_consolidate_nonpublic_settings(db))
    except Exception as e:
        logger.warning(f"consolidate_nonpublic_settings migration issue: {e}")

    # --- Rescue legacy raw settings docs (one-time, advisory-locked) ---
    # Drains data parked at raw `_id: "global_settings"` / "public_mode_config"
    # / "user_<uid>" documents inside MediaStudio-Settings through the shim's
    # routing (per-concern docs + CEO personal_settings) and removes the raw
    # docs. Without this, that data was invisible to the virtual-doc reads
    # and the bot appeared to lose thumbnails / dumb channels / templates on
    # every redeploy. Takes a full settings backup before touching anything.
    try:
        from db import db
        from db.migrations.rescue_legacy_settings import run_rescue_legacy_settings

        logger.info("Running DB migration: rescue_legacy_settings ...")
        app.loop.run_until_complete(run_rescue_legacy_settings(db))
    except Exception as e:
        logger.warning(f"rescue_legacy_settings migration issue: {e}")

    # --- Move per-user usage into MediaStudio-usage collection ---
    # See db/migrations/usage_collection_v1.py. Creates indexes, backfills
    # per-day + alltime + daily-global docs from legacy user.usage subdocs,
    # then unsets the legacy subdocs. Runs AFTER rescue_legacy_settings
    # when PR #373 is also deployed, otherwise as a standalone migration.
    try:
        from db import db
        from db.migrations.usage_collection_v1 import run_usage_collection_v1

        logger.info("Running DB migration: usage_collection_v1 ...")
        app.loop.run_until_complete(run_usage_collection_v1(db))
    except Exception as e:
        logger.warning(f"usage_collection_v1 migration issue: {e}")


    # --- Restore YouTube cookies from DB (survives container redeploys) ---
    try:
        from tools.YouTubeTool import restore_youtube_cookies_from_db
        logger.info("Restoring YouTube cookies from DB if needed...")
        restored = app.loop.run_until_complete(restore_youtube_cookies_from_db())
        if restored:
            logger.info("YouTube cookies restored from MongoDB.")
    except Exception as e:
        logger.warning(f"Error restoring YouTube cookies from DB: {e}")

    # --- Channel peer caching ---
    try:
        from db import db

        async def cache_channels():
            links = await db.get_all_dumb_channel_links()
            tasks = []

            async def cache_link(link):
                # Bots cannot resolve t.me/+… invite links (CheckChatInvite
                # is user-only → BOT_METHOD_INVALID), so don't even try —
                # the channel gets cached via its numeric id elsewhere.
                if isinstance(link, str) and ("/+" in link or "joinchat" in link):
                    logger.debug(f"[peer-cache] skipping invite link '{link}' (bots cannot resolve)")
                    return
                try:
                    await app.get_chat(link)
                except Exception as e:
                    # Non-fatal: peer may be private / link stale. Log so ops
                    # can investigate; downstream sends will surface the issue
                    # as PeerIdInvalid if this channel is actually needed.
                    logger.warning(f"[peer-cache] failed to cache link '{link}': {e}")

            for link in links:
                tasks.append(cache_link(link))

            config = await db.get_public_config() if Config.PUBLIC_MODE else await db.settings.find_one({"_id": "global_settings"})
            force_sub_channels = config.get("force_sub_channels", []) if config else []
            legacy_ch = config.get("force_sub_channel") if config else None

            async def cache_id(ch_id):
                try:
                    await app.get_chat(ch_id)
                except Exception as e:
                    logger.warning(f"[peer-cache] failed to cache id {ch_id}: {e}")

            if force_sub_channels:
                for ch in force_sub_channels:
                    if ch.get("id"):
                        tasks.append(cache_id(ch["id"]))
            elif legacy_ch:
                tasks.append(cache_id(legacy_ch))

            # Cache database channels (prevents PeerIdInvalid after redeploy)
            db_channels = config.get("database_channels", {}) if config else {}
            for _plan_name, ch_id in db_channels.items():
                if ch_id:
                    tasks.append(cache_id(ch_id))

            # Cache default channel from settings
            settings_doc = await db.settings.find_one({"_id": "global_settings"})
            if settings_doc:
                default_ch = settings_doc.get("channel")
                if default_ch:
                    tasks.append(cache_link(default_ch))

            if tasks:
                await asyncio.gather(*tasks)

        logger.info("Caching Channel peers...")
        app.loop.run_until_complete(cache_channels())
    except Exception as e:
        logger.warning(f"Error during Channel caching: {e}")

    # --- Background tasks ---
    try:
        from db import db

        async def db_cleanup():
            while True:
                try:
                    now = datetime.datetime.utcnow()
                    # Delete expired temporary files from DB
                    result = await db.files.delete_many(
                        {"status": "temporary", "expires_at": {"$lt": now}}
                    )
                    if result.deleted_count:
                        logger.info(f"Cleaned up {result.deleted_count} expired temporary files from DB.")

                    # Also clean orphaned disk files
                    cleaned, freed = await asyncio.to_thread(_sync_cleanup_orphaned_files)
                    if cleaned:
                        logger.info(f"Cleaned {cleaned} orphaned disk files, freed {freed / (1024*1024):.2f} MB.")
                except Exception as e:
                    logger.error(f"Error during DB cleanup: {e}")

                await asyncio.sleep(21600)  # Every 6 hours

        async def state_cleanup():
            """Periodically clean up expired user sessions and queue batches."""
            while True:
                await asyncio.sleep(1800)  # Every 30 minutes
                try:
                    from utils.state import cleanup_expired as state_cleanup_fn
                    expired = state_cleanup_fn()
                    if expired:
                        logger.info(f"Cleaned {expired} expired user sessions.")
                except Exception as e:
                    logger.debug(f"State cleanup: {e}")
                try:
                    from utils.queue_manager import queue_manager
                    queue_manager.cleanup_completed()
                except Exception as e:
                    logger.debug(f"Queue cleanup: {e}")
                try:
                    from plugins.flow import cleanup_stale_debounce_entries, cleanup_stale_file_sessions
                    cleaned_fs = cleanup_stale_file_sessions()
                    cleaned_db = cleanup_stale_debounce_entries()
                    if cleaned_fs or cleaned_db:
                        logger.info(f"Flow cleanup: {cleaned_fs} file sessions, {cleaned_db} debounce entries.")
                except Exception as e:
                    logger.debug(f"Flow cleanup: {e}")

        logger.info("Scheduling background tasks...")

        # spawn() needs a RUNNING event loop (asyncio.create_task). At this
        # point in main we're between run_until_complete() calls, so there
        # is none — calling spawn() directly raised "no running event loop"
        # and silently killed EVERY background job (DB cleanup, session
        # cleanup, ML worker). Creating the tasks inside a short
        # run_until_complete() attaches them to app.loop; they stay pending
        # when it returns and resume as soon as idle() runs the loop.
        async def _schedule_background_tasks():
            spawn(db_cleanup(), label="db_cleanup")
            spawn(state_cleanup(), label="state_cleanup")

            # Mirror-Leech persistent-queue worker: drains scheduled uploads
            # and retries transient failures with exponential backoff. Safe
            # no-op when Mongo is offline — the worker just loops.
            try:
                from tools.mirror_leech.Worker import start as _start_ml_worker
                _start_ml_worker(app)
            except Exception as e:
                logger.warning(f"Could not start Mirror-Leech worker: {e}")

        app.loop.run_until_complete(_schedule_background_tasks())

    except Exception as e:
        logger.warning(f"Could not schedule background tasks: {e}")

    # --- Recover stale flow sessions from DB ---
    try:
        from db import db

        async def recover_stale_sessions():
            cursor = db.users.find({"flow_session": {"$exists": True}})
            count = 0
            async for user_doc in cursor:
                uid = user_doc.get("user_id")
                if uid:
                    with contextlib.suppress(Exception):
                        await app.send_message(
                            uid,
                            "The bot was restarted and your active renaming session was lost.\n"
                            "Please start again by sending a file or using /start."
                        )
                    await db.clear_flow_session(uid)
                    count += 1
            if count:
                logger.info(f"Recovered {count} stale flow sessions from DB.")

        logger.info("Checking for stale flow sessions...")

        async def _spawn_recover():
            spawn(recover_stale_sessions(), label="recover_stale_sessions")

        app.loop.run_until_complete(_spawn_recover())
    except Exception as e:
        logger.warning(f"Error recovering stale sessions: {e}")

    # --- Orphaned file cleanup (async, non-blocking) ---
    try:
        async def async_cleanup_orphaned():
            cleaned_count, freed_space = await asyncio.to_thread(_sync_cleanup_orphaned_files)
            if cleaned_count > 0:
                logger.info(f"Cleanup complete. Removed {cleaned_count} files, freed {freed_space / (1024*1024):.2f} MB.")
            else:
                logger.info("Cleanup complete. No orphaned files found.")

        logger.info("Running automated orphaned file cleanup...")

        async def _spawn_orphan_cleanup():
            spawn(async_cleanup_orphaned(), label="orphaned_file_cleanup")

        app.loop.run_until_complete(_spawn_orphan_cleanup())
    except Exception as e:
        logger.warning(f"Error during orphaned file cleanup: {e}")

    # --- XTV Pro userbot ---
    try:
        from db import db

        async def get_userbot_session():
            return await db.get_pro_session()

        pro_data = app.loop.run_until_complete(get_userbot_session())

        if pro_data and pro_data.get("session_string"):
            logger.info(
                "𝕏TV Pro™ Session detected in database. Initializing Premium Userbot..."
            )
            user_bot = Client(
                "xtv_user_bot",
                api_id=pro_data.get("api_id", Config.API_ID),
                api_hash=pro_data.get("api_hash", Config.API_HASH),
                session_string=pro_data.get("session_string"),
                workers=50,
                max_concurrent_transmissions=10,
            )
            app.user_bot = user_bot

            logger.info("Starting 𝕏TV Pro™ Premium Userbot...")
            user_bot.start()
            logger.info("𝕏TV Pro™ Premium Userbot Started Successfully!")

        else:
            app.user_bot = None
            logger.warning(
                "No 𝕏TV Pro™ Session found in database. 4GB upload support is DISABLED."
            )
    except Exception as e:
        logger.error(f"Failed to initialize Userbot from DB: {e}")
        app.user_bot = None

    # --- Startup diagnostics ---
    admins_count = len(Config.ADMIN_IDS)
    tmdb_status = (
        "Configured"
        if Config.TMDB_API_KEY
        else "Missing (optional — TMDb features disabled)"
    )
    db_status = "Configured" if Config.MAIN_URI else "Missing"
    xtv_pro_status = "Enabled (4GB Support)" if getattr(app, 'user_bot', None) else "Disabled (2GB Limit)"

    startup_msg = (
        f"\n{'='*60}\n"
        f"  𝕏TV MediaStudio {Config.VERSION} Initialization\n"
        f"{'-'*60}\n"
        f"  Core Settings:\n"
        f"   - Debug Mode  : {'ON' if Config.DEBUG_MODE else 'OFF'}\n"
        f"   - Public Mode : {'ON' if Config.PUBLIC_MODE else 'OFF'}\n"
        f"   - XTV Pro     : {xtv_pro_status}\n"
        f"\n"
        f"  Access Control:\n"
        f"   - CEO ID      : {Config.CEO_ID if Config.CEO_ID else 'Not Set'}\n"
        f"   - Admins      : {admins_count} configured\n"
        f"\n"
        f"  Integrations:\n"
        f"   - Database    : {db_status}\n"
        f"   - TMDb API    : {tmdb_status}\n"
        f"\n"
        f"  Storage:\n"
        f"   - Down Dir    : ./{Config.DOWNLOAD_DIR}\n"
        f"   - Def Channel : {Config.DEFAULT_CHANNEL}\n"
        f"{'='*60}"
    )
    logger.info(startup_msg)
    idle()

    # --- Graceful shutdown ---
    logger.info("Shutting down...")

    # Cancel every in-flight Mirror-Leech task so the worker pool lets
    # the event loop close cleanly instead of hanging on an async http
    # stream.
    try:
        from tools.mirror_leech.Tasks import ml_worker_pool

        app.loop.run_until_complete(ml_worker_pool.shutdown())
    except Exception as e:
        logger.debug(f"ML worker pool shutdown: {e}")

    # Close persistent HTTP sessions
    try:
        from utils.tmdb import tmdb
        app.loop.run_until_complete(tmdb.close())
    except Exception as e:
        logger.debug(f"TMDb session cleanup: {e}")

    try:
        from utils.currency import close_session as close_currency_session
        app.loop.run_until_complete(close_currency_session())
    except Exception as e:
        logger.debug(f"Currency session cleanup: {e}")

    if user_bot:
        try:
            user_bot.stop()
        except Exception as e:
            logger.warning(f"Error stopping userbot: {e}")

    app.stop()
    logger.info("𝕏TV MediaStudio shut down cleanly.")

# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# Telegram Channel @XTVbots
# Developed for the 𝕏TV Network @XTVglobal
# Backup Channel @XTVhome
# Contact on Telegram @davdxpx
# --------------------------------------------------------------------------
