# --- Imports ---
import asyncio
import contextlib
import json
import re

from guessit import guessit

from utils.media.patterns import detect_all, flatten_specials, leading_episode_number
from utils.telegram.log import get_logger
from utils.tmdb import tmdb

logger = get_logger("utils.detect")


# === Template Key Normalization ===
# Historical reason: `Config.DEFAULT_FILENAME_TEMPLATES` stores "movies" (plural)
# but TMDB's `type` field yields "movie" (singular). Series happened to work
# because plural = singular for "series". This normalizer eliminates the
# mismatch everywhere template keys are built from a detected media type.
_TEMPLATE_KEY_ALIASES = {
    "movie": "movies",
    "movies": "movies",
    "series": "series",
    "tv": "series",
    "episode": "series",
    "show": "series",
}


def template_key_for(media_type, is_subtitle=False, personal_type=None):
    """Return the canonical key into Config.DEFAULT_FILENAME_TEMPLATES.

    - movie/movies → "movies"
    - series/tv/episode → "series"
    - subtitles get a "subtitles_" prefix
    - personal_type wins (personal_video/photo/file)
    """
    if personal_type:
        return f"personal_{personal_type}"
    if not media_type:
        return ""
    base = _TEMPLATE_KEY_ALIASES.get(str(media_type).lower(), str(media_type).lower())
    if is_subtitle:
        return f"subtitles_{base}"
    return base


# === FFprobe Audio Stream Analysis ===
async def probe_audio_streams(filepath, timeout=20):
    """Inspect audio streams of a media file via ffprobe.

    Returns one of "DUAL", "Multi", or None if nothing conclusive.
    - 1 stream → None (single audio, let caller keep empty)
    - 2 streams → "DUAL" if two distinct languages OR exactly two streams
    - 3+ streams → "Multi"
    - On error (ffprobe missing, bad file, timeout) → None; caller keeps state.
    """
    if not filepath:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index:stream_tags=language",
            "-of",
            "json",
            str(filepath),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
            logger.warning(f"ffprobe timeout on {filepath}")
            return None
    except FileNotFoundError:
        logger.warning("ffprobe not found on PATH; skipping audio stream probe.")
        return None
    except Exception as e:
        logger.warning(f"ffprobe failed on {filepath}: {e}")
        return None

    try:
        data = json.loads(stdout.decode("utf-8", errors="ignore") or "{}")
    except Exception as e:
        logger.warning(f"ffprobe JSON parse failed for {filepath}: {e}")
        return None

    streams = data.get("streams", []) or []
    count = len(streams)
    if count <= 1:
        return None
    # Distinct non-empty languages
    langs = {
        (s.get("tags") or {}).get("language", "").strip().lower()
        for s in streams
    }
    langs.discard("")
    langs.discard("und")
    if count >= 3:
        return "Multi"
    if count == 2:
        # If tags disagree OR missing, treat as DUAL (the common case for 2 audio tracks).
        return "DUAL"
    return None


def apply_autofill(fs):
    """Populate audio/codec/specials from runtime detection when safe.

    Rules (per field):
      - If `{field}_locked` is True → do nothing (user explicitly chose None).
      - If value already present → do nothing.
      - Else copy from `detected_{field}_runtime` if available.
    Mutates fs in place; returns set of filled field names.
    """
    filled = set()
    if not isinstance(fs, dict):
        return filled
    for field in ("audio", "codec"):
        if fs.get(f"{field}_locked"):
            continue
        if fs.get(field):
            continue
        runtime = fs.get(f"detected_{field}_runtime")
        if runtime:
            fs[field] = runtime
            filled.add(field)
    # Specials is a list — merge detected into existing if not locked.
    if not fs.get("specials_locked"):
        runtime_specials = fs.get("detected_specials_runtime") or []
        current = fs.get("specials") or []
        if runtime_specials and not current:
            fs["specials"] = list(dict.fromkeys(runtime_specials))
            filled.add("specials")
    return filled

