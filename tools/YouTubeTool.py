# --- Imports ---
import asyncio
import contextlib
import os
import re
import time
from pathlib import Path

from pyrogram import Client, StopPropagation, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from plugins.user_setup import track_tool_usage
from utils.state import clear_session, get_data, get_state, set_state, update_data
from utils.telegram.log import get_logger

# yt-dlp is an optional runtime dependency. Import lazily so main.py can still
# load the module even if the package is missing on the host.
try:
    import yt_dlp  # type: ignore
    _YTDLP_AVAILABLE = True
    _YTDLP_IMPORT_ERROR = None
except Exception as _e:  # pragma: no cover - only triggers when dep is absent
    yt_dlp = None
    _YTDLP_AVAILABLE = False
    _YTDLP_IMPORT_ERROR = str(_e)

logger = get_logger("tools.YouTubeTool")

# --- Constants ---
YT_URL_REGEX = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com|youtu\.be|youtube-nocookie\.com)/\S+",
    re.IGNORECASE,
)

VIDEO_QUALITIES = [
    ("360", "360p"),
    ("480", "480p"),
    ("720", "720p"),
    ("1080", "1080p"),
    ("best", "Best available"),
]

AUDIO_BITRATES = [
    ("128", "128 kbps"),
    ("192", "192 kbps"),
    ("320", "320 kbps"),
]

SUBTITLE_LANGUAGES = [
    ("en", "🇬🇧 English"),
    ("es", "🇪🇸 Spanish"),
    ("fr", "🇫🇷 French"),
    ("de", "🇩🇪 German"),
    ("hi", "🇮🇳 Hindi"),
    ("pt", "🇵🇹 Portuguese"),
    ("it", "🇮🇹 Italian"),
    ("ja", "🇯🇵 Japanese"),
    ("ko", "🇰🇷 Korean"),
    ("zh", "🇨🇳 Chinese"),
    ("ru", "🇷🇺 Russian"),
    ("ar", "🇸🇦 Arabic"),
]

# Plan-based max filesize (in bytes). Telegram bot limit is 2 GB; XTV Pro userbot
# pushes this to 4 GB for Deluxe.
MAX_SIZE_STANDARD = 2 * 1024 * 1024 * 1024
MAX_SIZE_DELUXE = 4 * 1024 * 1024 * 1024

# Progress update throttling
_PROGRESS_INTERVAL = 3.0  # seconds between status message edits


# === yt-dlp hardening (anti-bot / cookies / client fallback) ==============
#
# YouTube increasingly blocks data-center IPs with "Sign in to confirm you're
# not a bot". Four mitigations are layered here:
#
#   1) Cookie support — admins can upload a Netscape cookies.txt via
#      /ytcookies. We look for the file at the paths below and pass it to
#      every yt-dlp call via `cookiefile`. Cookies are ALSO mirrored to
#      MongoDB so they survive container redeploys (no volume mount needed):
#      see `restore_youtube_cookies_from_db()`.
#
#   2) Player-client rotation — yt-dlp's --extractor-args youtube:player_client
#      lets us ask for specific players. Different clients have different
#      anti-bot thresholds. We try a fallback chain if one fails with a
#      bot-check error.
#
#   3) Format-unavailable last-ditch fallback — if every player_client returns
#      "requested format is not available", we make one final attempt with a
#      maximally-permissive `format=best` selector (no height/size constraint).
#      This rescues edge cases where YouTube only exposes single muxed streams.
#
#   4) Friendly UI errors — `BotCheckError` / `FormatUnavailableError` let the
#      UI layer show dedicated retry/help screens instead of the generic
#      "Could not fetch video info".

# All cookie paths are anchored to the project root so they don't drift
# when pyrogram's `PARENT_DIR = Path(sys.argv[0]).parent` (used inside
# `message.download`) resolves differently from the bot's CWD \u2014 a
# mismatch that used to cause "uploaded cookies stored silently at a
# path yt-dlp never looks in".
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_COOKIES_CANDIDATES = [
    os.getenv("YT_COOKIES_FILE"),
    str(_PROJECT_ROOT / "config" / "yt_cookies.txt"),
    str(_PROJECT_ROOT / "yt_cookies.txt"),
    str(_PROJECT_ROOT / "downloads" / "yt_cookies.txt"),
]

# Single canonical on-disk path used for writing (uploads + DB restore).
_COOKIES_TARGET_PATH = str(_PROJECT_ROOT / "config" / "yt_cookies.txt")

# Order matters: iOS and TV clients tend to be less rate-limited than the
# default web client right now. yt-dlp picks a reasonable set automatically
# if we pass nothing, but when the default fails we try these explicitly.
_PLAYER_CLIENT_FALLBACKS = [
    None,              # yt-dlp default (usually includes web + android)
    "ios",
    "android",
    "tv_embedded",
    "web_embedded",
    "mweb",
]

# Markers that reliably indicate YouTube's anti-bot guard. The previous
# list included the substring "cookies", which false-matched every
# cookie-loading error message from yt-dlp ("Could not load cookies
# file\u2026") and sent the retry loop rotating through every player_client
# with the same broken cookies \u2014 always failing, and finally surfacing
# "bot-check triggered" to the user no matter what the real problem was.
_BOT_CHECK_MARKERS = (
    "sign in to confirm",
    "confirm you're not a bot",
    "confirm you\u2019re not a bot",
    "age-restricted",
    "please sign in",
    "consent cookie",
    "login required",
    "use --cookies",
    "use --cookies-from-browser",
)

# Markers that indicate yt-dlp could not load / parse the cookies file
# itself. These are NOT bot-check situations \u2014 rotating player clients
# won't help, the file is the problem. We surface this to the user as a
# dedicated error so they re-export cookies.txt instead of blaming
# YouTube.
_COOKIE_FILE_ERROR_MARKERS = (
    "could not load cookies",
    "unable to load cookies",
    "failed to parse cookies",
    "cookies file is not",
    "netscape format",
    "no valid cookies",
)

# Markers indicating the current player_client returned no usable formats.
# Rotating to a different client almost always resolves these — different
# clients expose different stream sets (e.g. iOS exposes HLS that web doesn't).
_FORMAT_UNAVAILABLE_MARKERS = (
    "requested format is not available",
    "no video formats found",
    "no formats found",
    "format not available",
    "requested formats are incompatible",
    "unable to extract",  # e.g. "unable to extract player response"
)


class BotCheckError(RuntimeError):
    """Raised when yt-dlp is blocked by YouTube's bot-check flow."""

    def __init__(self, original: str = ""):
        super().__init__(original or "YouTube bot-check triggered")
        self.original = original


class FormatUnavailableError(RuntimeError):
    """Raised when every player_client + the lenient last-ditch retry all
    failed with a `format-not-available` style error from yt-dlp."""

    def __init__(self, original: str = ""):
        super().__init__(original or "YouTube format unavailable")
        self.original = original


class CookieFileError(RuntimeError):
    """Raised when yt-dlp can't load / parse the cookies.txt file. The UI
    layer surfaces a dedicated "your cookies file is broken, re-export"
    screen — rotating player clients wouldn't help here."""

    def __init__(self, original: str = ""):
        super().__init__(original or "Cookies file invalid")
        self.original = original


def _get_cookies_file() -> str | None:
    """Return the first existing cookies file candidate, or None."""
    for p in _COOKIES_CANDIDATES:
        if not p:
            continue
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
        except OSError:
            continue
    return None


async def restore_youtube_cookies_from_db() -> bool:
    """Write the DB-stored cookies back to disk if no cookies file is present.

    Called from main.py at startup so admins don't have to re-upload
    cookies.txt after every container redeploy. Returns True when a fresh
    file was written from the DB, False otherwise (no DB record, or a
    file is already present on disk).
    """
    if _get_cookies_file():
        # Disk file already exists — prefer it over DB to allow manual override.
        return False
    try:
        from db import db
        record = await db.get_youtube_cookies()
    except Exception as e:
        logger.warning(f"restore_youtube_cookies_from_db: DB lookup failed: {e}")
        return False
    if not record or not record.get("cookies"):
        return False
    try:
        os.makedirs(os.path.dirname(_COOKIES_TARGET_PATH) or ".", exist_ok=True)
        with open(_COOKIES_TARGET_PATH, "w", encoding="utf-8") as f:
            f.write(record["cookies"])
        ts = record.get("updated_at")
        ts_str = ts.isoformat() if ts else "unknown date"
        logger.info(
            f"Restored YouTube cookies from DB → {_COOKIES_TARGET_PATH} "
            f"(uploaded {ts_str})"
        )
        return True
    except Exception as e:
        logger.warning(f"restore_youtube_cookies_from_db: write failed: {e}")
        return False


async def persist_youtube_cookies_to_db(cookies_text: str, uploaded_by: int | None = None) -> bool:
    """Save the cookies text to MongoDB so it survives container redeploys."""
    try:
        from db import db
        return await db.save_youtube_cookies(cookies_text, uploaded_by=uploaded_by)
    except Exception as e:
        logger.warning(f"persist_youtube_cookies_to_db failed: {e}")
        return False


async def delete_youtube_cookies() -> tuple[bool, bool]:
    """Remove cookies from BOTH disk and DB. Returns (disk_removed, db_removed)."""
    disk_removed = False
    cookies_path = _get_cookies_file()
    if cookies_path:
        try:
            os.remove(cookies_path)
            disk_removed = True
        except OSError as e:
            logger.warning(f"delete_youtube_cookies: disk remove failed: {e}")
    db_removed = False
    try:
        from db import db
        db_removed = await db.delete_youtube_cookies()
    except Exception as e:
        logger.warning(f"delete_youtube_cookies: DB delete failed: {e}")
    return disk_removed, db_removed


