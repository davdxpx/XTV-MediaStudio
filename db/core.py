# --- Imports ---
import contextlib
import datetime
import time

import certifi
from motor.motor_asyncio import AsyncIOMotorClient

from config import Config
from db import schema as _schema
from db.shim import SettingsCollectionShim
from utils.telegram.log import get_logger

logger = get_logger("database")

# === Classes ===
class Database:
    _SETTINGS_CACHE_TTL = 60  # seconds

    # Exposed for the admin DB Schema Health panel and migration tooling so
    # they can introspect the routing tables without reaching into globals.
    schema = _schema

    def __init__(self):
        self._settings_cache = {}  # doc_id -> (timestamp, doc)

        if not Config.MAIN_URI:
            logger.warning("MAIN_URI is not set in environment variables.")
            self.client = None
            self.db = None
            self.settings = None
            self.users = None
            self.daily_stats = None
            self.pending_payments = None
            self.files = None
            self.folders = None
            self.file_groups = None
            # MyFiles extras (audit / activity / quotas / shares)
            self.myfiles_audit = None
            self.myfiles_activity = None
            self.myfiles_quotas = None
            self.myfiles_shares = None
            self.ml_queue = None
            self.ml_history = None
        else:
            try:
                self.client = AsyncIOMotorClient(
                    Config.MAIN_URI, tlsCAFile=certifi.where()
                )
            except Exception as e:
                logger.error(
                    f"MongoDB SSL connection failed: {e}\n"
                    "  Fix: Ensure your MongoDB URI uses a valid TLS certificate,\n"
                    "  or update certifi: pip install --upgrade certifi"
                )
                raise

            self.db = self.client[Config.DB_NAME]

            # MediaStudio layout. `self.settings` is a back-compat shim over
            # the new per-concern docs; it transparently routes legacy access
            # patterns (find_one({"_id": "global_settings"}), etc.). All other
            # attributes point directly at their renamed collections.
            self.users = self.db[_schema.USERS_COLLECTION]
            self.settings = SettingsCollectionShim(
                self.db[_schema.SETTINGS_COLLECTION],
                self.users,
                ceo_id=Config.CEO_ID or None,
                public_mode=Config.PUBLIC_MODE,
            )
            self.daily_stats = self.db[_schema.DAILY_STATS_COLLECTION]
            self.pending_payments = self.db[_schema.PENDING_PAYMENTS_COLLECTION]
            self.files = self.db[_schema.FILES_COLLECTION]
            self.folders = self.db[_schema.FOLDERS_COLLECTION]
            self.file_groups = self.db[_schema.FILE_GROUPS_COLLECTION]
            # MyFiles extras collections (created lazily on first write;
            # indexes are established by db_migrations/myfiles_extras_v1).
            self.myfiles_audit = self.db[_schema.MYFILES_AUDIT_COLLECTION]
            self.myfiles_activity = self.db[_schema.MYFILES_ACTIVITY_COLLECTION]
            self.myfiles_quotas = self.db[_schema.MYFILES_QUOTAS_COLLECTION]
            self.myfiles_shares = self.db[_schema.MYFILES_SHARES_COLLECTION]
            # Mirror-Leech persistent queue (scheduled + retry-backoff
            # bookkeeping). Indexes are created lazily by Queue.ensure_indexes().
            self.ml_queue = self.db[_schema.ML_QUEUE_COLLECTION]
            # Mirror-Leech finished-task history (History screen + Repeat).
            # Indexes are created lazily by History.ensure_indexes().
            self.ml_history = self.db[_schema.ML_HISTORY_COLLECTION]
            # Usage tracking — rich per-day / lifetime / daily-global rollups
            # (PR E). Indexes are created by the usage_collection_v1 migration.
            self.usage = self.db[_schema.USAGE_COLLECTION]
            self.usage_alltime = self.db[_schema.USAGE_ALLTIME_COLLECTION]
            self.usage_daily_global = self.db[_schema.USAGE_DAILY_GLOBAL_COLLECTION]
            # UsageTracker facade — every write/read for usage counters
            # goes through this object so the three collections stay in
            # sync atomically. See db/usage.py.
            from db.usage import init_usage_tracker
            self.usage_tracker = init_usage_tracker(self)

    def _invalidate_settings_cache(self, user_id=None):
        doc_id = self._get_doc_id(user_id)
        self._settings_cache.pop(doc_id, None)

    async def get_setting(self, key, default=None, user_id=None):
        settings = await self.get_settings(user_id)
        return settings.get(key, default) if settings else default

    async def update_setting(self, key, value, user_id=None):
        if self.settings is None:
            return
        doc_id = self._get_doc_id(user_id)
        try:
            await self.settings.update_one(
                {"_id": doc_id}, {"$set": {key: value}}, upsert=True
            )
            self._invalidate_settings_cache(user_id)
        except Exception as e:
            logger.error(f"Error updating setting {key} for {doc_id}: {e}")

    async def save_flow_session(self, user_id: int, session_data: dict):
        if self.users is None:
            return
        import time as _time
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"flow_session": session_data, "flow_session_updated": _time.time()}},
            upsert=True
        )

    async def get_flow_session(self, user_id: int):
        if self.users is None:
            return None
        doc = await self.users.find_one({"user_id": user_id})
        return doc.get("flow_session") if doc else None

    async def clear_flow_session(self, user_id: int):
        if self.users is None:
            return
        await self.users.update_one(
            {"user_id": user_id},
            {"$unset": {"flow_session": "", "flow_session_updated": ""}}
        )

    def _get_doc_id(self, user_id=None):
        """Return the virtual doc id for a settings lookup.

        The SettingsCollectionShim routes "global_settings" and "user_<id>"
        transparently onto the MediaStudio layout, so call sites don't need
        to know anything about the underlying split.

        Non-public mode intentionally ignores ``user_id``: in a single-
        tenant private bot every caller — admin panels, rename flow,
        dumb-channel wizard — must see the same configuration. Honouring
        ``user_id`` here would silently split the same user's data across
        two docs (admin reads global, rename reads ``user_<id>``) which is
        exactly the symptom v1.6.0-alpha shipped with. In public mode the
        split is intended and per-user settings work as before.
        """
        if not Config.PUBLIC_MODE:
            return "global_settings"
        if user_id is not None:
            return f"user_{user_id}"
        return "global_settings"

    async def get_settings(self, user_id=None):
        if self.settings is None:
            return None

        doc_id = self._get_doc_id(user_id)

        # Check TTL cache first
        now = time.time()
        if doc_id in self._settings_cache:
            cached_time, cached_doc = self._settings_cache[doc_id]
            if now - cached_time < self._SETTINGS_CACHE_TTL:
                return cached_doc

        try:
            doc = await self.settings.find_one({"_id": doc_id})
            if not doc:
                default_settings = {
                    "_id": doc_id,
                    "thumbnail_file_id": None,
                    "thumbnail_binary": None,
                    "thumbnail_mode": "none",
                    "templates": Config.DEFAULT_TEMPLATES,
                    "filename_templates": Config.DEFAULT_FILENAME_TEMPLATES,
                    "channel": Config.DEFAULT_CHANNEL,
                    "preferred_language": "en-US",
                    "preferred_separator": ".",
                }
                await self.settings.insert_one(default_settings)
                self._settings_cache[doc_id] = (now, default_settings)
                return default_settings
            self._settings_cache[doc_id] = (now, doc)
            return doc
        except Exception as e:
            logger.error(f"Error fetching settings for {doc_id}: {e}")
            return None

    async def update_template(self, key, value, user_id=None):
        if self.settings is None:
            return
        doc_id = self._get_doc_id(user_id)
        try:
            await self.settings.update_one(
                {"_id": doc_id}, {"$set": {f"templates.{key}": value}}, upsert=True
            )
            self._invalidate_settings_cache(user_id)
        except Exception as e:
            logger.error(f"Error updating template for {doc_id}: {e}")

    async def update_thumbnail(self, file_id, binary_data, user_id=None):
        if self.settings is None:
            return
        doc_id = self._get_doc_id(user_id)
        try:
            await self.settings.update_one(
                {"_id": doc_id},
                {
                    "$set": {
                        "thumbnail_file_id": file_id,
                        "thumbnail_binary": binary_data,
                    }
                },
                upsert=True,
            )
            self._invalidate_settings_cache(user_id)
        except Exception as e:
            logger.error(f"Error updating thumbnail for {doc_id}: {e}")

    async def get_thumbnail(self, user_id=None):
        if self.settings is None:
            return None, None
        doc_id = self._get_doc_id(user_id)
        try:
            doc = await self.settings.find_one({"_id": doc_id})
            if doc:
                return doc.get("thumbnail_binary"), doc.get("thumbnail_file_id")
        except Exception as e:
            logger.error(f"Error fetching thumbnail for {doc_id}: {e}")
        return None, None

    async def get_thumbnail_mode(self, user_id=None):
        settings = await self.get_settings(user_id)
        if settings:
            return settings.get("thumbnail_mode", "none")
        return "none"

    async def update_thumbnail_mode(self, mode: str, user_id=None):
        if self.settings is None:
            return
        doc_id = self._get_doc_id(user_id)
        try:
            await self.settings.update_one(
                {"_id": doc_id}, {"$set": {"thumbnail_mode": mode}}, upsert=True
            )
            self._invalidate_settings_cache(user_id)
        except Exception as e:
            logger.error(f"Error updating thumbnail mode for {doc_id}: {e}")

    async def get_all_templates(self, user_id=None):
        settings = await self.get_settings(user_id)
        if settings:
            return settings.get("templates", Config.DEFAULT_TEMPLATES)
        return Config.DEFAULT_TEMPLATES

    async def get_system_filename_template(self, media_type, user_id=None):
        """Return the per-media-type system filename template.

        Preference order: explicit per-type key → legacy ``system_filename``
        key (so an install that used the single-key editor keeps working
        after the split) → Config default. Never raises.
        """
        templates = await self.get_all_templates(user_id)
        key = "system_filename_series" if media_type == "series" else "system_filename_movies"
        val = templates.get(key)
        if val:
            return val
        legacy = templates.get("system_filename")
        if legacy:
            return legacy
        return (
            Config.DEFAULT_SYSTEM_FILENAME_SERIES
            if media_type == "series"
            else Config.DEFAULT_SYSTEM_FILENAME_MOVIES
        )

    async def get_filename_templates(self, user_id=None):
        settings = await self.get_settings(user_id)
        raw = (
            settings.get("filename_templates", Config.DEFAULT_FILENAME_TEMPLATES)
            if settings
            else Config.DEFAULT_FILENAME_TEMPLATES
        )
        return self._normalize_template_keys(raw)

    @staticmethod
    def _normalize_template_keys(templates):
        """Accept legacy singular keys (e.g. 'movie', 'subtitles_movie') and
        expose them under the canonical plural keys used by Config defaults.
        Prevents Movie auto-detect confirmation losing Audio/Codec/Specials
        buttons when stored templates use a different key variant."""
        if not isinstance(templates, dict):
            return templates
        aliases = {
            "movie": "movies",
            "subtitles_movie": "subtitles_movies",
        }
        normalized = dict(templates)
        for legacy, canonical in aliases.items():
            if legacy in normalized and canonical not in normalized:
                normalized[canonical] = normalized[legacy]
        return normalized

    async def update_filename_template(self, key, value, user_id=None):
        if self.settings is None:
            return
        doc_id = self._get_doc_id(user_id)
        try:
            await self.settings.update_one(
                {"_id": doc_id},
                {"$set": {f"filename_templates.{key}": value}},
                upsert=True,
            )
            self._invalidate_settings_cache(user_id)
        except Exception as e:
            logger.error(f"Error updating filename template for {doc_id}: {e}")

    async def get_channel(self, user_id=None):
        settings = await self.get_settings(user_id)
        if settings:
            return settings.get("channel", Config.DEFAULT_CHANNEL)
        return Config.DEFAULT_CHANNEL

    async def update_channel(self, value, user_id=None):
        if self.settings is None:
            return
        doc_id = self._get_doc_id(user_id)
        try:
            await self.settings.update_one(
                {"_id": doc_id}, {"$set": {"channel": value}}, upsert=True
            )
            self._invalidate_settings_cache(user_id)
        except Exception as e:
            logger.error(f"Error updating channel for {doc_id}: {e}")

    async def get_preferred_language(self, user_id=None):
        settings = await self.get_settings(user_id)
        if settings:
            return settings.get("preferred_language", "en-US")
        return "en-US"

    async def update_preferred_language(self, value, user_id=None):
        if self.settings is None:
            return
        doc_id = self._get_doc_id(user_id)
        try:
            await self.settings.update_one(
                {"_id": doc_id}, {"$set": {"preferred_language": value}}, upsert=True
            )
            self._invalidate_settings_cache(user_id)
        except Exception as e:
            logger.error(f"Error updating preferred language for {doc_id}: {e}")

    async def get_preferred_separator(self, user_id=None):
        settings = await self.get_settings(user_id)
        if settings:
            return settings.get("preferred_separator", ".")
        return "."

    async def update_preferred_separator(self, value, user_id=None):
        if self.settings is None:
            return
        doc_id = self._get_doc_id(user_id)
        try:
            await self.settings.update_one(
                {"_id": doc_id}, {"$set": {"preferred_separator": value}}, upsert=True
            )
            self._invalidate_settings_cache(user_id)
        except Exception as e:
            logger.error(f"Error updating preferred separator for {doc_id}: {e}")

    async def get_workflow_mode(self, user_id=None):
        settings = await self.get_settings(user_id)
        if settings:
            return settings.get("workflow_mode", "smart_media_mode")
        return "smart_media_mode"

    async def update_workflow_mode(self, mode: str, user_id=None):
        if self.settings is None:
            return
        doc_id = self._get_doc_id(user_id)
        try:
            await self.settings.update_one(
                {"_id": doc_id}, {"$set": {"workflow_mode": mode}}, upsert=True
            )
            self._invalidate_settings_cache(user_id)
        except Exception as e:
            logger.error(f"Error updating workflow mode for {doc_id}: {e}")

    async def has_completed_setup(self, user_id=None):
        settings = await self.get_settings(user_id)
        if settings:
            return settings.get("setup_completed", False)
        return False

    async def mark_setup_completed(self, user_id=None, completed: bool = True):
        if self.settings is None:
            return
        doc_id = self._get_doc_id(user_id)
        try:
            await self.settings.update_one(
                {"_id": doc_id}, {"$set": {"setup_completed": completed}}, upsert=True
            )
            self._invalidate_settings_cache(user_id)
        except Exception as e:
            logger.error(f"Error updating setup_completed for {doc_id}: {e}")

    async def get_dumb_channels(self, user_id=None):
        settings = await self.get_settings(user_id)
        if settings:
            return settings.get("dumb_channels", {})
        return {}

    async def add_dumb_channel(
        self, channel_id, channel_name, invite_link=None, user_id=None
    ):
        if self.settings is None:
            return
        doc_id = self._get_doc_id(user_id)
        try:
            update_data = {f"dumb_channels.{channel_id}": channel_name}
            if invite_link:
                update_data[f"dumb_channel_links.{channel_id}"] = invite_link

            await self.settings.update_one(
                {"_id": doc_id}, {"$set": update_data}, upsert=True
            )
        except Exception as e:
            logger.error(f"Error adding dumb channel for {doc_id}: {e}")

    async def get_all_dumb_channel_links(self):
        if self.settings is None:
            return []
        links = set()
        async for doc in self.settings.find({"dumb_channel_links": {"$exists": True}}):
            for link in doc.get("dumb_channel_links", {}).values():
                if link:
                    links.add(link)
        return list(links)

    async def remove_dumb_channel(self, channel_id, user_id=None):
        if self.settings is None:
            return
        doc_id = self._get_doc_id(user_id)
        try:
            await self.settings.update_one(
                {"_id": doc_id},
                {"$unset": {f"dumb_channels.{channel_id}": ""}},
                upsert=True,
            )
            settings = await self.get_settings(user_id)
            if settings and settings.get("default_dumb_channel") == str(channel_id):
                await self.settings.update_one(
                    {"_id": doc_id},
                    {"$unset": {"default_dumb_channel": ""}},
                    upsert=True,
                )
        except Exception as e:
            logger.error(f"Error removing dumb channel for {doc_id}: {e}")

    async def get_default_dumb_channel(self, user_id=None):
        settings = await self.get_settings(user_id)
        if settings:
            return settings.get("default_dumb_channel")
        return None

    async def set_default_dumb_channel(self, channel_id, user_id=None):
        if self.settings is None:
            return
        doc_id = self._get_doc_id(user_id)
        try:
            await self.settings.update_one(
                {"_id": doc_id},
                {"$set": {"default_dumb_channel": str(channel_id)}},
                upsert=True,
            )
        except Exception as e:
            logger.error(f"Error setting default dumb channel for {doc_id}: {e}")

    async def get_movie_dumb_channel(self, user_id=None):
        settings = await self.get_settings(user_id)
        if settings:
            return settings.get("movie_dumb_channel")
        return None

    async def set_movie_dumb_channel(self, channel_id, user_id=None):
        if self.settings is None:
            return
        doc_id = self._get_doc_id(user_id)
        try:
            await self.settings.update_one(
                {"_id": doc_id},
                {"$set": {"movie_dumb_channel": str(channel_id)}},
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"Failed to set movie dumb channel: {e}")
            return False

    async def get_series_dumb_channel(self, user_id=None):
        settings = await self.get_settings(user_id)
        if settings:
            return settings.get("series_dumb_channel")
        return None

    async def set_series_dumb_channel(self, channel_id, user_id=None):
        if self.settings is None:
            return
        doc_id = self._get_doc_id(user_id)
        try:
            await self.settings.update_one(
                {"_id": doc_id},
                {"$set": {"series_dumb_channel": str(channel_id)}},
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"Failed to set series dumb channel: {e}")
            return False

    async def get_dumb_channel_timeout(self):
        if self.settings is None:
            return 3600
        if Config.PUBLIC_MODE:
            config = await self.get_public_config()
            return config.get("dumb_channel_timeout", 3600)
        else:
            doc = await self.settings.find_one({"_id": "global_settings"})
            if doc:
                return doc.get("dumb_channel_timeout", 3600)
            return 3600

    async def update_dumb_channel_timeout(self, timeout_seconds: int):
        if self.settings is None:
            return
        try:
            if Config.PUBLIC_MODE:
                await self.update_public_config("dumb_channel_timeout", timeout_seconds)
            else:
                await self.settings.update_one(
                    {"_id": "global_settings"},
                    {"$set": {"dumb_channel_timeout": timeout_seconds}},
                    upsert=True,
                )
        except Exception as e:
            logger.error(f"Error updating dumb channel timeout: {e}")

    async def get_pro_session(self):
        """Return the full xtv_pro_settings document, or None if absent.

        Callers expect a flat dict with at least the legacy keys
        (session_string, api_id, api_hash, tunnel_id, tunnel_link). New
        operational fields (phone_number, userbot_user_id, etc.) are also
        passed through transparently — no field is dropped.
        """
        if self.settings is None:
            return None
        doc = await self.settings.find_one({"_id": "xtv_pro_settings"})
        if not doc:
            return None
        result = dict(doc)
        result.pop("_id", None)
        return result

    async def save_pro_tunnel(self, tunnel_id: int, tunnel_link: str):
        if self.settings is None:
            return
        await self.settings.update_one(
            {"_id": "xtv_pro_settings"},
            {"$set": {"tunnel_id": tunnel_id, "tunnel_link": tunnel_link}},
            upsert=True,
        )

    async def save_pro_session(
        self,
        session_string: str,
        api_id: int = None,
        api_hash: str = None,
        *,
        phone_number: str = None,
        userbot_user_id: int = None,
        userbot_first_name: str = None,
        userbot_username: str = None,
        is_premium: bool = None,
        premium_expires_at=None,
    ):
        if self.settings is None:
            return

        now = datetime.datetime.utcnow()
        update_doc = {"session_string": session_string}
        if api_id and api_hash:
            update_doc["api_id"] = api_id
            update_doc["api_hash"] = api_hash
        if phone_number is not None:
            update_doc["phone_number"] = phone_number
        if userbot_user_id is not None:
            update_doc["userbot_user_id"] = userbot_user_id
        if userbot_first_name is not None:
            update_doc["userbot_first_name"] = userbot_first_name
        if userbot_username is not None:
            update_doc["userbot_username"] = userbot_username
        if is_premium is not None:
            update_doc["is_premium"] = bool(is_premium)
        if premium_expires_at is not None:
            update_doc["premium_expires_at"] = premium_expires_at

        await self.settings.update_one(
            {"_id": "xtv_pro_settings"},
            {
                "$set": update_doc,
                "$setOnInsert": {"authorised_at": now},
            },
            upsert=True,
        )
        # Always refresh authorised_at on a fresh login (covers re-auth) by
        # checking whether session_string changed via a separate touch.
        await self.settings.update_one(
            {"_id": "xtv_pro_settings"}, {"$set": {"authorised_at": now}}
        )

    async def update_pro_session(self, **fields):
        """Partial update for the xtv_pro_settings doc — used by the admin
        Manage screen for Health Check / Change Tunnel / clearing flags.
        """
        if self.settings is None or not fields:
            return
        await self.settings.update_one(
            {"_id": "xtv_pro_settings"}, {"$set": fields}, upsert=False
        )

    async def log_pro_upload(self, bytes_count: int):
        """Increment Pro tunnel telemetry counters after a successful upload.
        Best-effort: errors are swallowed at the call site.
        """
        if self.settings is None:
            return
        try:
            await self.settings.update_one(
                {"_id": "xtv_pro_settings"},
                {
                    "$inc": {
                        "upload_count_total": 1,
                        "upload_bytes_total": int(bytes_count or 0),
                    },
                    "$set": {"last_upload_at": datetime.datetime.utcnow()},
                },
                upsert=False,
            )
        except Exception as e:
            logger.warning(f"log_pro_upload failed: {e}")

    async def delete_pro_session(self):
        if self.settings is None:
            return
        await self.settings.delete_one({"_id": "xtv_pro_settings"})

    # === YouTube cookies persistence ======================================
    # Stored as a single doc so cookies survive container redeploys without
    # needing a volume mount. The on-disk file at config/yt_cookies.txt is
    # recreated from this doc at bot startup.
    async def get_youtube_cookies(self):
        """Return {'cookies': str, 'updated_at': datetime, 'uploaded_by': int}
        or None if no cookies have been saved yet."""
        if self.settings is None:
            return None
        try:
            doc = await self.settings.find_one({"_id": "youtube_cookies"})
        except Exception as e:
            logger.warning(f"get_youtube_cookies failed: {e}")
            return None
        if not doc or not doc.get("cookies"):
            return None
        return {
            "cookies": doc.get("cookies", ""),
            "updated_at": doc.get("updated_at"),
            "uploaded_by": doc.get("uploaded_by"),
        }

    async def save_youtube_cookies(self, cookies_text: str, uploaded_by: int | None = None):
        """Persist a Netscape-format cookies.txt blob to MongoDB."""
        if self.settings is None:
            return False
        if not cookies_text:
            return False
        try:
            await self.settings.update_one(
                {"_id": "youtube_cookies"},
                {"$set": {
                    "cookies": cookies_text,
                    "updated_at": datetime.datetime.utcnow(),
                    "uploaded_by": uploaded_by,
                }},
                upsert=True,
            )
            return True
        except Exception as e:
            logger.warning(f"save_youtube_cookies failed: {e}")
            return False

    async def delete_youtube_cookies(self):
        """Remove the persisted YouTube cookies document, if any."""
        if self.settings is None:
            return False
        try:
            res = await self.settings.delete_one({"_id": "youtube_cookies"})
            return bool(res.deleted_count)
        except Exception as e:
            logger.warning(f"delete_youtube_cookies failed: {e}")
            return False

    async def get_public_config(self):
        if self.settings is None:
            return {}
        try:
            doc = await self.settings.find_one({"_id": "public_mode_config"})
            if not doc:
                default_config = {
                    "_id": "public_mode_config",
                    "bot_name": "𝕏TV MediaStudio™",
                    "community_name": "Our Community",
                    "support_contact": "@davdxpx",
                    "force_sub_channel": None,
                    "force_sub_link": None,
                    "force_sub_username": None,
                    "force_sub_banner_file_id": None,
                    "force_sub_message_text": None,
                    "force_sub_button_label": None,
                    "force_sub_button_emoji": None,
                    "force_sub_channels": [],
                    "force_sub_welcome_text": None,
                    "daily_egress_mb": 0,
                    "daily_file_count": 0,
                    "global_daily_egress_mb": 0,
                    "premium_system_enabled": False,
                    "premium_trial_enabled": False,
                    "premium_trial_days": 1,
                    "premium_deluxe_enabled": False,
                    "currency_conversion_enabled": True,
                    "base_currency": "USD",
                    "stars_payment_enabled": False,
                    "xtv_pro_4gb_access": "all",
                    "premium_standard": {
                        "daily_egress_mb": 0,
                        "daily_file_count": 0,
                        "price_string": "0 USD",
                        "stars_price": 0,
                        "features": {
                            "priority_queue": False,
                            "xtv_pro_4gb": False,
                            "file_converter": True,
                            "audio_editor": True,
                            "watermarker": True,
                            "subtitle_extractor": True,
                            "video_trimmer": True,
                            "media_info": True,
                            "voice_converter": True,
                            "video_note_converter": True,
                            "youtube_tool": True,
                            "4k_enhancement": True,
                            "batch_processing_pro": True
                        }
                    },
                    "premium_deluxe": {
                        "daily_egress_mb": 0,
                        "daily_file_count": 0,
                        "price_string": "0 USD",
                        "stars_price": 0,
                        "features": {
                            "priority_queue": True,
                            "xtv_pro_4gb": True,
                            "file_converter": True,
                            "audio_editor": True,
                            "watermarker": True,
                            "subtitle_extractor": True,
                            "video_trimmer": True,
                            "media_info": True,
                            "voice_converter": True,
                            "video_note_converter": True,
                            "youtube_tool": True,
                            "4k_enhancement": True,
                            "batch_processing_pro": True
                        }
                    },
                    "payment_methods": {
                        "paypal_enabled": False,
                        "paypal_email": "",
                        "crypto_enabled": False,
                        "crypto_usdt": "",
                        "crypto_btc": "",
                        "crypto_eth": "",
                        "upi_enabled": False,
                        "upi_id": "",
                        "stars_enabled": False
                    },
                    "discounts": {
                        "months_3": 0,
                        "months_12": 0
                },
                "database_channels": {
                    "free": None,
                    "standard": None,
                    "deluxe": None
                },
                "myfiles_limits": {
                    "free": {
                        "permanent_limit": 50,
                        "folder_limit": 5,
                        "expiry_days": 10
                    },
                    "standard": {
                        "permanent_limit": 1000,
                        "folder_limit": 50,
                        "expiry_days": 30
                    },
                    "deluxe": {
                        "permanent_limit": -1, # -1 for unlimited
                        "folder_limit": -1,
                        "expiry_days": -1
                    }
                    }
                }
                await self.settings.insert_one(default_config)
                return default_config
            return doc
        except Exception as e:
            logger.error(f"Error fetching public config: {e}")
            return {}

    async def update_public_config(self, key, value):
        if self.settings is None:
            return
        try:
            await self.settings.update_one(
                {"_id": "public_mode_config"}, {"$set": {key: value}}, upsert=True
            )
        except Exception as e:
            logger.error(f"Error updating public config: {e}")

    async def get_global_daily_egress_limit(self) -> float:
        if self.settings is None:
            return 0.0

        if Config.PUBLIC_MODE:
            config = await self.get_public_config()
            return float(config.get("global_daily_egress_mb", 0))
        else:
            doc = await self.settings.find_one({"_id": "global_settings"})
            if doc:
                return float(doc.get("global_daily_egress_mb", 0))
            return 0.0

    async def update_global_daily_egress_limit(self, limit_mb: float):
        if self.settings is None:
            return
        try:
            if Config.PUBLIC_MODE:
                await self.update_public_config("global_daily_egress_mb", limit_mb)
            else:
                await self.settings.update_one(
                    {"_id": "global_settings"},
                    {"$set": {"global_daily_egress_mb": limit_mb}},
                    upsert=True,
                )
        except Exception as e:
            logger.error(f"Error updating global daily egress limit: {e}")

    async def get_feature_toggles(self):
        if self.settings is None:
            return {}
        try:
            if Config.PUBLIC_MODE:
                config = await self.get_public_config()
                return config.get("feature_toggles", {})
            else:
                doc = await self.settings.find_one({"_id": "global_settings"})
                if doc:
                    return doc.get("feature_toggles", {})
                return {}
        except Exception as e:
            logger.error(f"Error fetching feature toggles: {e}")
            return {}

    async def update_feature_toggle(self, feature_name: str, enabled: bool):
        if self.settings is None:
            return
        try:
            if Config.PUBLIC_MODE:
                await self.settings.update_one(
                    {"_id": "public_mode_config"},
                    {"$set": {f"feature_toggles.{feature_name}": enabled}},
                    upsert=True
                )
            else:
                await self.settings.update_one(
                    {"_id": "global_settings"},
                    {"$set": {f"feature_toggles.{feature_name}": enabled}},
                    upsert=True
                )
        except Exception as e:
            logger.error(f"Error updating feature toggle: {e}")

    async def get_user_usage(self, user_id: int) -> dict:
        """Return today's usage counters merged with lifetime totals so
        callers that iterate ``usage.egress_mb`` + ``usage.egress_mb_alltime``
        keep working after the usage_collection_v1 migration moved the
        data out of user-doc subdocs.
        """
        if self.usage_tracker is None:
            return {}
        try:
            today = await self.usage_tracker.get_user_today(user_id)
            alltime = await self.usage_tracker.get_user_alltime(user_id)
            merged: dict = {}
            if today:
                merged.update(today)
            if alltime:
                # Expose lifetime counters under the legacy names that
                # pre-PR-E readers expect.
                merged["egress_mb_alltime"] = alltime.get(
                    "egress_mb_alltime", merged.get("egress_mb_alltime", 0.0)
                )
                merged["file_count_alltime"] = alltime.get(
                    "file_count_alltime", merged.get("file_count_alltime", 0)
                )
            return merged
        except Exception as e:
            logger.error(f"Error fetching usage for user {user_id}: {e}")
            return {}

    async def get_global_usage_today(self) -> float:
        """Total egress (confirmed + reserved) for the current UTC day
        across all users. Reads from the daily-global rollup; falls back
        to the legacy ``daily_stats`` collection when the rollup is cold
        (first boot after migration but before any upload)."""
        if self.usage_tracker is not None:
            try:
                doc = await self.usage_tracker.get_global_today()
                if doc:
                    return float(doc.get("total_egress_mb", 0.0)) + float(
                        doc.get("total_reserved_egress_mb", 0.0)
                    )
            except Exception as e:
                logger.warning(f"usage_tracker.get_global_today failed: {e}")

        if self.daily_stats is None:
            return 0.0
        current_utc_date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        try:
            doc = await self.daily_stats.find_one({"date": current_utc_date})
            if doc:
                return float(doc.get("egress_mb", 0.0)) + float(
                    doc.get("reserved_egress_mb", 0.0)
                )
            return 0.0
        except Exception as e:
            logger.error(f"Error fetching global usage: {e}")
            return 0.0

    async def check_daily_quota(self, user_id: int, file_size_bytes: int) -> tuple[bool, str, dict]:
        if self.settings is None:
            return True, "", {}

        current_utc_date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        incoming_mb = file_size_bytes / (1024 * 1024)

        global_limit_mb = await self.get_global_daily_egress_limit()
        if global_limit_mb > 0:
            current_global_usage = await self.get_global_usage_today()
            if current_global_usage + incoming_mb > global_limit_mb:
                mb_limit_str = f"{global_limit_mb} MB"
                if global_limit_mb >= 1024:
                    mb_limit_str = f"{global_limit_mb / 1024:.2f} GB"

                return False, f"Global Bot Usage Limit reached for today ({mb_limit_str}). Please try again tomorrow.", {}

        if user_id == Config.CEO_ID or user_id in Config.ADMIN_IDS:
            return True, "", {}

        if not Config.PUBLIC_MODE:
            return True, "", {}

        config = await self.get_public_config()
        daily_egress_mb_limit = config.get("daily_egress_mb", 0)
        daily_file_count_limit = config.get("daily_file_count", 0)

        now = time.time()

        user_doc = await self.get_user(user_id)
        is_premium = False
        premium_plan = "standard"
        if user_doc:
            exp = user_doc.get("premium_expiry")
            if user_doc.get("is_premium") and (exp is None or exp > now):
                is_premium = True
                premium_plan = user_doc.get("premium_plan", "standard")

        premium_system_enabled = config.get("premium_system_enabled", False)

        if is_premium and premium_system_enabled:
            if premium_plan == "deluxe" and config.get("premium_deluxe_enabled", False):
                plan_settings = config.get("premium_deluxe", {})
            else:
                plan_settings = config.get("premium_standard", {})
            daily_egress_mb_limit = plan_settings.get("daily_egress_mb", 0)
            daily_file_count_limit = plan_settings.get("daily_file_count", 0)

        if daily_egress_mb_limit <= 0 and daily_file_count_limit <= 0:
            return True, "", {}

        try:
            # Usage lives in MediaStudio-usage (PR E). A new day
            # automatically becomes a new doc because the primary key
            # includes the date — no manual "reset at midnight" needed
            # like the old in-place subdoc model required.
            usage = await self.usage_tracker.get_user_today(user_id) if self.usage_tracker else {}
            if not usage:
                usage = {
                    "date": current_utc_date,
                    "egress_mb": 0.0,
                    "reserved_egress_mb": 0.0,
                    "file_count": 0,
                    "quota_hits": 0,
                }

            current_utc = datetime.datetime.utcnow()
            tomorrow = current_utc + datetime.timedelta(days=1)
            midnight = datetime.datetime(tomorrow.year, tomorrow.month, tomorrow.day)
            time_to_midnight = midnight - current_utc
            hours, remainder = divmod(int(time_to_midnight.total_seconds()), 3600)
            minutes, _ = divmod(remainder, 60)
            reset_str = f"Resets at midnight UTC — roughly {hours}h {minutes}m from now."

            if daily_file_count_limit > 0 and usage.get("file_count", 0) >= daily_file_count_limit:
                await self.record_quota_hit(user_id)
                return False, f"You've reached your daily {daily_file_count_limit} file limit. {reset_str}", usage

            current_user_egress = usage.get("egress_mb", 0.0) + usage.get("reserved_egress_mb", 0.0)
            if daily_egress_mb_limit > 0 and (current_user_egress + incoming_mb) > daily_egress_mb_limit:
                await self.record_quota_hit(user_id)
                mb_limit_str = f"{daily_egress_mb_limit} MB"
                if daily_egress_mb_limit >= 1024:
                    mb_limit_str = f"{daily_egress_mb_limit / 1024:.2f} GB"
                return False, f"You've reached your daily {mb_limit_str} egress limit. {reset_str}", usage

            return True, "", usage

        except Exception as e:
            logger.error(f"Error checking daily quota for {user_id}: {e}")
            return True, "", {}

    async def reserve_quota(self, user_id: int, file_size_bytes: int):
        """Reserve MB on the user's day doc before upload starts.

        Also keeps the legacy ``MediaStudio-daily-stats`` collection in
        step so the admin dashboard's daily-history graph keeps working
        until it's migrated to the new ``MediaStudio-usage-daily-global``
        source in a follow-up PR.
        """
        current_utc_date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        incoming_mb = file_size_bytes / (1024 * 1024)

        try:
            if self.daily_stats is not None:
                await self.daily_stats.update_one(
                    {"date": current_utc_date},
                    {"$inc": {"reserved_egress_mb": incoming_mb}},
                    upsert=True,
                )
            if self.usage_tracker is not None:
                await self.usage_tracker.reserve_egress(user_id, incoming_mb)
        except Exception as e:
            logger.error(f"Error reserving quota: {e}")

    async def release_quota(self, user_id: int, file_size_bytes: int):
        """Undo a reservation (cancel / failure path)."""
        current_utc_date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        incoming_mb = file_size_bytes / (1024 * 1024)

        try:
            if self.daily_stats is not None:
                await self.daily_stats.update_one(
                    {"date": current_utc_date},
                    {"$inc": {"reserved_egress_mb": -incoming_mb}},
                    upsert=True,
                )
            if self.usage_tracker is not None:
                await self.usage_tracker.release_egress(user_id, incoming_mb)
        except Exception as e:
            logger.error(f"Error releasing quota: {e}")

    async def record_quota_hit(self, user_id: int):
        current_utc_date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        try:
            if self.usage_tracker is not None:
                await self.usage_tracker.record_quota_hit(user_id)
            if self.daily_stats is not None:
                await self.daily_stats.update_one(
                    {"date": current_utc_date},
                    {"$inc": {"quota_hits": 1}},
                    upsert=True,
                )
        except Exception as e:
            logger.error(f"Error recording quota hit: {e}")

    async def update_usage(
        self,
        user_id: int,
        processed_file_size_bytes: int,
        reserved_file_size_bytes: int = 0,
        *,
        media_type: str | None = None,
        tool_name: str | None = None,
        pro_mode: bool = False,
        processing_time_seconds: float | None = None,
    ):
        """Record a completed upload.

        ``media_type`` and ``tool_name`` are optional breakdown keys the
        tracker stores under ``by_type`` / ``by_tool`` sub-dicts on the
        per-day + alltime + daily-global docs. ``pro_mode`` flags uploads
        that went through the 𝕏TV Pro userbot (>2 GB files).

        Old call sites that don't pass the new kwargs still work — the
        tracker just skips the breakdown increments.
        """
        current_utc_date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        processed_mb = processed_file_size_bytes / (1024 * 1024)
        reserved_mb = reserved_file_size_bytes / (1024 * 1024)

        try:
            if self.usage_tracker is not None:
                await self.usage_tracker.record_upload(
                    user_id,
                    processed_mb,
                    media_type=media_type,
                    tool_name=tool_name,
                    pro_mode=pro_mode,
                )
                if reserved_mb:
                    await self.usage_tracker.release_egress(user_id, reserved_mb)
                if processing_time_seconds:
                    await self.usage_tracker.record_task_duration(
                        user_id, processing_time_seconds
                    )

            if self.daily_stats is not None:
                await self.daily_stats.update_one(
                    {"date": current_utc_date},
                    {
                        "$inc": {
                            "egress_mb": processed_mb,
                            "reserved_egress_mb": -reserved_mb,
                            "file_count": 1,
                        }
                    },
                    upsert=True,
                )
        except Exception as e:
            logger.error(f"Error updating usage: {e}")

    async def get_daily_stats(self, limit=7):
        if self.settings is None:
            return []
        try:
            cursor = self.daily_stats.find({}).sort("date", -1).limit(limit)
            return await cursor.to_list(length=limit)
        except Exception as e:
            logger.error(f"Error fetching daily stats: {e}")
            return []

    async def get_top_users_today(self, limit=10, skip=0, date: str | None = None):
        """Leaderboard of top users by egress for ``date`` (or today).

        Reads from the MediaStudio-usage collection introduced in PR E —
        the old code path searched for legacy ``user_<id>`` docs in
        MediaStudio-Settings which never contained new usage data
        post-mediastudio_layout.

        Returns (rows, total). Each row is a usage doc with at least
        ``uid``, ``date``, ``egress_mb``, ``file_count``.
        """
        if self.usage_tracker is None:
            return [], 0
        target_date = date or datetime.datetime.utcnow().strftime("%Y-%m-%d")
        try:
            return await self.usage_tracker.get_leaderboard_day(
                target_date, limit=limit, skip=skip
            )
        except Exception as e:
            logger.error(f"Error fetching top users: {e}")
            return [], 0

    async def get_total_users(self):
        if self.settings is None:
            return 0
        try:
            return await self.settings.count_documents({"_id": {"$regex": "^user_"}})
        except Exception:
            return 0

    async def get_dashboard_stats(self):
        if self.settings is None:
            return {}

        current_utc_date = datetime.datetime.utcnow().strftime("%Y-%m-%d")

        try:

            today_stats = await self.daily_stats.find_one({"date": current_utc_date}) or {}

            all_time_pipeline = [
                {"$group": {
                    "_id": None,
                    "total_egress": {"$sum": "$egress_mb"},
                    "total_files": {"$sum": "$file_count"}
                }}
            ]
            all_time_result = await self.daily_stats.aggregate(all_time_pipeline).to_list(1)
            all_time = all_time_result[0] if all_time_result else {"total_egress": 0, "total_files": 0}

            total_users = await self.get_total_users()

            public_config = await self.get_public_config()
            blocked_users = len(public_config.get("blocked_users", []))

            first_stat = await self.daily_stats.find_one({}, sort=[("date", 1)])
            bot_start_date = first_stat["date"] if first_stat else current_utc_date

            return {
                "total_users": total_users,
                "files_today": today_stats.get("file_count", 0),
                "egress_today_mb": today_stats.get("egress_mb", 0.0),
                "quota_hits_today": today_stats.get("quota_hits", 0),
                "total_files": all_time.get("total_files", 0),
                "total_egress_mb": all_time.get("total_egress", 0.0),
                "blocked_users": blocked_users,
                "bot_start_date": bot_start_date
            }
        except Exception as e:
            logger.error(f"Error getting dashboard stats: {e}")
            return {}

    async def block_user(self, user_id: int):
        if self.settings is None:
            return
        try:
            await self.settings.update_one(
                {"_id": "public_mode_config"},
                {"$addToSet": {"blocked_users": user_id}},
                upsert=True
            )
        except Exception as e:
            logger.error(f"Error blocking user: {e}")

    async def unblock_user(self, user_id: int):
        if self.settings is None:
            return
        try:
            await self.settings.update_one(
                {"_id": "public_mode_config"},
                {"$pull": {"blocked_users": user_id}}
            )
        except Exception as e:
            logger.error(f"Error unblocking user: {e}")

    async def is_user_blocked(self, user_id: int) -> bool:
        if self.settings is None:
            return False
        try:
            config = await self.get_public_config()
            return user_id in config.get("blocked_users", [])
        except Exception:
            return False

    async def reset_user_quota(self, user_id: int):
        if self.settings is None:
            return
        try:
            await self.settings.update_one(
                {"_id": f"user_{user_id}"},
                {"$set": {
                    "usage.egress_mb": 0.0,
                    "usage.file_count": 0,
                    "usage.quota_hits": 0
                }}
            )
        except Exception as e:
            logger.error(f"Error resetting user quota: {e}")

    async def get_all_users(self):
        if self.settings is None:
            return []
        users = []
        try:
            async for doc in self.settings.find({"_id": {"$regex": "^user_"}}):
                user_id_str = str(doc["_id"]).replace("user_", "")
                if user_id_str.isdigit():
                    users.append(int(user_id_str))
        except Exception as e:
            logger.error(f"Error fetching all users: {e}")
        return users

    async def ensure_user(self, user_id: int, first_name: str, username: str = None, last_name: str = None, language_code: str = None, is_bot: bool = False):
        if self.users is None:
            return
        now = time.time()

        user_doc = await self.users.find_one({"user_id": user_id})

        if not user_doc:
            new_user = {
                "user_id": user_id,
                "first_name": first_name,
                "last_name": last_name,
                "username": username,
                "language_code": language_code,
                "is_bot": is_bot,
                "banned": False,
                "is_premium": False,
                "premium_plan": "standard",
                "premium_expiry": None,
                "trial_claimed": False,
                "joined_at": now,
                "updated_at": now,
                "last_active": now,
                "history": [],
                "referral_count": 0,
            }
            await self.users.insert_one(new_user)
        else:
            update_fields = {
                "first_name": first_name,
                "last_name": last_name,
                "username": username,
                "language_code": language_code,
                "is_bot": is_bot,
                "updated_at": now,
                "last_active": now
            }

            if "banned" not in user_doc:
                update_fields["banned"] = False
            if "is_premium" not in user_doc:
                update_fields["is_premium"] = False
            if "premium_plan" not in user_doc:
                update_fields["premium_plan"] = "standard"
            if "premium_expiry" not in user_doc:
                update_fields["premium_expiry"] = None
            if "trial_claimed" not in user_doc:
                update_fields["trial_claimed"] = False
            if "joined_at" not in user_doc:
                update_fields["joined_at"] = now
            if "history" not in user_doc:
                update_fields["history"] = []
            if "referral_count" not in user_doc:
                update_fields["referral_count"] = 0

            await self.users.update_one(
                {"user_id": user_id},
                {"$set": update_fields}
            )

    async def get_user(self, user_id: int):
        if self.users is None:
            return None
        return await self.users.find_one({"user_id": user_id})

    # Ghost docs get created by migrations / upserts from mirror-leech /
    # myfiles / force-sub helpers — they only set {user_id, …} and have no
    # first_name. A "real" user has always gone through ensure_user() which
    # writes first_name + joined_at. Filter those ghosts out of admin views.
    _REAL_USER_FILTER = {
        "first_name": {"$exists": True, "$nin": [None, ""]},
    }

    def _real_users_filter(self, filter_dict: dict) -> dict:
        if not filter_dict:
            return dict(self._REAL_USER_FILTER)
        return {"$and": [dict(self._REAL_USER_FILTER), dict(filter_dict)]}

    async def get_users_paginated(self, filter_dict: dict, skip: int, limit: int, sort_by: str = "joined_at"):
        if self.users is None:
            return []
        sort_order = -1 if sort_by in ["joined_at", "updated_at"] else 1
        cursor = (
            self.users.find(self._real_users_filter(filter_dict))
            .sort(sort_by, sort_order)
            .skip(skip)
            .limit(limit)
        )
        return await cursor.to_list(length=limit)

    async def count_users(self, filter_dict: dict):
        if self.users is None:
            return 0
        return await self.users.count_documents(self._real_users_filter(filter_dict))

    async def search_users(self, query: str, limit: int = 10):
        if self.users is None:
            return []
        if query.isdigit():
            filter_dict = {"user_id": int(query)}
        else:
            filter_dict = {
                "$or": [
                    {"username": {"$regex": query, "$options": "i"}},
                    {"first_name": {"$regex": query, "$options": "i"}},
                ]
            }
        cursor = self.users.find(self._real_users_filter(filter_dict)).limit(limit)
        return await cursor.to_list(length=limit)

    async def add_premium_user(self, user_id: int, days: float, plan: str = "standard") -> bool:
        if self.users is None:
            return False
        now = time.time()

        user_doc = await self.get_user(user_id)
        if not user_doc:
            return False

        current_exp = user_doc.get("premium_expiry", 0)
        current_plan = user_doc.get("premium_plan", "standard")
        if current_exp and current_exp > now and current_plan == plan:
            new_exp = current_exp + (days * 86400)
        else:
            new_exp = now + (days * 86400)

        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {
                "is_premium": True,
                "premium_plan": plan,
                "premium_expiry": new_exp
            }}
        )
        return True

    async def reset_user_premium(self, user_id: int):
        if self.users is None:
            return
        user_doc = await self.get_user(user_id)
        if user_doc and user_doc.get("is_premium"):
            await self.users.update_one(
                {"user_id": user_id},
                {"$set": {
                    "is_premium": False,
                    "premium_plan": "donator",
                    "premium_expiry": None
                }}
            )
        else:
            await self.users.update_one(
                {"user_id": user_id},
                {"$set": {
                    "is_premium": False,
                    "premium_plan": "standard",
                    "premium_expiry": None
                }}
            )

    async def delete_user_data(self, user_id: int):
        if self.users is None or self.settings is None:
            return
        await self.users.delete_one({"user_id": user_id})
        await self.settings.delete_one({"_id": f"user_{user_id}"})

    async def add_log(self, action: str, admin_id: int, description: str):

        logger.info(f"ADMIN_LOG [{action}] by {admin_id}: {description}")

    async def add_pending_payment(self, payment_id: str, user_id: int, plan: str, duration_months: int, amount_str: str, method: str):
        if self.pending_payments is None:
            return
        doc = {
            "_id": payment_id,
            "user_id": user_id,
            "plan": plan,
            "duration_months": duration_months,
            "amount": amount_str,
            "method": method,
            "status": "pending",
            "created_at": time.time()
        }
        await self.pending_payments.insert_one(doc)

    async def get_pending_payment(self, payment_id: str):
        if self.pending_payments is None:
            return None
        return await self.pending_payments.find_one({"_id": payment_id})

    async def update_pending_payment_status(self, payment_id: str, status: str):
        if self.pending_payments is None:
            return
        await self.pending_payments.update_one({"_id": payment_id}, {"$set": {"status": status}})

    async def get_all_pending_payments(self, limit: int = 20):
        if self.pending_payments is None:
            return []
        cursor = self.pending_payments.find({"status": "pending"}).sort("created_at", 1).limit(limit)
        return await cursor.to_list(length=limit)

    async def get_db_channel(self, plan: str):
        if self.settings is None:
            return None
        if Config.PUBLIC_MODE:
            config = await self.get_public_config()
            return config.get("database_channels", {}).get(plan)
        else:
            doc = await self.settings.find_one({"_id": "global_settings"})
            if doc:
                return doc.get("database_channels", {}).get(plan)
        return None

    # ------------------------------------------------------------------
    # MyFiles extras — audit / activity / quota / share helpers
    # ------------------------------------------------------------------

    async def audit_myfiles(
        self,
        actor_id: int,
        action: str,
        *,
        user_id: int | None = None,
        file_id=None,
        folder_id=None,
        before=None,
        after=None,
        meta: dict | None = None,
    ) -> None:
        """Append a MyFiles admin/audit event. Silent if DB offline."""
        if self.myfiles_audit is None:
            return
        doc = {
            "user_id": user_id if user_id is not None else actor_id,
            "actor_id": actor_id,
            "action": action,
            "created_at": datetime.datetime.utcnow(),
        }
        if file_id is not None:
            doc["file_id"] = file_id
        if folder_id is not None:
            doc["folder_id"] = folder_id
        if before is not None:
            doc["before"] = before
        if after is not None:
            doc["after"] = after
        if meta:
            doc["meta"] = meta
        try:
            await self.myfiles_audit.insert_one(doc)
        except Exception as exc:
            logger.debug("audit_myfiles insert failed: %s", exc)

    async def log_myfiles_activity(
        self,
        user_id: int,
        event: str,
        *,
        file_id=None,
        folder_id=None,
    ) -> None:
        """Append a user-facing activity entry (shown in the MyFiles feed)."""
        if self.myfiles_activity is None:
            return
        doc = {
            "user_id": user_id,
            "event": event,
            "created_at": datetime.datetime.utcnow(),
        }
        if file_id is not None:
            doc["file_id"] = file_id
        if folder_id is not None:
            doc["folder_id"] = folder_id
        try:
            await self.myfiles_activity.insert_one(doc)
        except Exception as exc:
            logger.debug("log_myfiles_activity insert failed: %s", exc)

    async def myfiles_get_quota(self, user_id: int) -> dict:
        """Return the quota doc for `user_id`, creating a default if missing."""
        if self.myfiles_quotas is None:
            return {
                "user_id": user_id,
                "storage_used_bytes": 0,
                "storage_quota_bytes": 0,
                "file_count": 0,
                "file_count_quota": 0,
            }
        doc = await self.myfiles_quotas.find_one({"user_id": user_id})
        if doc:
            return doc
        default = {
            "user_id": user_id,
            "storage_used_bytes": 0,
            "storage_quota_bytes": 0,   # 0 means "inherit plan default"
            "file_count": 0,
            "file_count_quota": 0,
            "last_recalculated_at": datetime.datetime.utcnow(),
        }
        with contextlib.suppress(Exception):
            await self.myfiles_quotas.insert_one(default)
        return default

    async def myfiles_incr_quota(
        self, user_id: int, *, bytes_delta: int = 0, file_delta: int = 0
    ) -> None:
        if self.myfiles_quotas is None:
            return
        try:
            await self.myfiles_quotas.update_one(
                {"user_id": user_id},
                {
                    "$inc": {
                        "storage_used_bytes": int(bytes_delta),
                        "file_count": int(file_delta),
                    },
                    "$setOnInsert": {
                        "storage_quota_bytes": 0,
                        "file_count_quota": 0,
                    },
                },
                upsert=True,
            )
        except Exception as exc:
            logger.debug("myfiles_incr_quota failed: %s", exc)

    async def myfiles_set_quota(
        self,
        user_id: int,
        *,
        storage_quota_bytes: int | None = None,
        file_count_quota: int | None = None,
    ) -> None:
        if self.myfiles_quotas is None:
            return
        update: dict = {}
        if storage_quota_bytes is not None:
            update["storage_quota_bytes"] = int(storage_quota_bytes)
        if file_count_quota is not None:
            update["file_count_quota"] = int(file_count_quota)
        if not update:
            return
        try:
            await self.myfiles_quotas.update_one(
                {"user_id": user_id}, {"$set": update}, upsert=True
            )
        except Exception as exc:
            logger.debug("myfiles_set_quota failed: %s", exc)

    async def myfiles_create_share(self, doc: dict) -> str | None:
        if self.myfiles_shares is None:
            return None
        doc.setdefault("created_at", datetime.datetime.utcnow())
        doc.setdefault("views", 0)
        try:
            res = await self.myfiles_shares.insert_one(doc)
            return str(res.inserted_id)
        except Exception as exc:
            logger.debug("myfiles_create_share failed: %s", exc)
            return None

    async def myfiles_resolve_share(self, token: str) -> dict | None:
        if self.myfiles_shares is None:
            return None
        return await self.myfiles_shares.find_one({"token": token})

    async def myfiles_revoke_share(self, token: str) -> bool:
        if self.myfiles_shares is None:
            return False
        try:
            res = await self.myfiles_shares.delete_one({"token": token})
            return bool(res.deleted_count)
        except Exception:
            return False

    async def update_db_channel(self, plan: str, channel_id: int):
        if self.settings is None:
            return
        if Config.PUBLIC_MODE:
            await self.settings.update_one(
                {"_id": "public_mode_config"},
                {"$set": {f"database_channels.{plan}": channel_id}},
                upsert=True
            )
        else:
            await self.settings.update_one(
                {"_id": "global_settings"},
                {"$set": {f"database_channels.{plan}": channel_id}},
                upsert=True
            )

db = Database()

# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# Telegram Channel @XTVbots
# Developed for the 𝕏TV Network @XTVglobal
# Backup Channel @XTVhome
# Contact on Telegram @davdxpx
# --------------------------------------------------------------------------