# === Helper Functions ===
def analyze_filename(filename):
    try:
        # Pre-process filename to handle some edge cases guessit misses
        modified_f = filename

        # Pattern 1: X.YY or XX.YY (e.g., 8.01 -> S08E01)
        match = re.search(r'(?:^|[^\d])(\d{1,2})\.(\d{2})(?:[^\d]|$)', modified_f)
        is_date = False
        if match:
            start_idx = match.start(1)
            if start_idx >= 5:
                preceding = modified_f[start_idx-5:start_idx]
                if re.match(r'\d{4}\.', preceding):  # Avoid YYYY.MM.DD
                    is_date = True
            if not is_date:
                season = match.group(1)
                episode = match.group(2)
                prefix = modified_f[:match.start(1)]
                suffix = modified_f[match.end(2):]
                modified_f = f"{prefix} S{int(season):02d}E{int(episode):02d} {suffix}"

        # Pattern 2: X_YY, XX_YY, X-YY, XX-YY, X~YY, XX~YY (e.g., 8-01 -> S08E01)
        # Avoid breaking standard formats like 2x01-02 by checking if it's preceded by 'x'
        match = re.search(r'(?:^|[^\dx])(\d{1,2})[_~-](\d{2})(?:[^\d]|$)', modified_f, re.IGNORECASE)
        is_date = False
        if match:
            start_idx = match.start(1)
            if start_idx >= 5:
                preceding = modified_f[start_idx-5:start_idx]
                if re.match(r'\d{4}[_~-]', preceding):  # Avoid YYYY-MM-DD
                    is_date = True
            if not is_date:
                season = match.group(1)
                episode = match.group(2)
                prefix = modified_f[:match.start(1)]
                suffix = modified_f[match.end(2):]
                modified_f = f"{prefix} S{int(season):02d}E{int(episode):02d} {suffix}"

        guess = guessit(modified_f)

        media_type = "movie"
        if guess.get("type") == "episode":
            media_type = "series"

        is_subtitle = False
        container = guess.get("container")
        if container in ["srt", "ass", "sub", "vtt"] or filename.lower().endswith((".srt", ".ass", ".sub", ".vtt")):
            is_subtitle = True

        quality = str(guess.get("screen_size", "720p"))
        if quality not in ["1080p", "720p", "2160p", "480p"]:
            if "1080" in quality:
                quality = "1080p"
            elif "2160" in quality or "4k" in quality.lower():
                quality = "2160p"
            elif "480" in quality:
                quality = "480p"
            else:
                quality = "720p"

        language = "en"
        if guess.get("language"):
            with contextlib.suppress(TypeError, ValueError):
                language = str(guess.get("language"))
        elif guess.get("subtitle_language"):
            with contextlib.suppress(TypeError, ValueError):
                language = str(guess.get("subtitle_language"))

        # v1.6.2: central pattern library covers 8 groups with ordered
        # matching, separator normalisation, and cross-group span
        # dedup. Legacy fields (`specials` list, `codec` str, `audio`
        # str) stay on the return so existing renderers keep working.
        detected_groups = detect_all(filename)
        extracted_specials = flatten_specials(detected_groups)
        quality_from_patterns = detected_groups.get("quality")
        if isinstance(quality_from_patterns, str) and quality_from_patterns:
            quality = quality_from_patterns
        codec_label = detected_groups.get("codec")
        audio_val = detected_groups.get("audio")
        if isinstance(audio_val, list):
            audio_label = " | ".join(audio_val)
        else:
            audio_label = audio_val or ""

        season_val = guess.get("season")
        episode_val = guess.get("episode")

        # Post-process list-like episodes if guessit misidentified "Show - 08 - 01" as [8, 1]
        if isinstance(episode_val, list) and len(episode_val) == 2 and not season_val:
            season_val = episode_val[0]
            episode_val = episode_val[1]

        if isinstance(season_val, list) and len(season_val) > 0:
            season_val = season_val[0]

        # Leading bare episode index: "51. The Immortal Legion.mkv" is
        # episode 51 (absolute numbering, common for anime / long-running
        # shows). Only applied when nothing else produced season/episode,
        # so explicit SxxExx / 1x02 style names always win.
        if season_val is None and episode_val is None:
            lead_ep = leading_episode_number(filename)
            if lead_ep is not None:
                episode_val = lead_ep
                media_type = "series"

        return {
            "title": guess.get("title"),
            "year": guess.get("year"),
            "season": season_val,
            "episode": episode_val,
            "quality": quality,
            "type": media_type,
            "is_subtitle": is_subtitle,
            "container": container,
            "language": language,
            "specials": extracted_specials,
            "codec": codec_label if isinstance(codec_label, str) else "",
            "audio": audio_label if isinstance(audio_label, str) else "",
            # Structured groups for callers that want granular access
            # (per-group pickers, richer UI). Safe to ignore — legacy
            # callers keep using `specials` / `codec` / `audio`.
            "detected_groups": detected_groups,
        }

    except Exception as e:
        logger.error(f"Error analyzing filename '{filename}': {e}")
        return {
            "title": filename,
            "quality": "720p",
            "type": "movie",
            "is_subtitle": filename.lower().endswith((".srt", ".ass", ".sub", ".vtt")),
            "language": "en",
        }

async def auto_match_tmdb(metadata, language="en-US"):
    # If no TMDB key is configured, auto-matching is disabled. Callers
    # already treat None as "no match" and fall back to General Mode, so
    # this short-circuit keeps the rest of the pipeline untouched.
    from utils.tmdb.gate import is_tmdb_available

    if not is_tmdb_available():
        return None

    title = metadata.get("title")
    media_type = metadata.get("type")

    if not title:
        return None

    results = []
    try:
        if media_type == "series":
            results = await tmdb.search_tv(title, language=language)
        else:
            results = await tmdb.search_movie(title, language=language)

        if not results:
            return None

        best_match = results[0]
        tmdb_id = best_match["id"]

        details = await tmdb.get_details(best_match["type"], tmdb_id, language=language)

        if not details:
            return None

        final_type = "series" if best_match["type"] == "tv" else "movie"
        final_title = (
            details.get("title") if final_type == "movie" else details.get("name")
        )
        final_year = (
            details.get("release_date")
            if final_type == "movie"
            else details.get("first_air_date", "")
        )[:4]
        poster = (
            f"https://image.tmdb.org/t/p/w500{details.get('poster_path')}"
            if details.get("poster_path")
            else None
        )

        return {
            "tmdb_id": tmdb_id,
            "title": final_title,
            "year": final_year,
            "poster": poster,
            "overview": details.get("overview", ""),
            "type": final_type,
        }

    except Exception as e:
        logger.error(f"Error in auto_match_tmdb: {e}")
        return None

# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# Telegram Channel @XTVbots
# Developed for the 𝕏TV Network @XTVglobal
# Backup Channel @XTVhome
# Contact on Telegram @davdxpx
# --------------------------------------------------------------------------