def _is_bot_check_error(err_text: str) -> bool:
    """Best-effort detection of YouTube's anti-bot guard from an error string."""
    if not err_text:
        return False
    low = err_text.lower()
    return any(m in low for m in _BOT_CHECK_MARKERS)


def _is_format_unavailable_error(err_text: str) -> bool:
    """Best-effort detection of 'no usable formats' from the current client."""
    if not err_text:
        return False
    low = err_text.lower()
    return any(m in low for m in _FORMAT_UNAVAILABLE_MARKERS)


def _is_cookie_file_error(err_text: str) -> bool:
    """Detect yt-dlp errors caused by a broken / unreadable cookies file.
    Rotating player clients won't help — the file itself is the problem."""
    if not err_text:
        return False
    low = err_text.lower()
    return any(m in low for m in _COOKIE_FILE_ERROR_MARKERS)


def _is_retryable_ytdlp_error(err_text: str) -> bool:
    """Errors where rotating to a different player_client is likely to help."""
    return _is_bot_check_error(err_text) or _is_format_unavailable_error(err_text)


def _ytdlp_base_opts(
    extra: dict | None = None,
    player_client: str | None = None,
) -> dict:
    """Build a yt-dlp opts dict with cookies + extractor args layered in.

    Any keys in `extra` override the defaults. This is the single place where
    we opt into hardening so every call site benefits.
    """
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # Retry transient network hiccups a few times before giving up.
        "retries": 3,
        "fragment_retries": 3,
        # Pretend to be a modern desktop browser to match cookies.
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
    }

    cookies = _get_cookies_file()
    if cookies:
        opts["cookiefile"] = cookies

    if player_client:
        opts["extractor_args"] = {"youtube": {"player_client": [player_client]}}

    if extra:
        opts.update(extra)
    return opts


def _run_ytdlp_with_fallback(
    url: str,
    base_extra: dict | None = None,
    download: bool = False,
):
    """Call yt-dlp extract_info, rotating through player clients on recoverable failures.

    Returns the info dict on success. Raises `BotCheckError` when every
    configured client failed with a bot-check marker. Re-raises other errors
    as-is from the last attempt.

    Rotation is triggered by either:
      * YouTube's anti-bot guard (see `_is_bot_check_error`), or
      * The current client returning no usable formats / failing extraction
        (see `_is_format_unavailable_error`). Different clients expose
        different stream sets, so a different client often succeeds where
        the default one doesn't.
    """
    last_exc: Exception | None = None
    for client_name in _PLAYER_CLIENT_FALLBACKS:
        opts = _ytdlp_base_opts(extra=base_extra, player_client=client_name)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=download)
        except Exception as e:  # yt_dlp.utils.DownloadError or similar
            last_exc = e
            msg = str(e)
            if _is_cookie_file_error(msg):
                # Broken cookies.txt — rotating player clients can't fix
                # this, fail fast with a dedicated error.
                logger.warning(f"yt-dlp cookie-file error: {msg[:200]}")
                raise CookieFileError(msg) from e
            if _is_bot_check_error(msg):
                logger.warning(
                    f"yt-dlp bot-check with player_client={client_name!r}: {msg[:200]}"
                )
                continue
            if _is_format_unavailable_error(msg):
                logger.warning(
                    f"yt-dlp format-unavailable with player_client={client_name!r}: {msg[:200]}"
                )
                continue
            # Non-recoverable failure — don't bother trying more clients.
            raise

    # All clients exhausted.
    err_text = str(last_exc) if last_exc else "unknown"
    if _is_bot_check_error(err_text):
        raise BotCheckError(err_text)
    if _is_format_unavailable_error(err_text):
        raise FormatUnavailableError(err_text)
    # Shouldn't really happen (we re-raise non-retryable errors above) but be safe.
    if last_exc:
        raise last_exc
    raise BotCheckError("yt-dlp exhausted all player clients")


def _run_ydl_session(build_extra_fn, action_fn):
    """Run a yt-dlp session, rotating player clients on retryable errors.

    `build_extra_fn` is a zero-argument callable that returns the `extra` dict
    to merge into the base opts (format selector, outtmpl, progress hooks, etc).
    `action_fn(ydl) -> result` performs the actual work on the YoutubeDL instance.

    Retries across `_PLAYER_CLIENT_FALLBACKS` when the error matches
    `_is_retryable_ytdlp_error`. Raises `BotCheckError` when every client hit
    the anti-bot guard; re-raises the last exception otherwise.
    """
    last_exc: Exception | None = None
    saw_format_unavailable = False
    for client_name in _PLAYER_CLIENT_FALLBACKS:
        opts = _ytdlp_base_opts(extra=build_extra_fn(), player_client=client_name)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return action_fn(ydl)
        except Exception as e:
            last_exc = e
            msg = str(e)
            if _is_cookie_file_error(msg):
                logger.warning(f"yt-dlp cookie-file error: {msg[:200]}")
                raise CookieFileError(msg) from e
            if _is_bot_check_error(msg):
                logger.warning(
                    f"yt-dlp bot-check with player_client={client_name!r}: {msg[:200]}"
                )
                continue
            if _is_format_unavailable_error(msg):
                saw_format_unavailable = True
                logger.warning(
                    f"yt-dlp format-unavailable with player_client={client_name!r}: {msg[:200]}"
                )
                continue
            raise

    # Last-ditch attempt for format-unavailable: drop the user's strict format
    # selector and fall back to plain `best`. Different YouTube videos expose
    # wildly different format trees (e.g. only a single muxed stream), so a
    # permissive selector often succeeds where the constrained one didn't.
    if saw_format_unavailable:
        for client_name in _PLAYER_CLIENT_FALLBACKS:
            extra = build_extra_fn()
            extra["format"] = "best"
            # Drop the merge_output_format too — single-stream "best" is already muxed.
            extra.pop("merge_output_format", None)
            opts = _ytdlp_base_opts(extra=extra, player_client=client_name)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    logger.info(
                        f"yt-dlp lenient retry (format=best) with player_client={client_name!r}"
                    )
                    return action_fn(ydl)
            except Exception as e:
                last_exc = e
                msg = str(e)
                if _is_retryable_ytdlp_error(msg):
                    continue
                raise

    err_text = str(last_exc) if last_exc else "unknown"
    if _is_bot_check_error(err_text):
        raise BotCheckError(err_text)
    if _is_format_unavailable_error(err_text):
        raise FormatUnavailableError(err_text)
    if last_exc:
        raise last_exc
    raise BotCheckError("yt-dlp exhausted all player clients")


# === Helper Functions ===
def extract_first_url(text: str) -> str | None:
    """Return the first YouTube URL found in a text, or None."""
    if not text:
        return None
    m = YT_URL_REGEX.search(text)
    if not m:
        return None
    url = m.group(0)
    # Normalize so yt-dlp always has a scheme
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def is_youtube_url(text: str) -> bool:
    return extract_first_url(text) is not None


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """Strip characters that cause issues on the filesystem / Telegram."""
    if not name:
        return "youtube"
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    name = name.strip(" .")
    if not name:
        name = "youtube"
    return name[:max_len]


async def get_user_max_filesize(user_id: int) -> int:
    """Return the max download size (bytes) allowed for this user's plan.

    Standard users / non-premium get 2 GB. Deluxe users with XTV Pro 4GB enabled
    get 4 GB. Falls back to 2 GB on any lookup error so we never exceed the
    Telegram bot hard limit unintentionally.
    """
    try:
        if not Config.PUBLIC_MODE:
            return MAX_SIZE_STANDARD
        from db import db
        user_doc = await db.get_user(user_id)
        if not user_doc or not user_doc.get("is_premium"):
            return MAX_SIZE_STANDARD
        plan_name = user_doc.get("premium_plan", "standard")
        config = await db.get_public_config()
        plan_settings = config.get(f"premium_{plan_name}", {})
        features = plan_settings.get("features", {})
        if features.get("xtv_pro_4gb"):
            return MAX_SIZE_DELUXE
        return MAX_SIZE_STANDARD
    except Exception as e:
        logger.debug(f"get_user_max_filesize fallback: {e}")
        return MAX_SIZE_STANDARD


def _ytdlp_missing_text() -> str:
    return (
        "❌ **YouTube support is not installed on this server.**\n\n"
        "> The `yt-dlp` package is missing.\n"
        "> Ask the administrator to run `pip install -r requirements.txt`."
    )


def _sync_extract_info(url: str) -> dict | None:
    """Blocking yt-dlp info extraction. Runs in a thread.

    Raises BotCheckError when YouTube's anti-bot guard blocks us so callers
    can surface a dedicated UI. Returns None on any other failure, logged.
    """
    if not _YTDLP_AVAILABLE:
        return None
    try:
        return _run_ytdlp_with_fallback(
            url,
            base_extra={"skip_download": True},
            download=False,
        )
    except (BotCheckError, FormatUnavailableError, CookieFileError):
        raise
    except Exception as e:
        logger.warning(f"yt-dlp extract_info failed for {url}: {e}")
        return None


async def extract_video_info(url: str) -> dict | None:
    """Fetch video metadata without downloading. Returns the yt-dlp info dict.

    Raises BotCheckError on YouTube anti-bot failures so the caller can show
    a dedicated help screen. Returns None for other failures.
    """
    if not _YTDLP_AVAILABLE:
        return None
    return await asyncio.to_thread(_sync_extract_info, url)


