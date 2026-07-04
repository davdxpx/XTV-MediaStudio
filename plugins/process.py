# --- Imports ---
import asyncio
import contextlib
import datetime as _dt
import logging
import math
import os
import random
import re
import secrets as _secrets
import shutil
import string
import time
from typing import Any, Dict, Optional, Tuple

import aiohttp
from pyrogram import Client
from pyrogram.enums import ChatType
from pyrogram.errors import MessageNotModified
from pyrogram.types import Message

from config import Config
from db import db
from utils.media.detect import apply_autofill, probe_audio_streams
from utils.media.ffmpeg_tools import (
    execute_ffmpeg,
    generate_ffmpeg_command,
    probe_file,
)
from utils.media.patterns import detect_all as _detect_patterns
from utils.media.patterns import flatten_specials as _flatten_specials
from utils.queue_manager import queue_manager
from utils.telegram.progress import progress_for_pyrogram
from utils.template import (
    SCOPE_GROUPS,
    allowed_fields_for,
    apply_trim,
    safe_format,
    shorten_filename,
)
from utils.tmdb import tmdb
from utils.XTVengine import XTVEngine

logger = logging.getLogger("TaskProcessor")

_SEMAPHORES: Dict[int, Dict[str, Optional[asyncio.Semaphore]]] = {}

# === Helper Functions ===
def get_semaphore(user_id: int, phase: str, is_priority: bool = False) -> asyncio.Semaphore:
    if user_id not in _SEMAPHORES:
        _SEMAPHORES[user_id] = {"download": None, "process": None, "upload": None}

    if _SEMAPHORES[user_id][phase] is None:
        limit = 10 if is_priority else 3
        _SEMAPHORES[user_id][phase] = asyncio.Semaphore(limit)

    return _SEMAPHORES[user_id][phase]