def _human_bytes(n: int | float) -> str:
    try:
        n = float(n or 0)
    except Exception:
        return "?"
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if n < 1024.0:
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} PiB"


class _ProgressState:
    """Holds throttling + latest-snapshot for yt-dlp progress hooks.

    yt-dlp's progress_hooks run in the download thread — we cannot `await` from
    there. Instead we stash the most recent snapshot and drain it from the event
    loop via a separate coroutine.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, status_msg, title_hint: str = ""):
        self.loop = loop
        self.status_msg = status_msg
        self.title_hint = title_hint
        self.latest: dict | None = None
        self.last_edit = 0.0
        self.closed = False

    def hook(self, d: dict) -> None:
        """yt-dlp progress hook entry (sync, runs off-loop)."""
        with contextlib.suppress(Exception):
            self.latest = d

    async def pump(self) -> None:
        """Periodically edits the status message with the latest progress."""
        while not self.closed:
            await asyncio.sleep(_PROGRESS_INTERVAL)
            d = self.latest
            if not d:
                continue
            if (time.time() - self.last_edit) < _PROGRESS_INTERVAL:
                continue
            text = self._format(d)
            if not text:
                continue
            try:
                await self.status_msg.edit_text(text)
                self.last_edit = time.time()
            except MessageNotModified:
                pass
            except Exception as e:
                logger.debug(f"Progress edit failed: {e}")

    def _format(self, d: dict) -> str | None:
        status = d.get("status")
        if status == "downloading":
            downloaded = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            pct = (downloaded / total * 100) if total else 0
            bar_len = 14
            filled = int(bar_len * pct / 100) if pct else 0
            bar = "█" * filled + "░" * (bar_len - filled)
            title_line = f"**{self.title_hint}**\n" if self.title_hint else ""
            return (
                f"⬇️ **Downloading from YouTube**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{title_line}"
                f"`{bar}` {pct:5.1f}%\n"
                f"• Size: `{_human_bytes(downloaded)} / {_human_bytes(total) if total else '?'}`\n"
                f"• Speed: `{_human_bytes(speed)}/s`\n"
                f"• ETA: `{int(eta)}s`"
            )
        if status == "finished":
            return (
                "✅ **Download complete — post-processing...**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "> Converting / muxing, please wait."
            )
        return None


def _sync_download_video(url: str, quality: str, output_dir: str, max_size: int,
                         hook) -> tuple[bool, str, dict | None]:
    """Blocking yt-dlp video download. Runs in a thread.

    Returns: (success, file_path_or_error_message, info_dict)
    """
    if not _YTDLP_AVAILABLE:
        return False, _YTDLP_IMPORT_ERROR or "yt-dlp not installed", None

    if quality == "best":
        fmt = f"bestvideo[filesize<?{max_size}]+bestaudio/best[filesize<?{max_size}]/best"
    else:
        try:
            h = int(quality)
        except ValueError:
            h = 720
        fmt = (
            f"bestvideo[height<={h}][filesize<?{max_size}]+bestaudio/"
            f"best[height<={h}][filesize<?{max_size}]/best[height<={h}]/best"
        )

    out_tmpl = os.path.join(output_dir, "%(title).80s [%(id)s].%(ext)s")

    def _build_extra():
        return {
            "format": fmt,
            "outtmpl": out_tmpl,
            "merge_output_format": "mp4",
            "restrictfilenames": False,
            "max_filesize": max_size,
            "progress_hooks": [hook] if hook else [],
            "writethumbnail": True,
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
                {"key": "EmbedThumbnail"},
                {"key": "FFmpegMetadata"},
            ],
        }

    def _action(ydl):
        info = ydl.extract_info(url, download=True)
        if not info:
            return False, "No info returned from yt-dlp.", None
        filepath = ydl.prepare_filename(info)
        # yt-dlp may have converted the container — swap extension to .mp4
        base, _ = os.path.splitext(filepath)
        mp4_path = base + ".mp4"
        if os.path.exists(mp4_path):
            filepath = mp4_path
        if not os.path.exists(filepath):
            return False, f"Output file not found: {filepath}", info
        return True, filepath, info

    try:
        return _run_ydl_session(_build_extra, _action)
    except (BotCheckError, FormatUnavailableError, CookieFileError):
        raise
    except yt_dlp.utils.DownloadError as e:
        return False, f"Download failed: {e}", None
    except Exception as e:
        logger.exception("yt-dlp video download crashed")
        return False, f"Unexpected error: {e}", None


async def download_video(url: str, quality: str, output_dir: str, max_size: int,
                         progress_state: _ProgressState | None = None
                         ) -> tuple[bool, str, dict | None]:
    hook = progress_state.hook if progress_state else None
    return await asyncio.to_thread(_sync_download_video, url, quality, output_dir, max_size, hook)


def _sync_download_audio(url: str, bitrate: str, output_dir: str, max_size: int,
                         hook) -> tuple[bool, str, dict | None]:
    """Blocking yt-dlp audio download → MP3 with requested bitrate."""
    if not _YTDLP_AVAILABLE:
        return False, _YTDLP_IMPORT_ERROR or "yt-dlp not installed", None

    out_tmpl = os.path.join(output_dir, "%(title).80s [%(id)s].%(ext)s")

    def _build_extra():
        return {
            "format": "bestaudio/best",
            "outtmpl": out_tmpl,
            "max_filesize": max_size,
            "progress_hooks": [hook] if hook else [],
            "writethumbnail": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(bitrate)},
                {"key": "EmbedThumbnail"},
                {"key": "FFmpegMetadata"},
            ],
        }

    def _action(ydl):
        info = ydl.extract_info(url, download=True)
        if not info:
            return False, "No info returned from yt-dlp.", None
        filepath = ydl.prepare_filename(info)
        base, _ = os.path.splitext(filepath)
        mp3_path = base + ".mp3"
        if os.path.exists(mp3_path):
            filepath = mp3_path
        if not os.path.exists(filepath):
            return False, f"Output file not found: {filepath}", info
        return True, filepath, info

    try:
        return _run_ydl_session(_build_extra, _action)
    except (BotCheckError, FormatUnavailableError, CookieFileError):
        raise
    except yt_dlp.utils.DownloadError as e:
        return False, f"Download failed: {e}", None
    except Exception as e:
        logger.exception("yt-dlp audio download crashed")
        return False, f"Unexpected error: {e}", None


async def download_audio(url: str, bitrate: str, output_dir: str, max_size: int,
                         progress_state: _ProgressState | None = None
                         ) -> tuple[bool, str, dict | None]:
    hook = progress_state.hook if progress_state else None
    return await asyncio.to_thread(_sync_download_audio, url, bitrate, output_dir, max_size, hook)


def _sync_download_thumbnail(url: str, output_dir: str) -> tuple[bool, str, dict | None]:
    """Fetch the best available thumbnail as a JPG file using yt-dlp + requests."""
    if not _YTDLP_AVAILABLE:
        return False, _YTDLP_IMPORT_ERROR or "yt-dlp not installed", None
    try:
        info = _sync_extract_info(url)
        if not info:
            return False, "Could not fetch video metadata.", None
    except (BotCheckError, FormatUnavailableError, CookieFileError):
        # Let typed errors bubble up so the UI can show a dedicated screen.
        raise
    try:

        # Prefer maxresdefault construction over arbitrary thumbnails list
        thumb_url = None
        video_id = info.get("id")
        if video_id:
            thumb_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"

        candidates = []
        if thumb_url:
            candidates.append(thumb_url)
        for t in (info.get("thumbnails") or []):
            u = t.get("url")
            if u and u not in candidates:
                candidates.append(u)
        if info.get("thumbnail") and info["thumbnail"] not in candidates:
            candidates.append(info["thumbnail"])

        import requests  # already in requirements.txt
        title = sanitize_filename(info.get("title") or video_id or "youtube")
        out_path = os.path.join(output_dir, f"{title}_thumbnail.jpg")

        last_err = None
        for cand in candidates:
            try:
                r = requests.get(cand, timeout=20, stream=True)
                if r.status_code == 200 and int(r.headers.get("content-length", 1)) > 1024:
                    with open(out_path, "wb") as f:
                        for chunk in r.iter_content(1024 * 64):
                            if chunk:
                                f.write(chunk)
                    if os.path.getsize(out_path) > 1024:
                        return True, out_path, info
                else:
                    last_err = f"HTTP {r.status_code}"
            except Exception as e:
                last_err = str(e)
                continue
        return False, f"No thumbnail could be fetched ({last_err}).", info
    except Exception as e:
        logger.exception("Thumbnail fetch crashed")
        return False, f"Unexpected error: {e}", None


async def download_thumbnail(url: str, output_dir: str) -> tuple[bool, str, dict | None]:
    return await asyncio.to_thread(_sync_download_thumbnail, url, output_dir)


def _sync_download_subtitles(url: str, lang: str, output_dir: str
                             ) -> tuple[bool, str, dict | None]:
    """Download subtitles/captions as an SRT file. Falls back to auto-captions."""
    if not _YTDLP_AVAILABLE:
        return False, _YTDLP_IMPORT_ERROR or "yt-dlp not installed", None

    out_tmpl = os.path.join(output_dir, "%(title).80s [%(id)s].%(ext)s")

    def _build_extra():
        return {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": [lang],
            "subtitlesformat": "srt/best",
            "convertsubtitles": "srt",
            "outtmpl": out_tmpl,
            "postprocessors": [
                {"key": "FFmpegSubtitlesConvertor", "format": "srt"},
            ],
        }

    def _action(ydl):
        info = ydl.extract_info(url, download=True)
        if not info:
            return False, "No info returned from yt-dlp.", None

        base = ydl.prepare_filename(info)
        base_noext, _ = os.path.splitext(base)

        # yt-dlp writes `<base>.<lang>.srt`. Try exact match first, then
        # any srt starting with our base (covers `en-US`, `en-orig`, …).
        candidate = f"{base_noext}.{lang}.srt"
        if os.path.exists(candidate):
            return True, candidate, info

        try:
            dir_ = os.path.dirname(base_noext) or "."
            prefix = os.path.basename(base_noext) + "."
            for fn in os.listdir(dir_):
                if fn.startswith(prefix) and fn.endswith(".srt"):
                    return True, os.path.join(dir_, fn), info
        except Exception:
            pass

        return False, f"No subtitles found for language '{lang}'.", info

    try:
        return _run_ydl_session(_build_extra, _action)
    except (BotCheckError, FormatUnavailableError, CookieFileError):
        raise
    except yt_dlp.utils.DownloadError as e:
        return False, f"Subtitle download failed: {e}", None
    except Exception as e:
        logger.exception("yt-dlp subtitle download crashed")
        return False, f"Unexpected error: {e}", None


async def download_subtitles(url: str, lang: str, output_dir: str
                             ) -> tuple[bool, str, dict | None]:
    return await asyncio.to_thread(_sync_download_subtitles, url, lang, output_dir)


def _format_duration(seconds) -> str:
    try:
        s = int(seconds or 0)
    except Exception:
        return "?"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{sec:02d}"
    return f"{m:d}:{sec:02d}"


def _format_number(n) -> str:
    try:
        n = int(n)
    except Exception:
        return "?"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _cancel_row() -> list[list[InlineKeyboardButton]]:
    return [[InlineKeyboardButton("❌ Cancel", callback_data="yt_cancel")]]


async def _render_bot_check_error(status_msg, user_id: int, bce: BotCheckError,
                                   back_state: str = "awaiting_youtube_url") -> None:
    """Replace the status message with a dedicated bot-check help screen.

    Shows different hints depending on whether cookies are already configured
    and whether the caller has admin rights (to expose /ytcookies).
    """
    has_cookies = _get_cookies_file() is not None
    admin_ids_set = set(getattr(Config, "ADMIN_IDS", []) or [])
    ceo_id = getattr(Config, "CEO_ID", None)
    is_admin = (user_id == ceo_id) or (user_id in admin_ids_set)

    lines = [
        "❌ **YouTube blocked this request**",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "YouTube asked to **sign in to confirm you're not a bot** — "
        "this usually happens on data-center IPs after heavy use.",
        "",
    ]
    if has_cookies:
        lines += [
            "🍪 Cookies **are** configured but were still rejected.",
            "The cookie file may be stale or tied to a flagged account.",
        ]
        if is_admin:
            lines.append("Upload a fresh `cookies.txt` via `/ytcookies`.")
        else:
            lines.append("Ask an admin to refresh cookies via `/ytcookies`.")
    else:
        lines += [
            "🍪 No cookies configured on this server.",
            (
                "An admin can upload a Netscape-format `cookies.txt` "
                "via `/ytcookies` to bypass this check."
            )
            if is_admin
            else "Ask an admin to upload a `cookies.txt` via `/ytcookies`.",
        ]
    lines += [
        "",
        "You can also try a different link or retry in a minute.",
    ]

    rows = [[InlineKeyboardButton("🔄 Retry", callback_data="yt_retry_url")]]
    if is_admin:
        rows.append([InlineKeyboardButton("🍪 Upload cookies", callback_data="yt_upload_cookies")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="yt_cancel")])

    if back_state:
        set_state(user_id, back_state)

    try:
        await status_msg.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(rows),
            disable_web_page_preview=True,
        )
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"_render_bot_check_error edit failed: {e}")

    logger.warning(f"yt bot-check surfaced to user {user_id}: {str(bce)[:200]}")


async def _render_format_unavailable_error(status_msg, user_id: int,
                                            fue: FormatUnavailableError,
                                            back_state: str = "awaiting_youtube_url") -> None:
    """Dedicated UI for 'Requested format is not available' errors.

    These usually mean YouTube only exposed a single muxed stream for this
    video (often the case for live streams, premieres, or rights-restricted
    content). The internal pipeline already falls back to `format=best`
    automatically — by the time we render this, every option was exhausted.
    """
    lines = [
        "❌ **No usable format found for this video**",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "YouTube didn't expose a downloadable stream that matches the "
        "requested quality, even after trying every player client and "
        "the lenient `best` fallback.",
        "",
        "**Common causes**",
        "• It's a **live stream / premiere** that hasn't ended yet.",
        "• The video is **DRM-protected** or members-only.",
        "• It's **age- or region-restricted** and your cookies don't grant access.",
        "• YouTube is currently rate-limiting this server.",
        "",
        "**What to try**",
        "• Pick a **different quality** (e.g. switch from 1080p to Best).",
        "• Try the **🎵 Audio (MP3)** mode if you only need the sound.",
        "• Refresh `cookies.txt` via `/ytcookies` — a fresh session sometimes "
        "unlocks more formats.",
        "• Wait a minute and retry — the rate-limit window is short.",
    ]
    rows = [
        [InlineKeyboardButton("🔄 Try Another URL", callback_data="yt_retry_url")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="yt_back_mode"),
         InlineKeyboardButton("❌ Cancel", callback_data="yt_cancel")],
    ]
    if back_state:
        set_state(user_id, back_state)
    try:
        await status_msg.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(rows),
            disable_web_page_preview=True,
        )
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"_render_format_unavailable_error edit failed: {e}")
    logger.warning(f"yt format-unavailable surfaced to user {user_id}: {str(fue)[:200]}")


async def _render_cookie_file_error(status_msg, user_id: int, cfe: CookieFileError,
                                    back_state: str | None = None):
    """Shown when yt-dlp could not parse the cookies.txt file itself.

    This is a distinct failure from the bot-check screen: the file is
    broken, not the request. We tell the user to re-export in Netscape
    format (the only shape yt-dlp supports).
    """
    admin_ids_set = set(getattr(Config, "ADMIN_IDS", []) or [])
    ceo_id = getattr(Config, "CEO_ID", None)
    is_admin = (user_id == ceo_id) or (user_id in admin_ids_set)

    lines = [
        "❌ **Cookies file is invalid**",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "yt-dlp could not read the uploaded `cookies.txt`. That almost "
        "always means the file is not in **Netscape** format (the only "
        "format yt-dlp accepts).",
        "",
        "**Fix it**",
        "• In your browser, install __Get cookies.txt LOCALLY__ (or a "
        "similar extension that exports **Netscape** format).",
        "• Log into `youtube.com` with a normal account.",
        "• Export cookies → save as `cookies.txt`.",
    ]
    if is_admin:
        lines.append("• Re-upload via `/ytcookies`.")
    else:
        lines.append("• Ask an admin to re-upload via `/ytcookies`.")

    rows = [
        [InlineKeyboardButton("🔄 Try Another URL", callback_data="yt_retry_url")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="yt_back_mode"),
         InlineKeyboardButton("❌ Cancel", callback_data="yt_cancel")],
    ]
    if back_state:
        set_state(user_id, back_state)
    try:
        await status_msg.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(rows),
            disable_web_page_preview=True,
        )
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"_render_cookie_file_error edit failed: {e}")
    logger.warning(f"yt cookie-file error surfaced to user {user_id}: {str(cfe)[:200]}")


def _mode_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Video", callback_data="yt_mode_video"),
         InlineKeyboardButton("🎵 Audio (MP3)", callback_data="yt_mode_audio")],
        [InlineKeyboardButton("🖼 Thumbnail", callback_data="yt_mode_thumb"),
         InlineKeyboardButton("📝 Subtitles", callback_data="yt_mode_subs")],
        [InlineKeyboardButton("ℹ️ Video Info", callback_data="yt_mode_info")],
        [InlineKeyboardButton("❌ Cancel", callback_data="yt_cancel")],
    ])


def _result_menu_markup() -> InlineKeyboardMarkup:
    """Markup shown below a delivered YouTube result (thumbnail/audio/video/
    subs/info). Lets the user go back to the per-URL mode menu, start over
    with a new link, or finish."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="yt_back_mode"),
         InlineKeyboardButton("🔗 New YouTube Link", callback_data="yt_new_link")],
        [InlineKeyboardButton("❌ Close", callback_data="yt_cancel")],
    ])