# === Classes ===
class TaskProcessor:

    def __init__(self, client: Client, message: Message, data: Dict[str, Any]):
        self.client = client
        self.message = message
        self.data = data

        self.user_id = message.chat.id
        self.message_id = message.id
        self.start_time = time.time()

        self.download_dir = Config.DOWNLOAD_DIR
        self.input_path: Optional[str] = None
        self.output_path: Optional[str] = None
        self.thumb_path: Optional[str] = None

        self.media_type = data.get("type")
        self.is_subtitle = data.get("is_subtitle", False)
        self.language = data.get("language", "en")
        self.tmdb_id = data.get("tmdb_id")
        self.original_name = data.get("original_name", "unknown.mkv")

        if data.get("title"):
            self.title = data.get("title")
        else:
            self.title = os.path.splitext(self.original_name)[0]

        self.year = data.get("year")
        self.poster_url = data.get("poster")
        self.season = data.get("season")
        self.episode = data.get("episode")
        self.quality = data.get("quality", "720p")
        self.file_message = data.get("file_message")

        self.status_msg: Optional[Message] = None
        self.settings: Optional[Dict] = None
        self.templates: Optional[Dict] = None
        self.filename_templates: Optional[Dict] = None
        self.channel: Optional[str] = None

        self.mode = "core"
        self.active_client = self.client
        self.tunnel_id = None
        self.tunneled_message_id = None
        self.is_priority = False

        # Template-system caches, populated lazily by `_build_fmt_dict`
        # the first time a scope/template needs them. Storing as None so
        # the attribute always exists — cheaper than hasattr checks.
        self._tmdb_details_data = None
        self._input_probe_data = None
        self._output_probe_data = None
        # Populated in `_prepare_resources` — every new placeholder reads
        # from here to avoid re-deriving formatted strings mid-pipeline.
        self._eager_context = {}
        self._pattern_groups = None

        try:
            user_bot = getattr(self.client, "user_bot", None)
            if user_bot:
                file_size = 0
                media = self.file_message.document or self.file_message.video
                if media:
                    file_size = media.file_size

                if file_size > 2000 * 1000 * 1000:
                    self.mode = "pro"
                    self.active_client = user_bot
                    logger.info(
                        f"Activated PRO Mode for task {self.message_id} (Size: {file_size})"
                    )
        except Exception as e:
            logger.warning(f"Error determining mode: {e}")

    async def run(self):
        batch_id = self.data.get("batch_id")
        item_id = self.data.get("item_id")

        file_size = 0
        if self.file_message:
            media = self.file_message.document or self.file_message.video or self.file_message.audio or self.file_message.photo
            if media:
                file_size = getattr(media, "file_size", 0)

        timeout_base = 3600
        timeout_multiplier = (file_size / (1024 * 1024 * 1024)) * 300 if file_size else 0
        phase_timeout = timeout_base + timeout_multiplier

        is_priority = False
        if Config.PUBLIC_MODE:
            user_doc = await db.get_user(self.user_id)
            if user_doc and user_doc.get("is_premium"):
                plan_name = user_doc.get("premium_plan", "standard")
                config = await db.get_public_config()
                if config.get("premium_system_enabled", False):
                    plan_settings = config.get(f"premium_{plan_name}", {})
                    is_priority = plan_settings.get("features", {}).get("priority_queue", False)
        self.is_priority = is_priority

        try:
            if not await self._initialize():
                if batch_id and item_id:
                    queue_manager.update_status(batch_id, item_id, "failed")
                return

            try:
                async with get_semaphore(self.user_id, "download", is_priority):
                    dl_success = await asyncio.wait_for(self._download_media(), timeout=phase_timeout)
                    if not dl_success:
                        if batch_id and item_id:
                            queue_manager.update_status(batch_id, item_id, "failed")
                        return
            except asyncio.TimeoutError:
                logger.error(f"Download phase timed out for {self.message_id}")
                await self._update_status("❌ **Download Timeout**\n\nTask exceeded maximum execution time.")
                if batch_id and item_id:
                    queue_manager.update_status(batch_id, item_id, "failed", "Timeout")
                return

            try:
                async with get_semaphore(self.user_id, "process", is_priority):
                    await asyncio.wait_for(self._prepare_resources(), timeout=1800)
                    proc_success = await asyncio.wait_for(self._process_media(), timeout=phase_timeout)
                    if not proc_success:
                        if batch_id and item_id:
                            queue_manager.update_status(batch_id, item_id, "failed")
                        return
            except asyncio.TimeoutError:
                logger.error(f"Process phase timed out for {self.message_id}")
                await self._update_status("❌ **Process Timeout**\n\nTask exceeded maximum execution time (FFmpeg stall).")
                if batch_id and item_id:
                    queue_manager.update_status(batch_id, item_id, "failed", "Timeout")
                return

            try:
                async with get_semaphore(self.user_id, "upload", is_priority):
                    await asyncio.wait_for(self._upload_media(), timeout=phase_timeout)
            except asyncio.TimeoutError:
                logger.error(f"Upload phase timed out for {self.message_id}")
                await self._update_status("❌ **Upload Timeout**\n\nTask exceeded maximum execution time.")
                if batch_id and item_id:
                    queue_manager.update_status(batch_id, item_id, "failed", "Timeout")
                return

        except Exception as e:
            logger.exception(f"Critical error in task for user {self.user_id}: {e}")
            await self._update_status(f"❌ **Critical System Error**\n\n`{str(e)}`")
            if batch_id and item_id:
                queue_manager.update_status(batch_id, item_id, "failed")
        finally:
            await self._cleanup()
            if (
                batch_id
                and queue_manager.is_batch_complete(batch_id)
                and not getattr(queue_manager.batches.get(batch_id), "summary_sent", False)
            ):
                try:
                    usage = await db.get_user_usage(self.user_id)
                    config = await db.get_public_config()
                    daily_egress_mb_limit = config.get("daily_egress_mb", 0)
                    daily_file_count_limit = config.get("daily_file_count", 0)
                    global_limit_mb = await db.get_global_daily_egress_limit()

                    now = time.time()
                    user_doc = await db.get_user(self.user_id)
                    is_premium = False
                    if user_doc:
                        exp = user_doc.get("premium_expiry")
                        if user_doc.get("is_premium") and (exp is None or exp > now):
                            is_premium = True

                    premium_system_enabled = config.get("premium_system_enabled", False)

                    if is_premium and premium_system_enabled:
                        daily_egress_mb_limit = config.get("premium_daily_egress_mb", 0)

                    user_files = usage.get("file_count", 0)
                    user_egress_mb = usage.get("egress_mb", 0.0)
                    global_usage_mb = await db.get_global_usage_today()

                    if self.user_id == Config.CEO_ID or self.user_id in Config.ADMIN_IDS:
                        if global_limit_mb > 0:
                            limit_str = f"{global_limit_mb} MB"
                            if global_limit_mb >= 1024:
                                limit_str = f"{global_limit_mb / 1024:.2f} GB"
                            used_str = f"{global_usage_mb:.2f} MB"
                            if global_usage_mb >= 1024:
                                used_str = f"{global_usage_mb / 1024:.2f} GB"
                            usage_text = f"Today: {user_files} files processed · {used_str} used of {limit_str} (Global Limit)"
                        else:
                            usage_text = f"Today: {user_files} files · {user_egress_mb:.2f} MB used (Unlimited)"
                    else:
                        if daily_egress_mb_limit <= 0 and daily_file_count_limit <= 0 and global_limit_mb <= 0:
                            usage_text = f"Today: {user_files} files · {user_egress_mb:.2f} MB used (No limits set)"
                        else:
                            limit_to_show = daily_egress_mb_limit
                            show_global = False
                            if global_limit_mb > 0 and (daily_egress_mb_limit <= 0 or global_limit_mb < daily_egress_mb_limit):
                                limit_to_show = global_limit_mb
                                show_global = True

                            if limit_to_show > 0:
                                limit_str = f"{limit_to_show} MB"
                                if limit_to_show >= 1024:
                                    limit_str = f"{limit_to_show / 1024:.2f} GB"
                            else:
                                limit_str = "Unlimited"

                            if show_global:
                                used_str = f"{global_usage_mb:.2f} MB"
                                if global_usage_mb >= 1024:
                                    used_str = f"{global_usage_mb / 1024:.2f} GB"
                            else:
                                used_str = f"{user_egress_mb:.2f} MB"
                                if user_egress_mb >= 1024:
                                    used_str = f"{user_egress_mb / 1024:.2f} GB"

                            limit_type = " (Global Limit)" if show_global else ""
                            usage_text = f"Today: {user_files} files · {used_str} used of {limit_str}{limit_type}"

                    summary_msg = queue_manager.get_batch_summary(batch_id, usage_text)
                    await self.client.send_message(self.user_id, summary_msg)
                    with contextlib.suppress(Exception):
                        await self.client.send_sticker(self.user_id, "CAACAgIAAxkBAAEQa0xpgkMvycmQypya3zZxS5rU8tuKBQACwJ0AAjP9EEgYhDgLPnTykDgE")
                    if queue_manager.batches.get(batch_id):
                        queue_manager.batches.get(batch_id).summary_sent = True
                except Exception as e:
                    logger.warning(f"Failed to send early batch completion msg: {e}")

    async def _initialize(self) -> bool:
        from pyrogram.errors import FloodWait
        if not shutil.which("ffmpeg"):
            try:
                await self.message.edit_text(
                    "❌ **System Error**\n\n`ffmpeg` binary not found. Contact administrator."
                )
            except MessageNotModified:
                pass
            except FloodWait as e:
                logger.warning(f"FloodWait in _initialize: sleeping for {e.value}s")
                await asyncio.sleep(e.value + 1)
                with contextlib.suppress(Exception):
                    await self.message.edit_text(
                        "❌ **System Error**\n\n`ffmpeg` binary not found. Contact administrator."
                    )
            except Exception:
                pass
            return False

        try:
            self.status_msg = await self.message.edit_text(
                "⏳ **Initializing Task...**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "> Allocating resources and configuring pipeline...\n"
                "\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{XTVEngine.get_signature(mode=self.mode)}"
            )
        except MessageNotModified:
            pass
        except FloodWait as e:
            logger.warning(f"FloodWait in _initialize: sleeping for {e.value}s")
            await asyncio.sleep(e.value + 1)
            with contextlib.suppress(Exception):
                self.status_msg = await self.message.edit_text(
                    "⏳ **Initializing Task...**\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "> Allocating resources and configuring pipeline...\n"
                    "\n━━━━━━━━━━━━━━━━━━━━\n"
                    f"{XTVEngine.get_signature(mode=self.mode)}"
                )
        except Exception:
            pass

        self.settings = await db.get_settings(self.user_id)
        if self.settings:
            self.templates = self.settings.get("templates", Config.DEFAULT_TEMPLATES)
            self.filename_templates = self.settings.get(
                "filename_templates", Config.DEFAULT_FILENAME_TEMPLATES
            )
            self.channel = self.settings.get("channel", Config.DEFAULT_CHANNEL)
        else:
            logger.warning("Database settings unavailable, using defaults.")
            self.templates = Config.DEFAULT_TEMPLATES
            self.filename_templates = Config.DEFAULT_FILENAME_TEMPLATES
            self.channel = Config.DEFAULT_CHANNEL

        return True

    async def _download_media(self) -> bool:
        await self._update_status(
            "📥 **Downloading Media...**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "> Establishing connection to Telegram servers...\n"
            "\n━━━━━━━━━━━━━━━━━━━━\n"
            f"{XTVEngine.get_signature(mode=self.mode)}"
        )

        ext = ".mkv"
        if self.original_name:
            orig_ext = os.path.splitext(self.original_name)[1].lower()
            if orig_ext:
                ext = orig_ext

        if self.is_subtitle and (not ext or ext not in [".srt", ".ass", ".vtt"]):
            ext = ".srt"

        if self.message.photo:
            ext = ".jpg"

        self.input_path = os.path.join(
            self.download_dir, f"{self.user_id}_{self.message_id}_input{ext}"
        )
        download_start = time.time()

        if self.media_type == "audio":
            if not hasattr(self, "metadata"):
                self.metadata = {}

            if self.data.get("audio_thumb_id"):
                self.thumb_path = os.path.join(
                    self.download_dir, f"{self.user_id}_{self.message_id}_thumb.jpg"
                )
                await self.active_client.download_media(
                    self.data.get("audio_thumb_id"), file_name=self.thumb_path
                )

            self.metadata["title"] = self.data.get("audio_title", "")
            self.metadata["artist"] = self.data.get("audio_artist", "")
            if self.data.get("audio_album"):
                self.metadata["album"] = self.data.get("audio_album", "")

        if self.data.get("local_file_path"):
            local_path = self.data.get("local_file_path")
            if os.path.exists(local_path):
                import shutil

                await asyncio.to_thread(shutil.move, local_path, self.input_path)
                file_size = os.path.getsize(self.input_path)
                logger.info(f"Local file moved: {self.input_path} ({file_size} bytes)")

                if self.file_message:

                    class DummyMedia:
                        def __init__(self, size):
                            self.file_size = size

                    if not hasattr(self.file_message, "document") or self.file_message.document is None:
                        self.file_message.document = DummyMedia(file_size)
                    else:
                        self.file_message.document.file_size = file_size

                return True
            else:
                await self._update_status("❌ **Local File Error**\n\nThe extracted file was not found.")
                return False

        target_message = self.file_message
        if self.mode == "pro":
            try:
                bot_me = await self.client.get_me()
                bot_username = bot_me.username

                channel = await self.active_client.create_channel(
                    title=f"𝕏TV Pro Ephemeral {self.message_id}",
                    description="Temporary tunnel for 𝕏TV Bot.",
                )
                self.tunnel_id = channel.id

                from pyrogram.types import ChatPrivileges

                await self.active_client.promote_chat_member(
                    self.tunnel_id,
                    bot_username,
                    privileges=ChatPrivileges(
                        can_manage_chat=True,
                        can_delete_messages=True,
                        can_manage_video_chats=True,
                        can_restrict_members=True,
                        can_promote_members=True,
                        can_change_info=True,
                        can_post_messages=True,
                        can_edit_messages=True,
                        can_invite_users=True,
                        can_pin_messages=True,
                    ),
                )

                ping_msg = await self.active_client.send_message(
                    self.tunnel_id, "ping", disable_notification=True
                )
                await ping_msg.delete()
                await asyncio.sleep(1)

                tunnel_msg = await self.client.copy_message(
                    chat_id=self.tunnel_id,
                    from_chat_id=self.file_message.chat.id,
                    message_id=self.file_message.id,
                )

                target_message = await self.active_client.get_messages(
                    chat_id=self.tunnel_id, message_ids=tunnel_msg.id
                )

                if not target_message or target_message.empty:
                    logger.error(
                        f"Could not fetch copied message {tunnel_msg.id} from tunnel {self.tunnel_id} via Userbot."
                    )
                    await self._update_status(
                        "❌ **Tunnel Resolution Error**\n\nUserbot failed to see the file in the internal tunnel."
                    )
                    return False

                # Pro telemetry: a successful tunnel resolution means the file
                # has flowed through the userbot. Best-effort, never blocks.
                with contextlib.suppress(Exception):
                    await db.log_pro_upload(file_size)

                self.tunneled_message_id = tunnel_msg.id

            except Exception as e:
                logger.error(f"Error creating/resolving Ephemeral Tunnel: {e}")
                await self._update_status(f"❌ **Tunnel Bridge Error**\n\n`{e}`")
                return False

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                downloaded_path = await self.active_client.download_media(
                    target_message,
                    file_name=self.input_path,
                    progress=progress_for_pyrogram,
                    progress_args=(
                        f"📥 **Downloading Media Content...**\n(Attempt {attempt}/{max_retries})",
                        self.status_msg,
                        download_start,
                        self.mode,
                        self.is_priority,
                    ),
                )

                if downloaded_path and os.path.exists(downloaded_path):
                    self.input_path = downloaded_path
                    file_size = os.path.getsize(self.input_path)
                    logger.info(f"Download attempt {attempt} success: {self.input_path} ({file_size} bytes)")

                    if file_size == 0:
                        logger.warning(f"Download attempt {attempt} failed: File size is 0 bytes.")
                        os.remove(self.input_path)
                        if attempt < max_retries:
                            await asyncio.sleep(3)
                            continue
                        else:
                            await self._update_status(
                                "❌ **Download Integrity Error**\n\nFile size is 0 bytes after retries."
                            )
                            return False
                    return True
                else:
                    logger.error(f"Download attempt {attempt} returned path but file missing: {self.input_path}")
                    if attempt < max_retries:
                        await asyncio.sleep(3)
                        continue
                    else:
                        await self._update_status(
                            "❌ **Download Verification Failed**\n\nFile not found on disk."
                        )
                        return False

            except Exception as e:
                logger.error(f"Download attempt {attempt} failed: {e}")
                if os.path.exists(self.input_path):
                    with contextlib.suppress(OSError):
                        os.remove(self.input_path)
                if attempt < max_retries:
                    await asyncio.sleep(5)
                    continue
                else:
                    await self._update_status(f"❌ **Network Error during Download**\n\n`{e}`")
                    return False

        return False

    async def _prepare_resources(self):
        await self._update_status(
            "🎨 **Preparing Resources...**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "> Optimizing thumbnails and configuring metadata...\n"
            "\n━━━━━━━━━━━━━━━━━━━━\n"
            f"{XTVEngine.get_signature(mode=self.mode)}"
        )

        if not self.thumb_path:
            self.thumb_path = os.path.join(
                self.download_dir, f"{self.user_id}_{self.message_id}_thumb.jpg"
            )

        if not self.is_subtitle and self.media_type != "audio":
            thumb_mode = self.settings.get("thumbnail_mode", "none") if self.settings else "none"

            if thumb_mode == "none":
                self.thumb_path = None
            else:
                thumb_binary = (
                    self.settings.get("thumbnail_binary") if self.settings else None
                )

                if thumb_mode == "custom" and thumb_binary:
                    def write_thumb():
                        with open(self.thumb_path, "wb") as f:
                            f.write(thumb_binary)
                    await asyncio.to_thread(write_thumb)
                elif thumb_mode == "auto" and self.poster_url:
                    try:
                        async with aiohttp.ClientSession() as session, session.get(self.poster_url) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                def write_poster():
                                    with open(self.thumb_path, "wb") as f:
                                        f.write(data)
                                await asyncio.to_thread(write_poster)
                    except Exception as e:
                        logger.warning(f"Failed to download poster: {e}")
                else:
                    self.thumb_path = None

        safe_title = re.sub(r'[\\/*?:"<>|,;\'!]', "", self.title)
        safe_title = safe_title.replace("&", "and")

        ext = ".mkv" if not self.is_subtitle else ".srt"
        if not self.is_subtitle and self.original_name:
            orig_ext = os.path.splitext(self.original_name)[1].lower()
            if orig_ext:
                ext = orig_ext

        if self.message.photo:
            ext = ".jpg"

        season_str = f"S{self.season:02d}" if self.season is not None else ""
        if isinstance(self.episode, list):
            episode_str = "".join([f"E{int(e):02d}" for e in self.episode])
        else:
            episode_str = f"E{self.episode:02d}" if self.episode is not None else ""
        season_episode = f"{season_str}{episode_str}"
        year_str = str(self.year) if self.year else ""

        pref_sep = await db.get_preferred_separator(self.user_id) if hasattr(self, 'user_id') else "."

        # v1.6.0: Runtime audio-stream probe + auto-fill.
        # Safe no-op when ffprobe missing, file absent, subtitle, or user locked fields.
        if (
            self.input_path
            and not self.is_subtitle
            and self.media_type not in ("audio", "convert", "extract_subtitles", "watermark")
            and not self.data.get("audio_locked")
            and not self.data.get("audio")
        ):
            try:
                detected = await probe_audio_streams(self.input_path)
                if detected:
                    self.data["detected_audio_runtime"] = detected
                    apply_autofill(self.data)
                    logger.info(
                        f"[autofill] user={self.user_id} msg={self.message_id} "
                        f"audio={self.data.get('audio')!r} (runtime-detected)"
                    )
            except Exception as e:
                logger.warning(f"probe_audio_streams failed: {e}")

        # Detection moved to utils.media.patterns (v1.6.2). The user's
        # confirm-screen picks (stored on self.data) still win over
        # regex detection; only fall back to detection when the field
        # isn't already on the session.
        if "specials" in self.data:
            extracted_specials = self.data["specials"]
            _fallback_groups = None
        else:
            _fallback_groups = _detect_patterns(self.original_name or "")
            extracted_specials = _flatten_specials(_fallback_groups)

        if "codec" in self.data:
            extracted_codec = [self.data["codec"]] if self.data["codec"] else []
        else:
            if _fallback_groups is None:
                _fallback_groups = _detect_patterns(self.original_name or "")
            codec_label = _fallback_groups.get("codec")
            extracted_codec = [codec_label] if isinstance(codec_label, str) and codec_label else []

        if "audio" in self.data:
            extracted_audio = [self.data["audio"]] if self.data["audio"] else []
        else:
            if _fallback_groups is None:
                _fallback_groups = _detect_patterns(self.original_name or "")
            audio_label = _fallback_groups.get("audio")
            extracted_audio = [audio_label] if isinstance(audio_label, str) and audio_label else []

        specials_str = pref_sep.join(extracted_specials)
        codec_str = pref_sep.join(extracted_codec)
        audio_str = pref_sep.join(extracted_audio)

        # Stash the raw pattern groups so SOURCE placeholders (Source, HDR,
        # Edition, Release, Extras) can read them later without re-running
        # the detector.
        self._pattern_groups = _fallback_groups if _fallback_groups is not None else _detect_patterns(self.original_name or "")
        self._eager_context = {
            "safe_title": safe_title,
            "year_str": year_str,
            "season_str": season_str,
            "episode_str": episode_str,
            "season_episode": season_episode,
            "pref_sep": pref_sep,
            "specials_str": specials_str,
            "codec_str": codec_str,
            "audio_str": audio_str,
            "ext_hint": ext,
        }

        def clean_filename(name, orig_template=""):

            name = re.sub(r'\[\s*\]', '', name)
            name = re.sub(r'\(\s*\)', '', name)
            name = re.sub(r'\{\s*\}', '', name)

            name = re.sub(r'[\._\s]{2,}', pref_sep, name)

            if orig_template and " " not in orig_template:
                if "." in orig_template:
                    name = name.replace(" ", ".")
                    if "_" not in orig_template:
                        name = name.replace("_", ".")
                elif "_" in orig_template:
                    name = name.replace(" ", "_")
                    if "." not in orig_template:
                        name = name.replace(".", "_")

            name = name.strip('._ ')
            return name

        if self.media_type == "general":
            template = self.data.get("general_name", "{filename}")
            scope_fmt = await self._build_fmt_dict("filename_general", template)
            base_name, fmt_err = safe_format(template, scope_fmt)
            if fmt_err:
                logger.warning(
                    f"General template {template!r} rejected ({fmt_err}); using fallback."
                )
                base_name = f"{safe_title}"
            else:
                base_name = clean_filename(base_name, template)

            logger.info(
                f"[rename] user={self.user_id} mode=general "
                f"template={template!r} specials={specials_str!r} codec={codec_str!r} "
                f"audio={audio_str!r} quality={self.quality!r} "
                f"original={self.original_name!r} -> {base_name!r}"
            )

            final_filename = f"{base_name}{ext}"
            meta_title = base_name

        elif self.media_type in ["audio", "convert", "extract_subtitles", "watermark"]:
            final_filename = "to_be_generated"
            meta_title = "to_be_generated"

        elif self.media_type == "series":
            if self.is_subtitle:
                template = self.filename_templates.get(
                    "subtitles_series",
                    Config.DEFAULT_FILENAME_TEMPLATES["subtitles_series"],
                )
                fn_scope = "filename_subs_series"
            else:
                template = self.filename_templates.get(
                    "series", Config.DEFAULT_FILENAME_TEMPLATES["series"]
                )
                fn_scope = "filename_series"

            scope_fmt = await self._build_fmt_dict(fn_scope, template)
            base_name, fmt_err = safe_format(template, scope_fmt)
            if fmt_err:
                logger.warning(
                    f"Series template {template!r} rejected ({fmt_err}); using fallback."
                )
                fallback_template = (
                    "{Title}.{Season_Episode}.{Quality}_[{Channel}]"
                    if not self.is_subtitle
                    else "{Title}.{Season_Episode}.{Language}"
                )
                base_name = (
                    f"{safe_title}.{season_episode}.{self.quality}_[{self.channel}]"
                    if not self.is_subtitle
                    else f"{safe_title}.{season_episode}.{self.language}"
                )
                base_name = clean_filename(base_name, fallback_template)
                base_name = shorten_filename(base_name, ext)
            else:
                base_name = clean_filename(base_name, template)
                # Keep the full name (incl. extension) under 64 chars:
                # abbreviate verbose tags (Dolby.Vision → DV), then drop
                # low-priority placeholders, then hard-truncate.
                base_name = shorten_filename(
                    base_name,
                    ext,
                    template=template,
                    fmt=scope_fmt,
                    cleaner=lambda n: clean_filename(n, template),
                )

            logger.info(
                f"[rename] user={self.user_id} mode=series sub={self.is_subtitle} "
                f"template={template!r} specials={specials_str!r} codec={codec_str!r} "
                f"audio={audio_str!r} quality={self.quality!r} "
                f"season_episode={season_episode!r} original={self.original_name!r} "
                f"-> {base_name!r}"
            )

            final_filename = f"{base_name}{ext}"
            title_template = self.templates.get("title", "")
            title_fmt = await self._build_fmt_dict("metadata_title", title_template)
            meta_title, title_err = safe_format(title_template, title_fmt)
            if title_err:
                logger.warning(
                    f"Series title template {title_template!r} rejected ({title_err}); "
                    f"falling back to raw title."
                )
                meta_title = self.title or ""
        else:
            personal_type = self.data.get("personal_type")
            if personal_type:
                key = f"personal_{personal_type}"
                template = self.filename_templates.get(
                    key, Config.DEFAULT_FILENAME_TEMPLATES[key]
                )
                fn_scope = f"filename_personal_{personal_type}"
            elif self.is_subtitle:
                template = self.filename_templates.get(
                    "subtitles_movies",
                    Config.DEFAULT_FILENAME_TEMPLATES["subtitles_movies"],
                )
                fn_scope = "filename_subs_movies"
            else:
                template = self.filename_templates.get(
                    "movies", Config.DEFAULT_FILENAME_TEMPLATES["movies"]
                )
                fn_scope = "filename_movies"

            scope_fmt = await self._build_fmt_dict(fn_scope, template)
            base_name, fmt_err = safe_format(template, scope_fmt)
            if fmt_err:
                logger.warning(
                    f"Movie template {template!r} rejected ({fmt_err}); using fallback."
                )
                fallback_template = (
                    "{Title}.{Year}.{Quality}_[{Channel}]"
                    if not self.is_subtitle
                    else "{Title}.{Year}.{Language}"
                )
                base_name = (
                    f"{safe_title}.{year_str}.{self.quality}_[{self.channel}]"
                    if not self.is_subtitle
                    else f"{safe_title}.{year_str}.{self.language}"
                )
                base_name = clean_filename(base_name, fallback_template)
                base_name = shorten_filename(base_name, ext)
            else:
                base_name = clean_filename(base_name, template)
                base_name = shorten_filename(
                    base_name,
                    ext,
                    template=template,
                    fmt=scope_fmt,
                    cleaner=lambda n: clean_filename(n, template),
                )

            logger.info(
                f"[rename] user={self.user_id} mode={self.media_type} sub={self.is_subtitle} "
                f"template={template!r} specials={specials_str!r} codec={codec_str!r} "
                f"audio={audio_str!r} quality={self.quality!r} year={year_str!r} "
                f"original={self.original_name!r} -> {base_name!r}"
            )

            final_filename = f"{base_name}{ext}"
            title_template = self.templates.get("title", "")
            title_fmt = await self._build_fmt_dict("metadata_title", title_template)
            meta_title, title_err = safe_format(title_template, title_fmt)
            if title_err:
                logger.warning(
                    f"Movie title template {title_template!r} rejected ({title_err}); "
                    f"falling back to raw title."
                )
                meta_title = self.title or ""
            meta_title = meta_title.strip()

        self.output_path = os.path.join(self.download_dir, final_filename)

        if os.path.exists(self.output_path):
            self.output_path = os.path.join(
                self.download_dir, f"{int(time.time())}_{final_filename}"
            )

        if not hasattr(self, "metadata"):
            self.metadata = {}

        if "title" not in self.metadata:
            self.metadata["title"] = meta_title

        # audio_title / subtitle_title keep their ``{lang}`` placeholder
        # intact — ffmpeg_tools fills it per-stream at encode time. For
        # those two scopes we substitute a sentinel before formatting so
        # the other TECHNICAL / TMDB placeholders still resolve.
        async def _render_meta(scope, template):
            if not template:
                return ""
            needs_lang_sentinel = scope in {"metadata_audio", "metadata_subtitle"}
            src = template
            if needs_lang_sentinel:
                src = src.replace("{lang}", "\x00LANG\x00")
            fmt = await self._build_fmt_dict(scope, src)
            rendered, err = safe_format(src, fmt)
            if err:
                logger.warning(
                    f"Metadata template ({scope}) {template!r} rejected ({err}); "
                    f"keeping raw template."
                )
                rendered = template
            if needs_lang_sentinel:
                rendered = rendered.replace("\x00LANG\x00", "{lang}")
            return rendered

        if "artist" not in self.metadata:
            self.metadata["artist"] = await _render_meta(
                "metadata_artist", self.templates.get("artist", "")
            )

        self.metadata.update(
            {
                "author":        await _render_meta("metadata_author",   self.templates.get("author", "")),
                "encoded_by":    "@XTVglobal",
                "video_title":   await _render_meta("metadata_video",    self.templates.get("video", "Encoded By:- @XTVglobal")),
                "audio_title":   await _render_meta("metadata_audio",    self.templates.get("audio", "Audio By:- @XTVglobal - {lang}")),
                "subtitle_title":await _render_meta("metadata_subtitle", self.templates.get("subtitle", "Subtitled By:- @XTVglobal - {lang}")),
                "default_language": "English",
                "copyright":     await _render_meta("metadata_copyright",self.templates.get("copyright", "@XTVglobal")),
                "comment":       await _render_meta("metadata_comment",  self.templates.get("comment", "")),
                "description":   await _render_meta("metadata_description", self.templates.get("description", "")),
                "genre":         await _render_meta("metadata_genre",    self.templates.get("genre", "")),
                "date":          await _render_meta("metadata_date",     self.templates.get("date", "")),
            }
        )

        # Series-only / audio-only tags — skip when the media type doesn't
        # match so ffmpeg doesn't get spurious entries.
        if self.media_type == "series":
            self.metadata["show"] = await _render_meta("metadata_show", self.templates.get("show", ""))
            network_val = await _render_meta("metadata_network", self.templates.get("network", ""))
            if network_val:
                self.metadata["network"] = network_val
        if self.media_type == "audio":
            album_val = await _render_meta("metadata_album", self.templates.get("album", ""))
            if album_val:
                self.metadata["album"] = album_val

    async def _process_media(self) -> bool:
        if self.media_type not in ["convert", "extract_subtitles", "audio", "watermark", "trim", "voice_convert", "video_note"]:
            await self._update_status(
                "⚙️ **Processing Media...**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "> Injecting metadata and optimizing container...\n"
                "\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{XTVEngine.get_signature(mode=self.mode)}"
            )

        total_duration = 0
        if self.media_type == "convert" or self.media_type == "extract_subtitles" or self.media_type == "watermark":
            try:
                probe, _ = await probe_file(self.input_path)
                if probe and "format" in probe and "duration" in probe["format"]:
                    total_duration = float(probe["format"]["duration"])
            except Exception as e:
                logger.warning(f"Could not get duration for progress: {e}")

        last_update_time = 0

        async def ffmpeg_progress(time_str):
            nonlocal last_update_time, total_duration

            if total_duration > 0 and (time.time() - last_update_time) > 5:
                try:

                    h, m, s = time_str.split(':')
                    current_time = int(h) * 3600 + int(m) * 60 + float(s)

                    percentage = (current_time / total_duration) * 100
                    if percentage > 100:
                        percentage = 100
                    if percentage < 0:
                        percentage = 0

                    filled_blocks = int(percentage / 10)
                    empty_blocks = 10 - filled_blocks
                    bar = "█" * filled_blocks + "·" * empty_blocks

                    def format_time(seconds):
                        hours = int(seconds // 3600)
                        minutes = int((seconds % 3600) // 60)
                        secs = int(seconds % 60)
                        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

                    remaining = total_duration - current_time
                    filled = int(percentage / 10)
                    bar = "■" * filled + "□" * (10 - filled)

                    msg = (
                        "⚙️ **Processing Media...**\n"
                        "━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"**Progress:**  `{percentage:.1f}%`\n"
                        f"[{bar}]\n\n"
                        f"> **Elapsed:** `{format_time(current_time)}` · "
                        f"**Remaining:** `{format_time(remaining)}`\n"
                        f"> **Duration:** `{format_time(total_duration)}`\n"
                        f"\n━━━━━━━━━━━━━━━━━━━━\n"
                        f"{XTVEngine.get_signature(mode=self.mode)}"
                    )

                    await self._update_status(msg)
                    last_update_time = time.time()
                except Exception as e:
                    logger.debug(f"Failed to update ffmpeg progress: {e}")

        safe_title = re.sub(r'[\\/*?:"<>|,;\'!]', "", self.title)
        safe_title = safe_title.replace("&", "and")

        ext = ".mkv" if not self.is_subtitle else ".srt"
        if not self.is_subtitle and self.original_name:
            orig_ext = os.path.splitext(self.original_name)[1].lower()
            if orig_ext:
                ext = orig_ext
        if self.message.photo:
            ext = ".jpg"

        if self.media_type == "watermark":
            await self._update_status(
                "©️ **Applying Watermark...**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "> Applying customized watermark overlay...\n"
                "\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{XTVEngine.get_signature(mode=self.mode)}"
            )
            from tools.ImageWatermarker import watermark
            wtype = self.data.get("watermark_type")
            wcontent = self.data.get("watermark_content")
            pos = self.data.get("watermark_position", "bottomright")
            size = self.data.get("watermark_size", "medium")

            success, stderr, self.output_path, meta_title = await watermark(
                self.input_path, self.download_dir, safe_title, ext, wtype, wcontent, pos, size,
                self.user_id, self.active_client, progress_callback=ffmpeg_progress
            )
            self.metadata["title"] = meta_title

        elif self.media_type == "convert":
            await self._update_status(
                "🔀 **Converting Media...**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "> Initializing video stream processor...\n"
                "\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{XTVEngine.get_signature(mode=self.mode)}"
            )
            from tools.FileConverter import convert
            target_format = self.data.get("target_format", "mkv")

            success, stderr, self.output_path, meta_title = await convert(
                self.input_path, self.download_dir, safe_title, target_format,
                progress_callback=ffmpeg_progress,
                session_data=self.data,
            )
            self.metadata["title"] = meta_title

        elif self.media_type == "extract_subtitles":
            await self._update_status(
                "📝 **Extracting Subtitles...**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "> Scanning video streams for text tracks...\n"
                "\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{XTVEngine.get_signature(mode=self.mode)}"
            )
            from tools.SubtitleExtractor import extract_subtitles

            success, stderr, self.output_path, meta_title = await extract_subtitles(
                self.input_path, self.download_dir, safe_title, progress_callback=ffmpeg_progress
            )
            self.metadata["title"] = meta_title

        elif self.media_type == "audio":
            await self._update_status(
                "🎵 **Editing Audio Metadata...**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "> Injecting metadata tags and cover art...\n"
                "\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{XTVEngine.get_signature(mode=self.mode)}"
            )
            from tools.AudioMetadataEditor import edit_audio_metadata
            thumb = self.thumb_path if (self.thumb_path and os.path.exists(self.thumb_path)) else None
            success, stderr, self.output_path, meta_title = await edit_audio_metadata(
                self.input_path, self.download_dir, safe_title, ext, self.metadata, thumb, progress_callback=ffmpeg_progress
            )
            self.metadata["title"] = meta_title

        elif self.media_type == "trim":
            await self._update_status(
                "✂️ **Trimming Video...**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"> ▶️ **Start:** `{self.data.get('trim_start', '00:00:00')}`\n"
                f"> ⏹️ **End:** `{self.data.get('trim_end', '00:00:00')}`\n"
                "\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{XTVEngine.get_signature(mode=self.mode)}"
            )
            from tools.VideoTrimmer import trim
            success, stderr, self.output_path, meta_title = await trim(
                self.input_path, self.download_dir, safe_title,
                self.data.get("trim_start", "00:00:00"),
                self.data.get("trim_end", "00:00:00"),
                progress_callback=ffmpeg_progress
            )
            self.metadata["title"] = meta_title

        elif self.media_type == "voice_convert":
            await self._update_status(
                "🎙️ **Converting to Voice Note...**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "> Converting audio to Telegram voice format...\n"
                "\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{XTVEngine.get_signature(mode=self.mode)}"
            )
            from tools.VoiceNoteConverter import convert_to_voice
            success, stderr, self.output_path, meta_title = await convert_to_voice(
                self.input_path, self.download_dir, safe_title, progress_callback=ffmpeg_progress
            )
            self.metadata["title"] = meta_title

        elif self.media_type == "video_note":
            await self._update_status(
                "⭕ **Converting to Video Note...**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "> Cropping to square and encoding round video...\n"
                "\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{XTVEngine.get_signature(mode=self.mode)}"
            )
            from tools.VideoNoteConverter import convert_to_video_note
            success, stderr, self.output_path, meta_title = await convert_to_video_note(
                self.input_path, self.download_dir, safe_title, progress_callback=ffmpeg_progress
            )
            self.metadata["title"] = meta_title

        else:
            cmd, err = await generate_ffmpeg_command(
                input_path=self.input_path,
                output_path=self.output_path,
                metadata=self.metadata,
                thumbnail_path=(
                    self.thumb_path
                    if (
                        self.thumb_path
                        and os.path.exists(self.thumb_path)
                        and not self.is_subtitle
                        and self.media_type != "convert"
                    )
                    else None
                ),
            )
            if not cmd:
                logger.error(f"FFmpeg command generation failed: {err}")
                await self._update_status(
                    f"❌ **Processing Configuration Error**\n\n`{err}`"
                )
                return False

            success, stderr = await execute_ffmpeg(cmd)

        if not success:
            err_msg = "Unknown Error"
            if stderr:
                err_lines = stderr.decode(errors='replace').strip().split('\n')
                # Grab the last few relevant lines for the user
                err_msg = "\n".join(err_lines[-5:]).strip()
                if not err_msg:
                    err_msg = "Unknown Error"

            logger.error(f"FFmpeg execution failed: {err_msg}")

            # Truncate to avoid Telegram message length limits
            if len(err_msg) > 500:
                err_msg = err_msg[-500:] + "..."

            await self._update_status(
                f"❌ **Transcoding Failed**\n\nThe FFmpeg engine reported an error during processing:\n\n`{err_msg}`"
            )
            return False

        if self.media_type == "extract_subtitles" and (
            not os.path.exists(self.output_path) or os.path.getsize(self.output_path) == 0
        ):
            logger.error("Subtitle extraction failed: no subtitles found in stream.")
            await self._update_status("❌ **Extraction Failed**\n\nNo subtitles were found in this video.")
            return False

        return True

    async def _upload_media(self):
        await self._update_status(
            "📤 **Uploading Result...**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "> Transferring optimized asset to Telegram...\n"
            "\n━━━━━━━━━━━━━━━━━━━━\n"
            f"{XTVEngine.get_signature(mode=self.mode)}"
        )

        upload_start = time.time()
        final_filename = os.path.basename(self.output_path)

        caption = await self._generate_caption(final_filename)

        target_chat_id = self.user_id
        is_tunneling = False

        if self.mode == "pro":
            is_tunneling = True
            if self.tunnel_id:
                target_chat_id = self.tunnel_id
            else:
                await self._update_status(
                    "❌ **Upload Configuration Error**\n\nPro Tunnel ID not initialized."
                )
                return

        from pyrogram.errors import FloodWait

        max_upload_retries = 3
        media_msg = None

        for upload_attempt in range(max_upload_retries):
            try:
                if is_tunneling:
                    with contextlib.suppress(Exception):
                        pass

                thumb = (
                    self.thumb_path
                    if (
                        self.thumb_path
                        and os.path.exists(self.thumb_path)
                        and not self.is_subtitle
                    )
                    else None
                )

                send_as = self.data.get("send_as")

                # Voice note: send as voice message
                if self.media_type == "voice_convert":
                    media_msg = await self.active_client.send_voice(
                        chat_id=target_chat_id,
                        voice=self.output_path,
                        caption=caption,
                        progress=progress_for_pyrogram,
                        progress_args=(
                            "📤 **Uploading Voice Note...**",
                            self.status_msg,
                            upload_start,
                            self.mode,
                            self.is_priority,
                        ),
                    )
                    break

                # Video note: send as round video
                if self.media_type == "video_note":
                    media_msg = await self.active_client.send_video_note(
                        chat_id=target_chat_id,
                        video_note=self.output_path,
                        progress=progress_for_pyrogram,
                        progress_args=(
                            "📤 **Uploading Video Note...**",
                            self.status_msg,
                            upload_start,
                            self.mode,
                            self.is_priority,
                        ),
                    )
                    break

                file_ext = os.path.splitext(self.output_path)[1].lower()
                is_vid_ext = file_ext in [".mp4", ".mkv", ".webm", ".avi", ".mov"]
                is_aud_ext = file_ext in [".mp3", ".flac", ".m4a", ".wav", ".ogg"]
                is_img_ext = file_ext in [".jpg", ".jpeg", ".png", ".webp"]

                if send_as == "photo" or (
                    self.message.photo and not send_as and not is_vid_ext and not is_aud_ext
                ):
                    media_msg = await self.active_client.send_photo(
                        chat_id=target_chat_id,
                        photo=self.output_path,
                        caption=caption,
                        progress=progress_for_pyrogram,
                        progress_args=(
                            (
                                "📤 **Uploading Photo (Tunneling)...**"
                                if is_tunneling
                                else "📤 **Uploading Photo...**"
                            ),
                            self.status_msg,
                            upload_start,
                            self.mode,
                            self.is_priority,
                        ),
                    )
                elif send_as == "media":
                    if is_img_ext:
                        media_msg = await self.active_client.send_photo(
                            chat_id=target_chat_id,
                            photo=self.output_path,
                            caption=caption,
                            progress=progress_for_pyrogram,
                            progress_args=(
                                (
                                    "📤 **Uploading Photo (Tunneling)...**"
                                    if is_tunneling
                                    else "📤 **Uploading Photo...**"
                                ),
                                self.status_msg,
                                upload_start,
                                self.mode,
                                self.is_priority,
                            ),
                        )
                    elif is_vid_ext:
                        media_msg = await self.active_client.send_video(
                            chat_id=target_chat_id,
                            video=self.output_path,
                            thumb=thumb,
                            caption=caption,
                            progress=progress_for_pyrogram,
                            progress_args=(
                                (
                                    "📤 **Uploading Video (Tunneling)...**"
                                    if is_tunneling
                                    else "📤 **Uploading Video...**"
                                ),
                                self.status_msg,
                                upload_start,
                                self.mode,
                                self.is_priority,
                            ),
                        )
                    elif is_aud_ext:
                        media_msg = await self.active_client.send_audio(
                            chat_id=target_chat_id,
                            audio=self.output_path,
                            thumb=thumb,
                            caption=caption,
                            title=self.metadata.get("title"),
                            performer=self.metadata.get("artist"),
                            progress=progress_for_pyrogram,
                            progress_args=(
                                (
                                    "📤 **Uploading Audio (Tunneling)...**"
                                    if is_tunneling
                                    else "📤 **Uploading Audio...**"
                                ),
                                self.status_msg,
                                upload_start,
                                self.mode,
                                self.is_priority,
                            ),
                        )
                    else:
                        media_msg = await self.active_client.send_document(
                            chat_id=target_chat_id,
                            document=self.output_path,
                            thumb=thumb,
                            caption=caption,
                            progress=progress_for_pyrogram,
                            progress_args=(
                                (
                                    "📤 **Uploading Media (Tunneling)...**"
                                    if is_tunneling
                                    else "📤 **Uploading Media...**"
                                ),
                                self.status_msg,
                                upload_start,
                                self.mode,
                                self.is_priority,
                            ),
                        )
                else:
                    media_msg = await self.active_client.send_document(
                        chat_id=target_chat_id,
                        document=self.output_path,
                        thumb=thumb,
                        caption=caption,
                        progress=progress_for_pyrogram,
                        progress_args=(
                            (
                                "📤 **Uploading Final File (Tunneling)...**"
                                if is_tunneling
                                else "📤 **Uploading Final File...**"
                            ),
                            self.status_msg,
                            upload_start,
                            self.mode,
                            self.is_priority,
                        ),
                    )

                if is_tunneling:
                    for tunnel_copy_attempt in range(3):
                        try:
                            await self.client.copy_message(
                                chat_id=self.user_id,
                                from_chat_id=self.tunnel_id,
                                message_id=media_msg.id,
                            )
                            break
                        except FloodWait as e:
                            logger.warning(f"FloodWait during tunnel copy: sleeping {e.value}s")
                            await asyncio.sleep(e.value + 1)
                        except Exception as e:
                            logger.error(
                                f"Failed to copy tunneled file to user {self.user_id}: {e}"
                            )
                            if tunnel_copy_attempt == 2:
                                await self.client.send_message(
                                    self.user_id,
                                    f"❌ **Delivery Error**\n\nThe file was processed successfully but the bot failed to deliver it to you from the tunnel. Error: `{e}`",
                                )
                break
            except FloodWait as e:
                logger.warning(f"FloodWait during upload: sleeping {e.value}s before retrying")
                await asyncio.sleep(e.value + 1)
            except Exception as e:
                if upload_attempt == max_upload_retries - 1:
                    raise e
                logger.warning(f"Upload attempt {upload_attempt + 1} failed: {e}. Retrying...")
                await asyncio.sleep(5)

        try:

            file_chat_id = self.data.get("file_chat_id")
            file_message_id = self.data.get("file_message_id")
            if file_chat_id and file_message_id:
                try:
                    await self.client.delete_messages(chat_id=file_chat_id, message_ids=file_message_id)
                except Exception as del_err:
                    logger.warning(f"Failed to auto-delete original message: {del_err}")

            usage_text = ""
            try:

                original_size = 0
                if self.file_message:
                    media = self.file_message.document or self.file_message.video or self.file_message.audio or self.file_message.photo
                    original_size = getattr(media, "file_size", 0) if media else 0

                processed_size = os.path.getsize(self.output_path)

                # PR E: record rich breakdown so the usage dashboard shows
                # per-type (movie/series/…) and per-tool (rename/convert/…)
                # counters. pro_mode flags uploads that used the 𝕏TV Pro
                # userbot for >2 GB files.
                media_type_key = self.media_type or None
                tool_name_key = self.data.get("tool_name") or "rename"
                pro_mode_flag = self.mode == "pro"
                task_duration = None
                if self.start_time:
                    task_duration = max(0.0, time.time() - self.start_time)

                await db.update_usage(
                    self.user_id,
                    processed_size,
                    reserved_file_size_bytes=original_size,
                    media_type=media_type_key,
                    tool_name=tool_name_key,
                    pro_mode=pro_mode_flag,
                    processing_time_seconds=task_duration,
                )

                self.processing_successful = True

                usage = await db.get_user_usage(self.user_id)
                config = await db.get_public_config()

                daily_egress_mb_limit = config.get("daily_egress_mb", 0)
                daily_file_count_limit = config.get("daily_file_count", 0)
                global_limit_mb = await db.get_global_daily_egress_limit()

                user_files = usage.get("file_count", 0)
                user_egress_mb = usage.get("egress_mb", 0.0)

                now = time.time()
                user_doc = await db.get_user(self.user_id)
                is_premium = False
                if user_doc:
                    exp = user_doc.get("premium_expiry")
                    if user_doc.get("is_premium") and (exp is None or exp > now):
                        is_premium = True

                premium_system_enabled = config.get("premium_system_enabled", False)

                if is_premium and premium_system_enabled:
                    daily_egress_mb_limit = config.get("premium_daily_egress_mb", 0)

                global_usage_mb = await db.get_global_usage_today()

                if self.user_id == Config.CEO_ID or self.user_id in Config.ADMIN_IDS:
                    if global_limit_mb > 0:
                        limit_str = f"{global_limit_mb} MB"
                        if global_limit_mb >= 1024:
                            limit_str = f"{global_limit_mb / 1024:.2f} GB"

                        used_str = f"{global_usage_mb:.2f} MB"
                        if global_usage_mb >= 1024:
                            used_str = f"{global_usage_mb / 1024:.2f} GB"

                        usage_text = f"Today: {user_files} files · {used_str} used of {limit_str} (Global Limit)"
                    else:
                        usage_text = f"Today: {user_files} files · {user_egress_mb:.2f} MB used (Unlimited)"
                else:
                    if daily_egress_mb_limit <= 0 and daily_file_count_limit <= 0 and global_limit_mb <= 0:
                        usage_text = f"Today: {user_files} files · {user_egress_mb:.2f} MB used (No limits set)"
                    else:
                        limit_to_show = daily_egress_mb_limit
                        show_global = False
                        if global_limit_mb > 0 and (daily_egress_mb_limit <= 0 or global_limit_mb < daily_egress_mb_limit):
                            limit_to_show = global_limit_mb
                            show_global = True

                        if limit_to_show > 0:
                            limit_str = f"{limit_to_show} MB"
                            if limit_to_show >= 1024:
                                limit_str = f"{limit_to_show / 1024:.2f} GB"
                        else:
                            limit_str = "Unlimited"

                        if show_global:
                            used_str = f"{global_usage_mb:.2f} MB"
                            if global_usage_mb >= 1024:
                                used_str = f"{global_usage_mb / 1024:.2f} GB"
                        else:
                            used_str = f"{user_egress_mb:.2f} MB"
                            if user_egress_mb >= 1024:
                                used_str = f"{user_egress_mb / 1024:.2f} GB"

                        limit_type = " (Global Limit)" if show_global else ""
                        usage_text = f"Today: {user_files} files · {used_str} used of {limit_str}{limit_type}"

            except Exception as usage_e:
                logger.error(
                    f"Error fetching/updating usage for success message: {usage_e}"
                )

            await self.status_msg.delete()

            batch_id = self.data.get("batch_id")
            item_id = self.data.get("item_id")
            dumb_channel = self.data.get("dumb_channel")

            import datetime
            user_doc = await db.get_user(self.user_id)
            if Config.PUBLIC_MODE:
                plan = user_doc.get("premium_plan", "standard") if user_doc and user_doc.get("is_premium") else "free"
            else:
                plan = "global"

            db_channel_id = await db.get_db_channel(plan)

            saved_file_id = None
            storage_channel = db_channel_id

            try:
                if db_channel_id:
                    from pyrogram.errors import PeerIdInvalid
                    try:
                        db_msg = await self.client.copy_message(
                            chat_id=db_channel_id,
                            from_chat_id=media_msg.chat.id,
                            message_id=media_msg.id,
                        )
                    except PeerIdInvalid:
                        await self.client.get_chat(db_channel_id)
                        db_msg = await self.client.copy_message(
                            chat_id=db_channel_id,
                            from_chat_id=media_msg.chat.id,
                            message_id=media_msg.id,
                        )
                    saved_file_id = db_msg.id
                else:
                    # Fallback to the chat the user is in if no DB channel is configured
                    # The file was already sent to the user, so we just use that message.
                    # Note: If the user deletes this message from their history, it will break.
                    # But it ensures the file is "saved" in their myfiles automatically right out of the box.
                    storage_channel = target_chat_id
                    saved_file_id = media_msg.id

                config = await db.get_public_config() if Config.PUBLIC_MODE else await db.settings.find_one({"_id": "global_settings"})
                limits = config.get("myfiles_limits", {}).get(plan, {})
                perm_limit = limits.get("permanent_limit", 50)
                expiry_days = limits.get("expiry_days", 10)

                auto_perm = True
                user_settings = await db.get_settings(self.user_id)
                if user_settings and "myfiles_auto_permanent" in user_settings:
                    auto_perm = user_settings["myfiles_auto_permanent"]

                perm_count = await db.files.count_documents({"user_id": self.user_id, "status": "permanent"})

                status = "temporary"
                if auto_perm and (perm_limit == -1 or perm_count < perm_limit):
                    status = "permanent"

                expiry_date = None
                if status == "temporary" and expiry_days != -1:
                    expiry_date = datetime.datetime.utcnow() + datetime.timedelta(days=expiry_days)

                folder_id = None
                dest_folder = self.data.get("dest_folder")
                skip_myfiles = False

                # Check if MyFiles system is globally enabled
                myfiles_enabled = await db.get_setting("myfiles_enabled", default=False)
                is_admin = (self.user_id == Config.CEO_ID or self.user_id in Config.ADMIN_IDS)
                if not myfiles_enabled and not is_admin:
                    skip_myfiles = True

                if dest_folder == "none":
                    skip_myfiles = True
                elif dest_folder and dest_folder != "root":
                    from bson.objectid import ObjectId
                    with contextlib.suppress(Exception):
                        folder_id = ObjectId(dest_folder)

                if (not dest_folder or dest_folder == "auto") and not skip_myfiles:
                    if self.tmdb_id:
                        folder_type = "series" if self.media_type == "series" else "movies"
                        folder = await db.folders.find_one({"user_id": self.user_id, "tmdb_id": self.tmdb_id})
                        if not folder:
                            res = await db.folders.insert_one({
                                "user_id": self.user_id,
                                "name": self.title,
                                "type": folder_type,
                                "tmdb_id": self.tmdb_id,
                                "created_at": datetime.datetime.utcnow()
                            })
                            folder_id = res.inserted_id
                        else:
                            folder_id = folder["_id"]
                    elif self.media_type == "audio":
                        artist = self.metadata.get("artist", "Unknown Artist").strip()
                        if not artist:
                            artist = "Unknown Artist"
                        folder = await db.folders.find_one({"user_id": self.user_id, "name": artist, "type": "music"})
                        if not folder:
                            res = await db.folders.insert_one({
                                "user_id": self.user_id,
                                "name": artist,
                                "type": "music",
                                "created_at": datetime.datetime.utcnow()
                            })
                            folder_id = res.inserted_id
                        else:
                            folder_id = folder["_id"]

                if not skip_myfiles:
                    # Smart System Filenames & TMDb Integration
                    internal_name = final_filename

                    try:
                        safe_title = re.sub(r'[\\/*?:"<>|,;\'!]', "", self.title) if self.title else ""
                        safe_title = safe_title.replace("&", "and")
                        year_str = str(self.year) if self.year else ""

                        # Pick the split key for the current media type.
                        # get_system_filename_template falls back to the
                        # legacy single `system_filename` key when neither
                        # split key is set, so existing installs keep
                        # working unchanged.
                        sys_scope = (
                            "system_filename_series"
                            if self.media_type == "series"
                            else "system_filename_movies"
                        )
                        system_filename_template = await db.get_system_filename_template(
                            self.media_type, self.user_id
                        )

                        if system_filename_template:
                            sys_fmt = await self._build_fmt_dict(
                                sys_scope, system_filename_template
                            )

                            base_name, sys_err = safe_format(
                                system_filename_template, sys_fmt
                            )
                            if sys_err:
                                logger.warning(
                                    f"System filename template {system_filename_template!r} "
                                    f"rejected ({sys_err}); using safe title fallback."
                                )
                                base_name = f"{safe_title}" if safe_title else "unknown"

                            def clean_sys_filename(name, orig_template=""):
                                import re
                                name = re.sub(r'\[\s*\]', '', name)
                                name = re.sub(r'\(\s*\)', '', name)
                                name = re.sub(r'\{\s*\}', '', name)
                                name = re.sub(r'[\._\s]{2,}', " ", name)
                                name = name.strip('._ ')
                                return name

                            file_ext = os.path.splitext(self.output_path)[1].lower() if self.output_path else ".mkv"
                            internal_name = clean_sys_filename(base_name, system_filename_template) + file_ext
                        elif self.title:
                            file_ext = os.path.splitext(self.output_path)[1].lower() if self.output_path else ".mkv"
                            if self.media_type == "series":
                                ep_str = "".join([f"E{int(e):02d}" for e in self.episode]) if isinstance(self.episode, list) else f"E{self.episode:02d}" if self.episode else ""
                                internal_name = f"{self.title} S{self.season:02d}{ep_str}{file_ext}"
                            elif self.year:
                                internal_name = f"{self.title} ({self.year}){file_ext}"
                            else:
                                internal_name = f"{self.title}{file_ext}"
                    except Exception as e:
                        logger.warning(f"Error applying system filename template: {e}")

                    try:
                        stored_size = os.path.getsize(self.output_path) if self.output_path else 0
                    except OSError:
                        stored_size = 0

                    file_data = {
                        "user_id": self.user_id,
                        "file_name": internal_name,
                        "message_id": saved_file_id,
                        "channel_id": storage_channel,
                        "status": status,
                        "folder_id": folder_id,
                        "created_at": datetime.datetime.utcnow(),
                        "expires_at": expiry_date,
                        "tmdb_id": self.tmdb_id,
                        "poster_url": self.poster_url,
                        "media_type": self.media_type,
                        "season": self.season,
                        "episode": self.episode,
                        # Size + quality power the MyFiles Insights
                        # dashboard (storage totals, biggest files) and
                        # richer file-detail screens.
                        "file_size": stored_size,
                        "quality": self.quality,
                    }
                    await db.files.insert_one(file_data)
            except Exception as e:
                logger.error(f"Failed to save file to DB Channel {storage_channel}: {e}")

            # Attach the inserted file to the queue item
            if batch_id and item_id and 'file_data' in locals():
                q_item = queue_manager.batches.get(batch_id).get_item(item_id) if queue_manager.batches.get(batch_id) else None
                if q_item:
                    file_doc = await db.files.find_one({"file_name": file_data["file_name"], "user_id": self.user_id}, sort=[("created_at", -1)])
                    if file_doc:
                        q_item.db_file_id = str(file_doc["_id"])

            if batch_id and item_id:
                if not dumb_channel:
                    queue_manager.update_status(batch_id, item_id, "done_dumb")
                else:
                    queue_manager.update_status(batch_id, item_id, "done_user")

                if queue_manager.is_batch_complete(batch_id) and not getattr(
                    queue_manager.batches.get(batch_id), "summary_sent", False
                ):
                    try:
                        deep_link = None

                        # Gather all successfully processed file IDs from the queue
                        batch_obj = queue_manager.batches.get(batch_id)
                        if batch_obj:
                            success_ids = [getattr(i, "db_file_id", None) for i in batch_obj.items.values() if i.status in ["done", "done_dumb", "done_user"] and getattr(i, "db_file_id", None)]

                            if success_ids:
                                import uuid
                                config = await db.get_public_config() if Config.PUBLIC_MODE else await db.settings.find_one({"_id": "global_settings"})
                                user_doc = await db.get_user(self.user_id)
                                is_premium = user_doc.get("is_premium", False) if user_doc else False
                                plan = user_doc.get("premium_plan", "standard") if is_premium else "free"

                                plan_features = config.get(f"premium_{plan}", {}).get("features", {})
                                batch_sharing_enabled = plan_features.get("batch_sharing", False)
                                has_batch_pro = self.data.get("has_batch_pro", False)
                                is_global_admin = (self.user_id == Config.CEO_ID or self.user_id in Config.ADMIN_IDS)

                                if (batch_sharing_enabled and has_batch_pro) or is_global_admin or plan == "global":
                                    user_settings = await db.get_settings(self.user_id)
                                    use_anon = user_settings.get("link_anonymity", False) if user_settings else False

                                    if use_anon:
                                        group_id = f"{uuid.uuid4().hex[:16]}"
                                    else:
                                        group_id = f"{self.user_id}_{int(datetime.datetime.utcnow().timestamp())}"

                                    group_doc = {
                                        "group_id": group_id,
                                        "user_id": self.user_id,
                                        "files": success_ids,
                                        "created_at": datetime.datetime.utcnow()
                                    }

                                    # Add expiry if auto-expire links is enabled
                                    auto_expire = user_settings.get("privacy_auto_expire_links", False) if user_settings else False
                                    if auto_expire:
                                        dur = user_settings.get("privacy_link_expiry_duration", "24h") if user_settings else "24h"
                                        dur_map = {"1h": 1, "6h": 6, "24h": 24, "7d": 168, "30d": 720}
                                        hours = dur_map.get(dur, 24)
                                        group_doc["expires_at"] = datetime.datetime.utcnow() + datetime.timedelta(hours=hours)

                                    await db.file_groups.insert_one(group_doc)

                                    bot_me = await self.client.get_me()
                                    bot_username = bot_me.username
                                    deep_link = f"https://t.me/{bot_username}?start=group_{group_id}"

                        summary_msg = queue_manager.get_batch_summary(batch_id, usage_text, deep_link)
                        await self.client.send_message(
                            self.user_id, summary_msg, disable_web_page_preview=True
                        )
                        with contextlib.suppress(Exception):
                            await self.client.send_sticker(self.user_id, "CAACAgIAAxkBAAEQa0xpgkMvycmQypya3zZxS5rU8tuKBQACwJ0AAjP9EEgYhDgLPnTykDgE")
                        if queue_manager.batches.get(batch_id):
                            queue_manager.batches.get(batch_id).summary_sent = True
                    except Exception as e:
                        logger.warning(f"Failed to send batch completion msg: {e}")

                if dumb_channel:
                    wait_start = time.time()
                    timeout = await db.get_dumb_channel_timeout()
                    wait_msg = None
                    last_wait_text = None

                    while True:
                        blocking_item = queue_manager.get_blocking_item(
                            batch_id, item_id
                        )
                        if not blocking_item:
                            break

                        if time.time() - wait_start > timeout:
                            logger.warning(
                                f"Timeout waiting for dumb channel upload for {final_filename}"
                            )
                            if wait_msg:
                                await wait_msg.delete()
                            break

                        wait_text = f"⏳ **Waiting for {blocking_item.display_name} to finish To send it in the dumb channel**"

                        if not wait_msg:
                            wait_msg = await self.message.reply_text(wait_text)
                            last_wait_text = wait_text
                        elif last_wait_text != wait_text:
                            try:
                                await wait_msg.edit_text(wait_text)
                                last_wait_text = wait_text
                            except Exception as e:
                                logger.warning(f"Failed to edit wait message: {e}")

                        await asyncio.sleep(5)

                    if wait_msg:
                        with contextlib.suppress(Exception):
                            await wait_msg.delete()

                    try:
                        from pyrogram.errors import PeerIdInvalid
                        try:
                            if is_tunneling:
                                await self.client.copy_message(
                                    chat_id=dumb_channel,
                                    from_chat_id=self.tunnel_id,
                                    message_id=media_msg.id,
                                )
                            else:
                                await self.client.copy_message(
                                    chat_id=dumb_channel,
                                    from_chat_id=media_msg.chat.id,
                                    message_id=media_msg.id,
                                )
                        except PeerIdInvalid:
                            await self.client.get_chat(dumb_channel)
                            if is_tunneling:
                                await self.client.copy_message(
                                    chat_id=dumb_channel,
                                    from_chat_id=self.tunnel_id,
                                    message_id=media_msg.id,
                                )
                            else:
                                await self.client.copy_message(
                                    chat_id=dumb_channel,
                                    from_chat_id=media_msg.chat.id,
                                    message_id=media_msg.id,
                                )
                        queue_manager.update_status(batch_id, item_id, "done_dumb")
                    except Exception as e:
                        logger.error(
                            f"Failed to copy {final_filename} to dumb channel {dumb_channel}: {e}"
                        )
                        queue_manager.update_status(batch_id, item_id, "failed", str(e))

            elif not batch_id:
                try:

                    await self.client.send_message(
                        self.user_id, f"✅ **Processing Complete!**\n\n📊 **Usage:** {usage_text.replace('Today: ', '')}"
                    )
                    with contextlib.suppress(Exception):
                        await self.client.send_sticker(self.user_id, "CAACAgIAAxkBAAEQa0xpgkMvycmQypya3zZxS5rU8tuKBQACwJ0AAjP9EEgYhDgLPnTykDgE")
                except Exception as e:
                    logger.warning(f"Failed to send single completion msg: {e}")

        except Exception as e:
            logger.error(f"Upload failed: {e}")
            await self._update_status(f"❌ **Upload Protocol Failed**\n\n`{e}`")
            batch_id = self.data.get("batch_id")
            item_id = self.data.get("item_id")
            if batch_id and item_id:
                queue_manager.update_status(batch_id, item_id, "failed", str(e))
        finally:
            if is_tunneling and self.tunnel_id:
                try:
                    await self.active_client.delete_channel(self.tunnel_id)
                    logger.info(f"Cleaned up ephemeral tunnel {self.tunnel_id}")
                except Exception as e:
                    logger.warning(
                        f"Failed to cleanup ephemeral tunnel {self.tunnel_id}: {e}"
                    )

    # ------------------------------------------------------------------
    # Template-system helpers
    # ------------------------------------------------------------------
    # Every inline `.format(**fmt_dict)` site across the task now goes
    # through `_build_fmt_dict(scope, template)`. It:
    #
    #  * populates BASIC/EPISODE/SOURCE/FILE eagerly (no I/O),
    #  * hits ffprobe only when the scope+template actually reference a
    #    TECHNICAL placeholder,
    #  * hits TMDb only when the scope+template reference a TMDB one,
    #  * memoises both on the instance so a batch of scopes within the
    #    same task never double-fetches,
    #  * filters to `allowed_fields_for(scope)` so we never leak a key
    #    a scope didn't opt into (preserves validate_template's contract),
    #  * applies `apply_trim` so long placeholders (Overview, Tagline,
    #    Genres, …) stay below the filename 255-byte limit.

    def _basic_vars(self):
        ec = self._eager_context or {}
        uploader = (
            f"@{self.data.get('username')}"
            if self.data.get("username")
            else f"id:{self.user_id}"
        )
        return {
            "Title":    ec.get("safe_title", self.title or ""),
            "title":    ec.get("safe_title", self.title or ""),
            "Year":     ec.get("year_str", ""),
            "year":     ec.get("year_str", ""),
            "Channel":  self.channel or "",
            "channel":  self.channel or "",
            "Language": self.language or "",
            "language": self.language or "",
            "lang":     self.language or "",
            "Uploader": uploader,
        }

    def _episode_vars(self):
        ec = self._eager_context or {}
        season_num = f"{self.season:02d}" if self.season else ""
        if isinstance(self.episode, list):
            ep_num = "".join(f"{int(e):02d}" for e in self.episode)
        elif self.episode:
            ep_num = f"{int(self.episode):02d}"
        else:
            ep_num = ""
        series_name = ec.get("safe_title", self.title or "") if self.media_type == "series" else ""
        return {
            "Season":         ec.get("season_str", ""),
            "SeasonNum":      season_num,
            "season":         season_num,
            "Episode":        ec.get("episode_str", ""),
            "EpisodeNum":     ep_num,
            "episode":        ep_num,
            "Season_Episode": ec.get("season_episode", ""),
            "season_episode": ec.get("season_episode", ""),
            "SeriesName":     series_name,
            "series_name":    series_name,
            "NumSeasons":     "",
            "NumEpisodes":    "",
            "FirstAirYear":   "",
        }

    def _source_vars(self):
        """Placeholders for the SOURCE group.

        User picks (made via the confirm-screen pickers and stored on
        ``self.data``) take precedence over the regex detector output
        in ``self._pattern_groups``. This way a filename that originally
        didn't carry the tag — or that the detector mismatched — still
        gets the user's explicit choice.

        ``{Specials}`` stays as the legacy aggregate. New sessions
        populate the split category keys (``source`` / ``hdr`` / …)
        and leave ``specials`` empty, so the aggregate folds across
        all sources without double-counting.
        """
        ec = self._eager_context or {}
        g = self._pattern_groups or {}
        sep = ec.get("pref_sep", ".")

        def _multi_str(user_val, detector_val):
            if user_val:
                if isinstance(user_val, list):
                    return sep.join(s for s in user_val if s)
                return str(user_val)
            if isinstance(detector_val, list):
                return sep.join(detector_val)
            return detector_val or ""

        source_val   = self.data.get("source")   or g.get("source") or ""
        hdr_val      = self.data.get("hdr")      or g.get("hdr") or ""
        edition_val  = _multi_str(self.data.get("edition"),  g.get("edition"))
        release_val  = _multi_str(self.data.get("release"),  g.get("release"))
        extras_val   = _multi_str(self.data.get("extras"),   g.get("extras"))

        # Build {Specials} as a deduped aggregate of every detected /
        # user-picked tag so legacy templates keep working.
        aggregate = []
        for piece in (source_val, hdr_val, edition_val, release_val, extras_val):
            if not piece:
                continue
            for part in piece.split(sep) if sep in piece else [piece]:
                part = part.strip()
                if part and part not in aggregate:
                    aggregate.append(part)
        legacy_specials = ec.get("specials_str", "")
        if legacy_specials:
            for part in legacy_specials.split(sep):
                part = part.strip()
                if part and part not in aggregate:
                    aggregate.append(part)
        specials_str = sep.join(aggregate)

        return {
            "Source":   source_val,
            "HDR":      hdr_val,
            "Edition":  edition_val,
            "Release":  release_val,
            "Extras":   extras_val,
            "Specials": specials_str,
            "Codec":    ec.get("codec_str", ""),
            "Audio":    ec.get("audio_str", ""),
        }

    def _file_vars(self):
        ec = self._eager_context or {}
        fname = os.path.splitext(self.original_name)[0] if self.original_name else ""
        ext_hint = ec.get("ext_hint", "")
        size_str = ""
        size_bytes = ""
        if self.output_path and os.path.exists(self.output_path):
            try:
                b = os.path.getsize(self.output_path)
                size_bytes = str(b)
                size_str = self._humanbytes(b)
            except OSError:
                pass
        now = _dt.datetime.utcnow()
        token = "".join(_secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
        # Hashtag derived from title (+ season for series) — alnum only.
        base_tag = re.sub(r"[^A-Za-z0-9]", "", self.title or "")
        if self.media_type == "series" and self.season:
            hashtag = f"#{base_tag}S{int(self.season):02d}" if base_tag else ""
        else:
            hashtag = f"#{base_tag}" if base_tag else ""
        return {
            "Filename":  fname,
            "filename":  fname,
            "Ext":       ext_hint,
            "Size":      size_str,
            "size":      size_str,
            "SizeBytes": size_bytes,
            "Date":      now.strftime("%Y-%m-%d"),
            "Time":      now.strftime("%H:%M"),
            "Random":    token,
            "random":    token,
            "Hashtag":   hashtag,
            "hashtag":   hashtag,
        }

    def _technical_vars(self, probe):
        """Extract TECHNICAL placeholders from a probe_file() result.
        Accepts the probe tuple shape (data, streams) used elsewhere as
        well as a raw dict — both occur in this codebase.
        """
        data = probe
        if isinstance(probe, tuple):
            data = probe[0] if probe else None
        if not isinstance(data, dict):
            return {}

        fmt = data.get("format") or {}
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio = next((s for s in streams if s.get("codec_type") == "audio"), {})

        try:
            total_sec = int(float(fmt.get("duration") or 0))
        except (TypeError, ValueError):
            total_sec = 0
        if total_sec:
            hh = total_sec // 3600
            mm = (total_sec % 3600) // 60
            ss = total_sec % 60
            duration_str = f"{hh:02d}:{mm:02d}:{ss:02d}"
        else:
            duration_str = ""

        width = video.get("width") or ""
        height = video.get("height") or ""
        resolution = f"{width}x{height}" if width and height else ""

        br_raw = fmt.get("bit_rate")
        bitrate_str = ""
        if br_raw:
            try:
                br_mbps = float(br_raw) / 1_000_000
                bitrate_str = f"{br_mbps:.1f} Mbps"
            except (TypeError, ValueError):
                bitrate_str = ""

        fr = video.get("r_frame_rate") or ""
        if fr and "/" in fr:
            try:
                num, den = fr.split("/")
                fr = f"{float(num) / float(den):.3f}" if float(den) else fr
            except (TypeError, ValueError, ZeroDivisionError):
                pass

        channels = audio.get("channels") or ""
        channel_layout = {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}.get(channels, str(channels) if channels else "")
        audio_lang = (audio.get("tags") or {}).get("language", "") or ""

        return {
            "Quality":        self.quality or "",
            "quality":        self.quality or "",
            "Resolution":     resolution,
            "resolution":     resolution,
            "Width":          str(width) if width else "",
            "Height":         str(height) if height else "",
            "Duration":       duration_str,
            "duration":       duration_str,
            "DurationSec":    str(total_sec) if total_sec else "",
            "VideoCodec":     video.get("codec_name") or "",
            "video_codec":    video.get("codec_name") or "",
            "AudioCodec":     audio.get("codec_name") or "",
            "audio_codec":    audio.get("codec_name") or "",
            "AudioChannels":  channel_layout,
            "audio_channels": channel_layout,
            "AudioLang":      audio_lang,
            "audio_lang":     audio_lang,
            "Bitrate":        bitrate_str,
            "FrameRate":      str(fr),
        }

    def _tmdb_vars(self, details):
        if not isinstance(details, dict):
            return {}
        ec = self._eager_context or {}
        sep = ec.get("pref_sep", ".")
        genres = details.get("genres") or []
        genres_str = (
            sep.join(g for g in genres if g)
            if isinstance(genres, list)
            else str(genres or "")
        )
        countries = details.get("production_countries") or []
        countries_str = sep.join(c for c in countries if c) if isinstance(countries, list) else str(countries or "")
        networks = details.get("networks") or []
        network_str = networks[0] if isinstance(networks, list) and networks else (str(networks or ""))
        rating_raw = details.get("vote_average") or 0
        try:
            rating_str = f"{float(rating_raw):.1f}" if rating_raw else ""
        except (TypeError, ValueError):
            rating_str = ""
        runtime_raw = details.get("runtime") or 0
        try:
            runtime_str = f"{int(runtime_raw)}m" if runtime_raw else ""
        except (TypeError, ValueError):
            runtime_str = ""
        release_date = details.get("release_date") or details.get("first_air_date") or ""
        first_air_year = (details.get("first_air_date") or "")[:4]

        return {
            "TMDbId":            str(self.tmdb_id or ""),
            "tmdb_id":           str(self.tmdb_id or ""),
            "OriginalTitle":     details.get("original_title") or "",
            "Overview":          details.get("overview") or "",
            "overview":          details.get("overview") or "",
            "Rating":            rating_str,
            "rating":            rating_str,
            "Runtime":           runtime_str,
            "runtime":           runtime_str,
            "Genres":            genres_str,
            "genres":            genres_str,
            "Tagline":           details.get("tagline") or "",
            "tagline":           details.get("tagline") or "",
            "OriginalLanguage":  details.get("original_language") or "",
            "Countries":         countries_str,
            "Network":           network_str,
            "network":           network_str,
            "ReleaseDate":       release_date,
            "release_date":      release_date,
            "NumSeasons":        str(details.get("number_of_seasons") or ""),
            "NumEpisodes":       str(details.get("number_of_episodes") or ""),
            "FirstAirYear":      first_air_year,
        }

    @staticmethod
    def _template_references_group(template, group_key):
        """Return True if ``template`` contains any placeholder belonging
        to ``group_key``. Cheap: walks Formatter.parse once and intersects
        with the group's field names.
        """
        if not template:
            return False
        try:
            from utils.template import CATALOG
            group = CATALOG.get(group_key)
            if not group:
                return False
            names = {f.name for f in group.fields}
            for _lit, field, _spec, _conv in string.Formatter().parse(template):
                if field is None:
                    continue
                root = field.split(".", 1)[0].split("[", 1)[0].strip()
                if root in names:
                    return True
        except ValueError:
            # Malformed template — safe_format will catch and log later.
            return False
        return False

    async def _probe_input_cached(self):
        if self._input_probe_data is not None:
            return self._input_probe_data
        if not self.input_path or not os.path.exists(self.input_path):
            return None
        try:
            self._input_probe_data = await probe_file(self.input_path)
        except Exception as e:
            logger.warning(f"probe_file(input) failed: {e}")
            self._input_probe_data = None
        return self._input_probe_data

    async def _probe_output_cached(self):
        if self._output_probe_data is not None:
            return self._output_probe_data
        if not self.output_path or not os.path.exists(self.output_path):
            # Fall back to input probe so caption generation still has
            # resolution / duration data if the output isn't ready yet.
            return await self._probe_input_cached()
        try:
            self._output_probe_data = await probe_file(self.output_path)
        except Exception as e:
            logger.warning(f"probe_file(output) failed: {e}")
            self._output_probe_data = None
        return self._output_probe_data

    async def _tmdb_details_cached(self):
        if self._tmdb_details_data is not None:
            return self._tmdb_details_data
        if not self.tmdb_id:
            return None
        try:
            self._tmdb_details_data = await tmdb.get_details_cached(
                self.tmdb_id, self.media_type
            )
        except Exception as e:
            logger.warning(f"tmdb.get_details_cached failed: {e}")
            self._tmdb_details_data = None
        return self._tmdb_details_data

    async def _build_fmt_dict(self, scope, template=None):
        """Return the placeholder mapping for ``scope`` filtered to its
        allowed fields and scope-aware-trimmed. ``template`` is optional
        — when provided, TECHNICAL / TMDB fetches are skipped entirely
        if the template text doesn't reference any placeholder from
        those groups.
        """
        values = {}
        values.update(self._basic_vars())
        values.update(self._episode_vars())
        values.update(self._source_vars())
        values.update(self._file_vars())

        groups = SCOPE_GROUPS.get(scope, ())

        needs_tech = "TECHNICAL" in groups and (
            template is None or self._template_references_group(template, "TECHNICAL")
        )
        if needs_tech:
            probe_source = (
                self._probe_output_cached
                if scope == "caption"
                else self._probe_input_cached
            )
            probe = await probe_source()
            if probe:
                values.update(self._technical_vars(probe))

        needs_tmdb = "TMDB" in groups and self.tmdb_id and (
            template is None or self._template_references_group(template, "TMDB")
        )
        if needs_tmdb:
            details = await self._tmdb_details_cached()
            if details:
                values.update(self._tmdb_vars(details))

        allowed = allowed_fields_for(scope)
        filtered = {k: v for k, v in values.items() if k in allowed}
        for name in allowed:
            filtered.setdefault(name, "")
        return apply_trim(filtered, scope)

    def _select_filename_scope(self):
        """Pick the scope name that matches the current rename flow."""
        personal = self.data.get("personal_type")
        if personal in ("video", "photo", "file"):
            return f"filename_personal_{personal}"
        if self.media_type == "general":
            return "filename_general"
        if self.media_type == "series":
            return "filename_subs_series" if self.is_subtitle else "filename_series"
        return "filename_subs_movies" if self.is_subtitle else "filename_movies"

    async def _generate_caption(self, filename: str) -> str:
        template = self.templates.get("caption", "{random}")

        # Keep the `{random}`-only shortcut — existing users rely on this
        # as a cheap unique-id generator; skipping the probe+tmdb path
        # for it saves ~50 ms per upload.
        if "{random}" in template or template == "{random}":
            return "".join(random.choices(string.ascii_letters + string.digits, k=16))

        fmt = await self._build_fmt_dict("caption", template)
        # Preserve the original `{filename}` semantics — final_filename
        # takes precedence over the FILE group's self.original_name value.
        fmt["filename"] = filename
        fmt["Filename"] = filename

        caption, caption_err = safe_format(template, fmt)
        if caption_err:
            logger.warning(
                f"Caption template {template!r} rejected ({caption_err}); using filename."
            )
            return filename
        return caption

    @staticmethod
    def _humanbytes(size: int) -> str:
        if not size:
            return ""
        power = 2**10
        n = 0
        dic_power = {0: " ", 1: "K", 2: "M", 3: "G", 4: "T"}
        while size > power:
            size /= power
            n += 1
        return str(round(size, 2)) + " " + dic_power[n] + "B"

    def _cancel_markup(self):
        """Inline keyboard with a Cancel button bound to this task.

        Returns None when the task has already entered a terminal state so
        final success/error messages don't carry a stale Cancel button.
        """
        if getattr(self, "_terminal_status", False):
            return None
        if not self.status_msg:
            return None
        try:
            from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        except Exception:
            return None
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "❌ Cancel Task",
                callback_data=f"cancel_task_{self.status_msg.id}",
            )
        ]])

    async def _update_status(self, text: str, *, terminal: bool = False):
        from pyrogram.errors import FloodWait, MessageIdInvalid
        if terminal:
            self._terminal_status = True
        markup = self._cancel_markup()
        for _attempt in range(3):
            try:
                if self.status_msg:
                    if markup is not None:
                        await self.status_msg.edit_text(text, reply_markup=markup)
                    else:
                        await self.status_msg.edit_text(text)
                return
            except FloodWait as e:
                logger.warning(f"FloodWait in _update_status: sleeping for {e.value}s")
                await asyncio.sleep(e.value + 1)
            except MessageIdInvalid:
                logger.warning("MessageIdInvalid in _update_status: message was likely deleted.")
                return
            except MessageNotModified:
                return
            except Exception as e:
                logger.warning(f"Failed to update status message: {e}")
                return

    async def _cleanup(self):
        for path in [self.input_path, self.output_path, self.thumb_path]:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    logger.warning(f"Failed to remove temp file {path}: {e}")

        if self.mode == "pro" and self.tunnel_id:
            with contextlib.suppress(Exception):
                await self.active_client.delete_channel(self.tunnel_id)

        if self.data.get("extract_dir") and self.data.get("batch_id"):
            extract_dir = self.data.get("extract_dir")
            batch_id = self.data.get("batch_id")

            if queue_manager.is_batch_complete(batch_id) and os.path.exists(extract_dir):
                try:
                    shutil.rmtree(extract_dir, ignore_errors=True)
                except Exception as e:
                    logger.warning(f"Failed to remove extraction directory {extract_dir}: {e}")

        if not getattr(self, "processing_successful", False):
            try:
                original_size = 0
                if self.file_message:
                    media = self.file_message.document or self.file_message.video or self.file_message.audio or self.file_message.photo
                    original_size = getattr(media, "file_size", 0) if media else 0

                if original_size > 0:
                    await db.release_quota(self.user_id, original_size)
            except Exception as e:
                logger.error(f"Failed to release quota in cleanup: {e}")