def _mode_menu_text(title: str, uploader: str, duration: str) -> str:
    return (
        f"▶️ **YouTube Tool**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"**🎬 {title}**\n"
        f"__by {uploader} · ⏱ {duration}__\n\n"
        f"Choose what you want to do:"
    )


def _quality_menu_markup() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for code, label in VIDEO_QUALITIES:
        row.append(InlineKeyboardButton(label, callback_data=f"yt_quality_{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="yt_cancel")])
    return InlineKeyboardMarkup(rows)


def _bitrate_menu_markup() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=f"yt_audio_bitrate_{code}")]
            for code, label in AUDIO_BITRATES]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="yt_cancel")])
    return InlineKeyboardMarkup(rows)


def _sub_lang_menu_markup() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for code, label in SUBTITLE_LANGUAGES:
        row.append(InlineKeyboardButton(label, callback_data=f"yt_sub_lang_{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="yt_cancel")])
    return InlineKeyboardMarkup(rows)


# === Menu Handlers ===
@Client.on_callback_query(filters.regex(r"^youtube_tool_menu$"))
async def handle_youtube_tool_menu(client, callback_query):
    await track_tool_usage(callback_query.from_user.id, "youtube_tool")
    await callback_query.answer()
    user_id = callback_query.from_user.id
    clear_session(user_id)

    if not _YTDLP_AVAILABLE:
        with contextlib.suppress(MessageNotModified):
            await callback_query.message.edit_text(
                _ytdlp_missing_text(),
                reply_markup=InlineKeyboardMarkup(_cancel_row()),
            )
        return

    set_state(user_id, "awaiting_youtube_url")
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            "▶️ **YouTube Tool**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "> Send me a **YouTube URL** to begin.\n\n"
            "**What I can do:**\n"
            "🎬 Download video (up to 1080p / best)\n"
            "🎵 Extract audio as MP3 (128 / 192 / 320 kbps)\n"
            "🖼 Grab the HD thumbnail\n"
            "📝 Download subtitles (multiple languages)\n"
            "ℹ️ Show video info & metadata\n\n"
            "__Tip: You can also paste a YouTube URL any time to get this menu automatically.__",
            reply_markup=InlineKeyboardMarkup(_cancel_row()),
            disable_web_page_preview=True,
        )


def format_video_info(info: dict) -> str:
    """Render a yt-dlp info dict as a readable Telegram message."""
    if not info:
        return "❌ No metadata available."

    title = info.get("title") or "Untitled"
    uploader = info.get("uploader") or info.get("channel") or "Unknown"
    duration = _format_duration(info.get("duration"))
    views = _format_number(info.get("view_count"))
    likes = _format_number(info.get("like_count"))
    upload_date = info.get("upload_date") or ""
    if upload_date and len(upload_date) == 8:
        upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

    description = (info.get("description") or "").strip()
    if len(description) > 600:
        description = description[:600] + "…"

    tags = info.get("tags") or []
    tags_str = ", ".join(f"#{t.replace(' ', '_')}" for t in tags[:10]) if tags else "—"

    chapters = info.get("chapters") or []
    chapters_str = ""
    if chapters:
        lines = []
        for c in chapters[:10]:
            start = _format_duration(c.get("start_time"))
            name = (c.get("title") or "").strip()
            if name:
                lines.append(f"  • `{start}` — {name}")
        if len(chapters) > 10:
            lines.append(f"  • … and {len(chapters) - 10} more")
        if lines:
            chapters_str = "\n\n**📚 Chapters:**\n" + "\n".join(lines)

    return (
        f"ℹ️ **Video Info**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"**Title:** {title}\n"
        f"**Uploader:** {uploader}\n"
        f"**Duration:** `{duration}`\n"
        f"**Views:** `{views}`  ·  **Likes:** `{likes}`\n"
        f"**Uploaded:** `{upload_date or '?'}`\n\n"
        f"**Tags:** {tags_str}"
        f"{chapters_str}\n\n"
        f"**Description:**\n{description or '—'}"
    )


# === URL Input Handler ===
@Client.on_message(filters.text & filters.private & ~filters.command([
    "start", "help", "info", "end", "settings", "myfiles",
    "r", "rename", "a", "audio", "c", "convert", "w", "watermark",
    "s", "subtitle", "t", "trim", "mi", "mediainfo", "v", "voice",
    "vn", "videonote", "g", "general", "p", "personal",
    "yt", "youtube",
]), group=1)
async def handle_youtube_url_input(client, message):
    """Handles URL input while user is in the YouTube-tool awaiting-URL state."""
    user_id = message.from_user.id
    state = get_state(user_id)
    if state != "awaiting_youtube_url":
        return  # not our flow

    url = extract_first_url(message.text or "")
    if not url:
        await message.reply_text(
            "⚠️ That doesn't look like a YouTube URL.\n"
            "Please send a link from `youtube.com`, `youtu.be` or `youtube-nocookie.com`.",
            reply_markup=InlineKeyboardMarkup(_cancel_row()),
        )
        raise StopPropagation

    update_data(user_id, "youtube_url", url)
    status_msg = await message.reply_text(
        "🔎 **Fetching video info...**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "> Please wait a moment.",
        reply_markup=InlineKeyboardMarkup(_cancel_row()),
    )

    try:
        info = await extract_video_info(url)
    except BotCheckError as bce:
        await _render_bot_check_error(status_msg, user_id, bce, back_state="awaiting_youtube_url")
        return
    except FormatUnavailableError as fue:
        await _render_format_unavailable_error(status_msg, user_id, fue, back_state="awaiting_youtube_url")
        return
    except CookieFileError as cfe:
        await _render_cookie_file_error(status_msg, user_id, cfe, back_state="awaiting_youtube_url")
        return

    if not info:
        set_state(user_id, "awaiting_youtube_url")
        with contextlib.suppress(MessageNotModified):
            await status_msg.edit_text(
                "❌ Could not fetch video info.\n\n"
                "The link might be invalid, private, age-restricted, or "
                "region-blocked. Send another URL or cancel.",
                reply_markup=InlineKeyboardMarkup(_cancel_row()),
            )
        return

    update_data(user_id, "video_title", info.get("title") or "Untitled")
    update_data(user_id, "video_id", info.get("id"))
    update_data(user_id, "video_duration", info.get("duration") or 0)
    update_data(user_id, "video_uploader", info.get("uploader") or info.get("channel") or "Unknown")
    set_state(user_id, "awaiting_yt_mode")

    title = info.get("title") or "Untitled"
    uploader = info.get("uploader") or info.get("channel") or "Unknown"
    duration = _format_duration(info.get("duration"))

    with contextlib.suppress(MessageNotModified):
        await status_msg.edit_text(
            f"▶️ **YouTube Tool**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"**🎬 {title}**\n"
            f"__by {uploader} · ⏱ {duration}__\n\n"
            f"Choose what you want to do:",
            reply_markup=_mode_menu_markup(),
            disable_web_page_preview=True,
        )
    raise StopPropagation


# === Mode Selection Callbacks ===
@Client.on_callback_query(filters.regex(r"^yt_mode_(video|audio|thumb|subs|info)$"))
async def handle_yt_mode(client, callback_query):
    user_id = callback_query.from_user.id
    data = callback_query.data
    mode = data.replace("yt_mode_", "")

    session = get_data(user_id)
    url = session.get("youtube_url") if session else None
    if not url:
        return await callback_query.answer(
            "⚠️ Session expired. Please start again.", show_alert=True
        )
    await callback_query.answer()
    update_data(user_id, "youtube_mode", mode)

    if mode == "video":
        set_state(user_id, "awaiting_yt_quality")
        with contextlib.suppress(MessageNotModified):
            await callback_query.message.edit_text(
                "🎬 **Video Download**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Pick a quality. Higher quality → larger file.\n"
                "> Files over your plan's limit will fail to upload.",
                reply_markup=_quality_menu_markup(),
            )
        return

    if mode == "audio":
        set_state(user_id, "awaiting_yt_audio_bitrate")
        with contextlib.suppress(MessageNotModified):
            await callback_query.message.edit_text(
                "🎵 **Audio (MP3) Download**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Pick an MP3 bitrate:",
                reply_markup=_bitrate_menu_markup(),
            )
        return

    if mode == "subs":
        set_state(user_id, "awaiting_yt_sub_lang")
        with contextlib.suppress(MessageNotModified):
            await callback_query.message.edit_text(
                "📝 **Subtitle Download**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Pick a language. If no manual subtitles exist I'll try "
                "auto-generated ones.",
                reply_markup=_sub_lang_menu_markup(),
            )
        return

    if mode == "thumb":
        await _run_thumbnail_download(client, callback_query.message, user_id, url)
        return

    if mode == "info":
        await _run_info_display(client, callback_query.message, user_id, url)
        return


# === Processing Runners ===
def _safe_remove(path: str | None) -> None:
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.debug(f"Failed to remove temp file {path}: {e}")


@Client.on_callback_query(filters.regex(r"^yt_quality_(360|480|720|1080|best)$"))
async def handle_yt_quality(client, callback_query):
    user_id = callback_query.from_user.id
    quality = callback_query.data.replace("yt_quality_", "")
    session = get_data(user_id)
    url = session.get("youtube_url") if session else None
    if not url:
        return await callback_query.answer(
            "⚠️ Session expired. Please start again.", show_alert=True
        )
    await callback_query.answer()
    await _run_video_download(client, callback_query.message, user_id, url, quality)


async def _run_video_download(client, status_msg, user_id: int, url: str, quality: str):
    title_hint = (get_data(user_id) or {}).get("video_title") or ""
    with contextlib.suppress(MessageNotModified):
        await status_msg.edit_text(
            f"🎬 **Preparing video download...**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"**{title_hint}**\n"
            f"> Quality: `{quality}`\n\n"
            f"__Starting download...__",
        )

    loop = asyncio.get_event_loop()
    progress = _ProgressState(loop, status_msg, title_hint=title_hint)
    pump_task = asyncio.create_task(progress.pump())

    max_size = await get_user_max_filesize(user_id)
    output_dir = Config.DOWNLOAD_DIR

    filepath = None
    info = None
    try:
        ok, result, info = await download_video(url, quality, output_dir, max_size, progress)
        progress.closed = True
        pump_task.cancel()
        if not ok:
            with contextlib.suppress(MessageNotModified):
                await status_msg.edit_text(
                    f"❌ **Video download failed.**\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"`{result}`",
                    reply_markup=_result_menu_markup(),
                )
            return
        filepath = result

        await status_msg.edit_text(
            f"⬆️ **Uploading to Telegram...**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"**{title_hint}**"
        )

        caption = (
            f"🎬 **{info.get('title') or 'YouTube Video'}**\n"
            f"__{info.get('uploader') or ''}__\n"
            f"Quality: `{quality}`"
        )
        try:
            delivered_msg = await client.send_video(
                chat_id=user_id,
                video=filepath,
                caption=caption,
                duration=int(info.get("duration") or 0),
                supports_streaming=True,
            )
        except Exception as e:
            logger.warning(f"send_video failed, falling back to send_document: {e}")
            delivered_msg = await client.send_document(chat_id=user_id, document=filepath, caption=caption)

        # Archive into MyFiles so YouTube downloads show up alongside
        # renamed files. Best-effort — never blocks delivery.
        with contextlib.suppress(Exception):
            from utils.myfiles.store import save_message_to_myfiles
            await save_message_to_myfiles(
                client,
                user_id,
                delivered_msg,
                file_name=os.path.basename(filepath) if filepath else None,
                media_type="video",
                tool_name="youtube",
            )

        with contextlib.suppress(MessageNotModified):
            await status_msg.edit_text(
                "✅ **Video delivered above.**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Choose next action:",
                reply_markup=_result_menu_markup(),
            )
    except BotCheckError as bce:
        progress.closed = True
        if not pump_task.done():
            pump_task.cancel()
        await _render_bot_check_error(status_msg, user_id, bce, back_state="awaiting_yt_result")
    except FormatUnavailableError as fue:
        progress.closed = True
        if not pump_task.done():
            pump_task.cancel()
        await _render_format_unavailable_error(status_msg, user_id, fue, back_state="awaiting_yt_result")
    except CookieFileError as cfe:
        progress.closed = True
        if not pump_task.done():
            pump_task.cancel()
        await _render_cookie_file_error(status_msg, user_id, cfe, back_state="awaiting_yt_result")
    except Exception as e:
        logger.exception("Video download pipeline crashed")
        with contextlib.suppress(Exception):
            await status_msg.edit_text(
                f"❌ Unexpected error: `{e}`",
                reply_markup=_result_menu_markup(),
            )
    finally:
        progress.closed = True
        if not pump_task.done():
            pump_task.cancel()
        _safe_remove(filepath)
        set_state(user_id, "awaiting_yt_result")


@Client.on_callback_query(filters.regex(r"^yt_audio_bitrate_(128|192|320)$"))
async def handle_yt_audio_bitrate(client, callback_query):
    user_id = callback_query.from_user.id
    bitrate = callback_query.data.replace("yt_audio_bitrate_", "")
    session = get_data(user_id)
    url = session.get("youtube_url") if session else None
    if not url:
        return await callback_query.answer(
            "⚠️ Session expired. Please start again.", show_alert=True
        )
    await callback_query.answer()
    await _run_audio_download(client, callback_query.message, user_id, url, bitrate)


async def _run_audio_download(client, status_msg, user_id: int, url: str, bitrate: str):
    title_hint = (get_data(user_id) or {}).get("video_title") or ""
    with contextlib.suppress(MessageNotModified):
        await status_msg.edit_text(
            f"🎵 **Preparing audio download...**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"**{title_hint}**\n"
            f"> Bitrate: `{bitrate} kbps`\n\n"
            f"__Starting download...__"
        )

    loop = asyncio.get_event_loop()
    progress = _ProgressState(loop, status_msg, title_hint=title_hint)
    pump_task = asyncio.create_task(progress.pump())

    max_size = await get_user_max_filesize(user_id)
    output_dir = Config.DOWNLOAD_DIR

    filepath = None
    info = None
    try:
        ok, result, info = await download_audio(url, bitrate, output_dir, max_size, progress)
        progress.closed = True
        pump_task.cancel()
        if not ok:
            with contextlib.suppress(MessageNotModified):
                await status_msg.edit_text(
                    f"❌ **Audio download failed.**\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"`{result}`",
                    reply_markup=_result_menu_markup(),
                )
            return
        filepath = result

        await status_msg.edit_text(
            f"⬆️ **Uploading MP3 to Telegram...**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"**{title_hint}**"
        )

        caption = (
            f"🎵 **{info.get('title') or 'YouTube Audio'}**\n"
            f"__{info.get('uploader') or ''}__\n"
            f"Bitrate: `{bitrate} kbps`"
        )
        try:
            delivered_msg = await client.send_audio(
                chat_id=user_id,
                audio=filepath,
                caption=caption,
                duration=int(info.get("duration") or 0),
                performer=info.get("uploader") or None,
                title=info.get("title") or None,
            )
        except Exception as e:
            logger.warning(f"send_audio failed, falling back to send_document: {e}")
            delivered_msg = await client.send_document(chat_id=user_id, document=filepath, caption=caption)

        with contextlib.suppress(Exception):
            from utils.myfiles.store import save_message_to_myfiles
            await save_message_to_myfiles(
                client,
                user_id,
                delivered_msg,
                file_name=os.path.basename(filepath) if filepath else None,
                media_type="audio",
                tool_name="youtube",
            )

        with contextlib.suppress(MessageNotModified):
            await status_msg.edit_text(
                "✅ **Audio delivered above.**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Choose next action:",
                reply_markup=_result_menu_markup(),
            )
    except BotCheckError as bce:
        progress.closed = True
        if not pump_task.done():
            pump_task.cancel()
        await _render_bot_check_error(status_msg, user_id, bce, back_state="awaiting_yt_result")
    except FormatUnavailableError as fue:
        progress.closed = True
        if not pump_task.done():
            pump_task.cancel()
        await _render_format_unavailable_error(status_msg, user_id, fue, back_state="awaiting_yt_result")
    except CookieFileError as cfe:
        progress.closed = True
        if not pump_task.done():
            pump_task.cancel()
        await _render_cookie_file_error(status_msg, user_id, cfe, back_state="awaiting_yt_result")
    except Exception as e:
        logger.exception("Audio download pipeline crashed")
        with contextlib.suppress(Exception):
            await status_msg.edit_text(
                f"❌ Unexpected error: `{e}`",
                reply_markup=_result_menu_markup(),
            )
    finally:
        progress.closed = True
        if not pump_task.done():
            pump_task.cancel()
        _safe_remove(filepath)
        set_state(user_id, "awaiting_yt_result")


@Client.on_callback_query(filters.regex(r"^yt_sub_lang_([a-zA-Z\-]+)$"))
async def handle_yt_sub_lang(client, callback_query):
    user_id = callback_query.from_user.id
    lang = callback_query.data.replace("yt_sub_lang_", "")
    session = get_data(user_id)
    url = session.get("youtube_url") if session else None
    if not url:
        return await callback_query.answer(
            "⚠️ Session expired. Please start again.", show_alert=True
        )
    await callback_query.answer()
    await _run_subtitle_download(client, callback_query.message, user_id, url, lang)


async def _run_subtitle_download(client, status_msg, user_id: int, url: str, lang: str):
    title_hint = (get_data(user_id) or {}).get("video_title") or ""
    with contextlib.suppress(MessageNotModified):
        await status_msg.edit_text(
            f"📝 **Fetching subtitles...**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"**{title_hint}**\n"
            f"> Language: `{lang}`"
        )

    output_dir = Config.DOWNLOAD_DIR
    filepath = None
    try:
        ok, result, info = await download_subtitles(url, lang, output_dir)
        if not ok:
            with contextlib.suppress(MessageNotModified):
                await status_msg.edit_text(
                    f"❌ **Subtitle download failed.**\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"`{result}`",
                    reply_markup=_result_menu_markup(),
                )
            return
        filepath = result

        caption = (
            f"📝 **Subtitles — {info.get('title') or 'YouTube Video'}**\n"
            f"Language: `{lang}`"
        )
        try:
            await client.send_document(chat_id=user_id, document=filepath, caption=caption)
        except Exception as e:
            logger.exception("Subtitle send_document failed")
            with contextlib.suppress(Exception):
                await status_msg.edit_text(
                    f"❌ Upload failed: `{e}`",
                    reply_markup=_result_menu_markup(),
                )
            return

        with contextlib.suppress(MessageNotModified):
            await status_msg.edit_text(
                "✅ **Subtitles delivered above.**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Choose next action:",
                reply_markup=_result_menu_markup(),
            )
    except BotCheckError as bce:
        await _render_bot_check_error(status_msg, user_id, bce, back_state="awaiting_yt_result")
    except FormatUnavailableError as fue:
        await _render_format_unavailable_error(status_msg, user_id, fue, back_state="awaiting_yt_result")
    except CookieFileError as cfe:
        await _render_cookie_file_error(status_msg, user_id, cfe, back_state="awaiting_yt_result")
    except Exception as e:
        logger.exception("Subtitle pipeline crashed")
        with contextlib.suppress(Exception):
            await status_msg.edit_text(
                f"❌ Unexpected error: `{e}`",
                reply_markup=_result_menu_markup(),
            )
    finally:
        _safe_remove(filepath)
        set_state(user_id, "awaiting_yt_result")


async def _run_thumbnail_download(client, status_msg, user_id: int, url: str):
    title_hint = (get_data(user_id) or {}).get("video_title") or ""
    with contextlib.suppress(MessageNotModified):
        await status_msg.edit_text(
            f"🖼 **Fetching thumbnail...**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"**{title_hint}**"
        )

    output_dir = Config.DOWNLOAD_DIR
    filepath = None
    try:
        ok, result, info = await download_thumbnail(url, output_dir)
        if not ok:
            with contextlib.suppress(MessageNotModified):
                await status_msg.edit_text(
                    f"❌ **Thumbnail download failed.**\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"`{result}`",
                    reply_markup=_result_menu_markup(),
                )
            return
        filepath = result

        caption = (
            f"🖼 **{info.get('title') or 'YouTube Thumbnail'}**\n"
            f"__{info.get('uploader') or ''}__"
        )
        try:
            await client.send_photo(chat_id=user_id, photo=filepath, caption=caption)
        except Exception as e:
            logger.warning(f"send_photo failed, falling back to send_document: {e}")
            await client.send_document(chat_id=user_id, document=filepath, caption=caption)

        # Keep the thumbnail image in chat; replace the status message with
        # a small result menu (Back to Menu / New Link).
        with contextlib.suppress(MessageNotModified):
            await status_msg.edit_text(
                "✅ **Thumbnail delivered above.**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Choose next action:",
                reply_markup=_result_menu_markup(),
            )
    except BotCheckError as bce:
        await _render_bot_check_error(status_msg, user_id, bce, back_state="awaiting_yt_result")
    except FormatUnavailableError as fue:
        await _render_format_unavailable_error(status_msg, user_id, fue, back_state="awaiting_yt_result")
    except CookieFileError as cfe:
        await _render_cookie_file_error(status_msg, user_id, cfe, back_state="awaiting_yt_result")
    except Exception as e:
        logger.exception("Thumbnail pipeline crashed")
        with contextlib.suppress(Exception):
            await status_msg.edit_text(
                f"❌ Unexpected error: `{e}`",
                reply_markup=_result_menu_markup(),
            )
    finally:
        _safe_remove(filepath)
        # Session is kept alive so Back-to-Menu works. State is set to
        # awaiting_yt_result so stray text messages don't trigger other flows.
        set_state(user_id, "awaiting_yt_result")


async def _run_info_display(client, status_msg, user_id: int, url: str):
    with contextlib.suppress(MessageNotModified):
        await status_msg.edit_text(
            "ℹ️ **Fetching full metadata...**\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )

    try:
        info = await extract_video_info(url)
    except BotCheckError as bce:
        await _render_bot_check_error(status_msg, user_id, bce, back_state="awaiting_yt_result")
        return
    except FormatUnavailableError as fue:
        await _render_format_unavailable_error(status_msg, user_id, fue, back_state="awaiting_yt_result")
        return
    except CookieFileError as cfe:
        await _render_cookie_file_error(status_msg, user_id, cfe, back_state="awaiting_yt_result")
        return
    if not info:
        with contextlib.suppress(Exception):
            await status_msg.edit_text(
                "❌ Could not fetch video info. The link may be invalid, "
                "private or region-blocked.",
                reply_markup=_result_menu_markup(),
            )
        set_state(user_id, "awaiting_yt_result")
        return

    text = format_video_info(info)
    delivered = False
    try:
        await status_msg.edit_text(
            text,
            disable_web_page_preview=True,
            reply_markup=_result_menu_markup(),
        )
        delivered = True
    except MessageNotModified:
        delivered = True
    except Exception as e:
        # Telegram may reject ultra-long messages — fall back to a document upload
        logger.warning(f"Info edit failed, sending as document: {e}")
        try:
            tmp_path = os.path.join(Config.DOWNLOAD_DIR, f"yt_info_{info.get('id') or user_id}.txt")
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(text)
            await client.send_document(chat_id=user_id, document=tmp_path,
                                       caption="ℹ️ YouTube Video Info")
            _safe_remove(tmp_path)
            with contextlib.suppress(MessageNotModified):
                await status_msg.edit_text(
                    "✅ **Info delivered above.**\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "Choose next action:",
                    reply_markup=_result_menu_markup(),
                )
            delivered = True
        except Exception as e2:
            logger.exception("Info fallback failed")
            with contextlib.suppress(Exception):
                await status_msg.edit_text(
                    f"❌ Could not render info: `{e2}`",
                    reply_markup=_result_menu_markup(),
                )
    set_state(user_id, "awaiting_yt_result")
    _ = delivered  # placeholder for future telemetry


# === Post-result navigation: Back to Menu / New YouTube Link ===
@Client.on_callback_query(filters.regex(r"^yt_back_mode$"))
async def handle_yt_back_mode(client, callback_query):
    """Return the user to the per-URL mode menu, keeping the session alive."""
    user_id = callback_query.from_user.id
    session = get_data(user_id) or {}
    url = session.get("youtube_url")
    if not url:
        return await callback_query.answer(
            "⚠️ Session expired. Please send a new YouTube link.", show_alert=True
        )
    await callback_query.answer()
    set_state(user_id, "awaiting_yt_mode")

    title = session.get("video_title") or "Untitled"
    uploader = session.get("video_uploader") or "Unknown"
    duration = _format_duration(session.get("video_duration"))
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            _mode_menu_text(title, uploader, duration),
            reply_markup=_mode_menu_markup(),
            disable_web_page_preview=True,
        )


@Client.on_callback_query(filters.regex(r"^yt_new_link$"))
async def handle_yt_new_link(client, callback_query):
    """Clear the current video context and ask for a new URL."""
    user_id = callback_query.from_user.id
    await callback_query.answer()
    # Keep the user in the YouTube flow but drop video-specific data.
    for key in ("youtube_url", "video_title", "video_id",
                "video_duration", "video_uploader", "youtube_mode"):
        update_data(user_id, key, None)
    set_state(user_id, "awaiting_youtube_url")
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            "▶️ **YouTube Tool**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "> Paste the next **YouTube URL** to continue.",
            reply_markup=InlineKeyboardMarkup(_cancel_row()),
            disable_web_page_preview=True,
        )


# === Cancel ===
@Client.on_callback_query(filters.regex(r"^yt_cancel$"))
async def handle_yt_cancel(client, callback_query):
    user_id = callback_query.from_user.id
    clear_session(user_id)
    await callback_query.answer("Cancelled.")
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            "❌ **YouTube Tool — cancelled.**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Use /start or /yt to open it again."
        )


# === Auto URL Detection ===
# Runs after the dedicated awaiting_youtube_url handler (group=1). If the user
# pastes a YouTube URL without any active state, we offer to open the tool.
@Client.on_message(
    filters.private & filters.text
    & filters.regex(r"(youtube\.com|youtu\.be|youtube-nocookie\.com)"),
    group=2,
)
async def handle_yt_auto_detect(client, message):
    user_id = message.from_user.id
    if get_state(user_id):
        return  # leave existing flows alone

    if not _YTDLP_AVAILABLE:
        return  # don't advertise a broken feature

    # Gate on feature toggles / premium access, same as the /yt shortcut.
    try:
        from db import db
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
            return
    except Exception as e:
        logger.debug(f"Auto-detect feature check failed: {e}")
        return

    url = extract_first_url(message.text or "")
    if not url:
        return

    update_data(user_id, "youtube_url", url)
    status_msg = await message.reply_text(
        "🔎 **YouTube URL detected — fetching info...**\n"
        "━━━━━━━━━━━━━━━━━━━━",
        reply_markup=InlineKeyboardMarkup(_cancel_row()),
    )

    try:
        info = await extract_video_info(url)
    except BotCheckError as bce:
        await _render_bot_check_error(status_msg, user_id, bce, back_state="awaiting_youtube_url")
        return
    except FormatUnavailableError as fue:
        await _render_format_unavailable_error(status_msg, user_id, fue, back_state="awaiting_youtube_url")
        return
    except CookieFileError as cfe:
        await _render_cookie_file_error(status_msg, user_id, cfe, back_state="awaiting_youtube_url")
        return
    if not info:
        with contextlib.suppress(MessageNotModified):
            await status_msg.edit_text(
                "❌ Could not fetch video info. The link may be invalid, "
                "private or region-blocked.",
                reply_markup=InlineKeyboardMarkup(_cancel_row()),
            )
        return

    update_data(user_id, "video_title", info.get("title") or "Untitled")
    update_data(user_id, "video_id", info.get("id"))
    update_data(user_id, "video_duration", info.get("duration") or 0)
    update_data(user_id, "video_uploader", info.get("uploader") or info.get("channel") or "Unknown")
    set_state(user_id, "awaiting_yt_mode")

    title = info.get("title") or "Untitled"
    uploader = info.get("uploader") or info.get("channel") or "Unknown"
    duration = _format_duration(info.get("duration"))
    with contextlib.suppress(MessageNotModified):
        await status_msg.edit_text(
            f"▶️ **YouTube Tool**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"**🎬 {title}**\n"
            f"__by {uploader} · ⏱ {duration}__\n\n"
            f"Choose what you want to do:",
            reply_markup=_mode_menu_markup(),
            disable_web_page_preview=True,
        )