@Client.on_callback_query(lambda _, __, q: q.data and q.data.startswith("cancel_task_"))
async def handle_cancel_task(client, callback_query):
    """User pressed Cancel Task on a status message. Cancels the task
    registered for that status-message id (no-op if already done)."""
    from utils.tasks import cancel_by_key

    try:
        status_msg_id = int(callback_query.data.split("_", 2)[2])
    except (ValueError, IndexError):
        await callback_query.answer("Invalid cancel request.", show_alert=False)
        return

    cancelled = cancel_by_key(status_msg_id)
    if cancelled:
        await callback_query.answer("⛔️ Cancelling task...", show_alert=False)
    else:
        await callback_query.answer(
            "Task already finished or not cancellable.", show_alert=False
        )


async def process_file(client, message, data):
    """Entry point for file processing. Wrapped to ensure any unexpected
    failure (e.g. DB down before TaskProcessor can take over) still lands
    with a user-facing message instead of dying silently in a spawned task."""
    user_id = message.from_user.id if message.from_user else message.chat.id

    try:
        await db.ensure_user(
            user_id=user_id,
            first_name=message.from_user.first_name if message.from_user else message.chat.title,
            username=message.from_user.username if message.from_user else None,
            last_name=message.from_user.last_name if message.from_user else None,
            language_code=message.from_user.language_code if message.from_user else None,
            is_bot=message.from_user.is_bot if message.from_user else False,
        )
    except Exception as e:
        logger.exception(f"ensure_user failed for {user_id}: {e}")
        with contextlib.suppress(Exception):
            await message.edit_text(
                "❌ **Database Error**\n\n"
                "Could not initialize your session. Please try again in a moment.\n"
                "If this persists, notify an admin."
            )
        return

    processor = TaskProcessor(client, message, data)
    try:
        await processor.run()
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await message.edit_text("⛔️ **Task Cancelled** by user.")
        raise
    except Exception as e:
        logger.exception(f"process_file uncaught for user={user_id}: {e}")
        with contextlib.suppress(Exception):
            await message.edit_text(
                f"❌ **Unexpected Error**\n\n`{type(e).__name__}: {e}`\n\n"
                "The task was aborted. Your quota has been released."
            )

# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# Telegram Channel @XTVbots
# Developed for the 𝕏TV Network @XTVglobal
# Backup Channel @XTVhome
# Contact on Telegram @davdxpx
# --------------------------------------------------------------------------