# === Retry + admin cookies management ====================================
# Note: _COOKIES_TARGET_PATH is defined at the top of this file together with
# _COOKIES_CANDIDATES so the DB-restore helpers can use it too.


def _is_yt_admin(user_id: int) -> bool:
    admin_ids_set = set(getattr(Config, "ADMIN_IDS", []) or [])
    ceo_id = getattr(Config, "CEO_ID", None)
    return (user_id == ceo_id) or (user_id in admin_ids_set)


@Client.on_callback_query(filters.regex(r"^yt_retry_url$"))
async def handle_yt_retry_url(client, callback_query):
    """Re-enter URL state and ask for the link again (preserving session)."""
    user_id = callback_query.from_user.id
    await callback_query.answer()
    set_state(user_id, "awaiting_youtube_url")
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            "🔗 **Send a YouTube URL**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Paste any `youtube.com` / `youtu.be` link.",
            reply_markup=InlineKeyboardMarkup(_cancel_row()),
        )


@Client.on_callback_query(filters.regex(r"^yt_upload_cookies$"))
async def handle_yt_upload_cookies_cb(client, callback_query):
    """Admin-only prompt to upload a cookies.txt document."""
    user_id = callback_query.from_user.id
    if not _is_yt_admin(user_id):
        return await callback_query.answer("Admins only.", show_alert=True)

    await callback_query.answer()
    set_state(user_id, "awaiting_yt_cookies_upload")
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            "🍪 **Upload YouTube cookies**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Send a Netscape-format `cookies.txt` as a **document**.\n\n"
            "Tip: use a browser extension like "
            "__Get cookies.txt LOCALLY__ and export cookies while "
            "logged into `youtube.com`.\n\n"
            "Send `cancel` to abort.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="yt_cancel"),
            ]]),
            disable_web_page_preview=True,
        )


@Client.on_message(filters.command(["ytcookies"]) & filters.private)
async def handle_ytcookies_cmd(client, message):
    """Entry point: admins run /ytcookies to upload a fresh cookies file."""
    user_id = message.from_user.id
    if not _is_yt_admin(user_id):
        return await message.reply_text("❌ Admins only.")

    set_state(user_id, "awaiting_yt_cookies_upload")

    # Collect current state from both disk and DB.
    has_disk = _get_cookies_file() is not None
    db_record = None
    try:
        from db import db
        db_record = await db.get_youtube_cookies()
    except Exception as e:
        logger.warning(f"handle_ytcookies_cmd: DB lookup failed: {e}")

    disk_line = "✅ on disk" if has_disk else "❌ missing on disk"
    if db_record:
        ts = db_record.get("updated_at")
        ts_str = ts.strftime("%Y-%m-%d %H:%M UTC") if ts else "unknown"
        uploader = db_record.get("uploaded_by") or "unknown"
        db_line = (
            f"✅ persisted in MongoDB\n"
            f"   · last updated: `{ts_str}`\n"
            f"   · uploader id:  `{uploader}`"
        )
    else:
        db_line = "❌ not persisted in MongoDB (will be lost on redeploy)"

    overall_ok = has_disk or bool(db_record)
    header = (
        "✅ **currently configured**" if overall_ok else "❌ **not configured**"
    )

    buttons = [[InlineKeyboardButton("❌ Cancel", callback_data="yt_cancel")]]
    if overall_ok:
        buttons.insert(0, [InlineKeyboardButton(
            "🗑 Remove cookies", callback_data="yt_cookies_remove"
        )])

    await message.reply_text(
        f"🍪 **YouTube Cookies — {header}**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"• Disk: {disk_line}\n"
        f"• DB:   {db_line}\n\n"
        "Send a Netscape-format `cookies.txt` as a **document** to "
        "install/refresh it. Cookies are mirrored to MongoDB so they "
        "survive container redeploys automatically.\n\n"
        "Export tip: in your browser, log into `youtube.com` and use an "
        "extension such as __Get cookies.txt LOCALLY__ → save as "
        "`cookies.txt` → send that file here.\n\n"
        "Send `cancel` to abort — or use /ytcookies_remove to wipe "
        "everything.",
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
    )


@Client.on_message(filters.document & filters.private, group=0)
async def handle_ytcookies_upload(client, message):
    """Accept the uploaded cookies document when the user is in our state."""
    user_id = message.from_user.id
    if get_state(user_id) != "awaiting_yt_cookies_upload":
        from pyrogram import ContinuePropagation
        raise ContinuePropagation
    if not _is_yt_admin(user_id):
        from pyrogram import ContinuePropagation
        raise ContinuePropagation

    doc = message.document
    # Best-effort file-type gate: cookies files are tiny text files.
    if doc and doc.file_size and doc.file_size > 2 * 1024 * 1024:
        await message.reply_text(
            "❌ Cookie file is suspiciously large (>2 MB). "
            "Please send the Netscape-format `cookies.txt` only."
        )
        raise StopPropagation

    # Ensure target dir exists.
    try:
        os.makedirs(os.path.dirname(_COOKIES_TARGET_PATH) or ".", exist_ok=True)
    except Exception as e:
        await message.reply_text(f"❌ Could not prepare cookies dir: {e}")
        raise StopPropagation from e

    # Pyrogram's `message.download(file_name=...)` resolves a relative
    # path against its own `PARENT_DIR` (derived from sys.argv[0]) —
    # which is not always the bot's CWD. We pass an absolute path and
    # also capture the returned location so we read back from exactly
    # where pyrogram actually wrote the file.
    saved_path: str | None = None
    try:
        saved_path = await message.download(file_name=_COOKIES_TARGET_PATH)
    except Exception as e:
        await message.reply_text(f"❌ Download failed: {e}")
        raise StopPropagation from e

    if not saved_path or not os.path.isfile(saved_path):
        await message.reply_text(
            "❌ Upload failed: Pyrogram reported no file path. "
            "Please try again or check the bot's storage."
        )
        raise StopPropagation

    # If pyrogram landed the file somewhere other than our canonical
    # target path, copy it over so every other code path (_get_cookies_file,
    # restore_youtube_cookies_from_db, delete_youtube_cookies) keeps
    # pointing at a single location.
    if os.path.abspath(saved_path) != os.path.abspath(_COOKIES_TARGET_PATH):
        try:
            import shutil
            shutil.copyfile(saved_path, _COOKIES_TARGET_PATH)
            with contextlib.suppress(OSError):
                os.remove(saved_path)
            saved_path = _COOKIES_TARGET_PATH
        except Exception as e:
            logger.warning(
                f"handle_ytcookies_upload: could not move {saved_path} "
                f"to {_COOKIES_TARGET_PATH}: {e}"
            )

    # Quick sanity check: file must contain at least one YouTube cookie line.
    ok = False
    full_text = ""
    try:
        with open(saved_path, "r", encoding="utf-8", errors="ignore") as f:
            full_text = f.read()
        if "youtube.com" in full_text.lower():
            ok = True
    except Exception:
        ok = False

    # Mirror into MongoDB so redeploys don't wipe the cookies.
    db_saved = False
    if full_text:
        try:
            db_saved = await persist_youtube_cookies_to_db(
                full_text, uploaded_by=user_id
            )
        except Exception as e:
            logger.warning(f"handle_ytcookies_upload: DB persist failed: {e}")

    set_state(user_id, None)

    db_line = (
        "✅ Mirrored to MongoDB — will survive redeploys."
        if db_saved else
        "⚠️ Could NOT persist to MongoDB — cookies may be lost on redeploy."
    )

    if not ok:
        await message.reply_text(
            "⚠️ **Cookies installed, but no `youtube.com` entry found**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "The file was saved, but it doesn't look like a YouTube "
            "cookies export. If YouTube still blocks you, export a fresh "
            "cookies.txt **while logged into youtube.com** and try again.\n\n"
            f"{db_line}"
        )
        raise StopPropagation

    await message.reply_text(
        "✅ **YouTube cookies installed**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"• Disk: `{_COOKIES_TARGET_PATH}`\n"
        f"• DB:   {db_line}\n\n"
        "New yt-dlp requests will use this cookie jar automatically.\n\n"
        "Try your YouTube link again now."
    )
    raise StopPropagation


@Client.on_message(filters.command(["ytcookies_remove"]) & filters.private)
async def handle_ytcookies_remove_cmd(client, message):
    """Admin-only: wipe cookies from both disk and DB."""
    user_id = message.from_user.id
    if not _is_yt_admin(user_id):
        return await message.reply_text("❌ Admins only.")

    disk_removed, db_removed = await delete_youtube_cookies()
    disk_line = "✅ removed" if disk_removed else "ℹ️ nothing to remove"
    db_line = "✅ removed" if db_removed else "ℹ️ nothing to remove"

    await message.reply_text(
        "🗑 **YouTube cookies removed**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"• Disk: {disk_line}\n"
        f"• DB:   {db_line}\n\n"
        "Use /ytcookies to upload a fresh cookies.txt."
    )


@Client.on_callback_query(filters.regex(r"^yt_cookies_remove$"))
async def handle_yt_cookies_remove_cb(client, callback_query):
    """Inline Remove button from the /ytcookies panel."""
    user_id = callback_query.from_user.id
    if not _is_yt_admin(user_id):
        return await callback_query.answer("Admins only.", show_alert=True)

    await callback_query.answer("Removing cookies…")
    disk_removed, db_removed = await delete_youtube_cookies()
    set_state(user_id, None)

    disk_line = "✅ removed" if disk_removed else "ℹ️ nothing to remove"
    db_line = "✅ removed" if db_removed else "ℹ️ nothing to remove"

    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            "🗑 **YouTube cookies removed**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"• Disk: {disk_line}\n"
            f"• DB:   {db_line}\n\n"
            "Use /ytcookies to upload a fresh cookies.txt.",
            disable_web_page_preview=True,
        )


# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# Telegram Channel @XTVbots
# Developed for the 𝕏TV Network @XTVglobal
# Backup Channel @XTVhome
# Contact on Telegram @davdxpx
# --------------------------------------------------------------------------
