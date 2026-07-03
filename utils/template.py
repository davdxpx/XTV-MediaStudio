# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""Filename-template helpers.

Admin filename templates, per-user rename templates, and the caption
template all go through Python's ``str.format()``. A single stray brace
(``"Title}.{Quality}"``) raises ``ValueError: Single '}' encountered in
format string`` deep inside the rename pipeline, and until now that
bubbled up as an unrecoverable task failure.

These helpers validate templates up-front at the input boundary and
provide a drop-in ``safe_format`` that never raises — if the template
is malformed, we return the raw template string untouched so the
caller can surface a clear error or fall back to a default.
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

_FORMATTER = string.Formatter()


def validate_template(
    template: str,
    allowed_fields: Optional[set[str]] = None,
) -> Tuple[bool, Optional[str]]:
    """Return ``(ok, error)`` for a user-supplied format template.

    ``ok`` is ``True`` iff ``str.format()`` would accept the template
    without raising ``ValueError`` / ``IndexError`` / ``KeyError`` for
    structural reasons (unbalanced braces, numeric positional fields,
    unknown placeholders when ``allowed_fields`` is provided).

    ``error`` is a short, user-facing message when ``ok`` is ``False``.
    """
    if not isinstance(template, str):
        return False, "Template must be a string."

    if not template.strip():
        return False, "Template cannot be empty."

    try:
        parsed = list(_FORMATTER.parse(template))
    except ValueError as exc:
        return False, f"Malformed template: {exc}. Use `{{Field}}` and escape literal braces as `{{{{` / `}}}}`."

    unknown: list[str] = []
    for _literal, field_name, _format_spec, _conversion in parsed:
        if field_name is None:
            continue
        root = field_name.split(".", 1)[0].split("[", 1)[0].strip()
        if not root:
            return False, "Positional placeholders like `{}` are not supported. Use named fields, e.g. `{Title}`."
        if root.isdigit():
            return False, "Numeric placeholders like `{0}` are not supported. Use named fields, e.g. `{Title}`."
        if allowed_fields is not None and root not in allowed_fields:
            unknown.append(root)

    if unknown:
        joined = ", ".join(f"`{{{name}}}`" for name in sorted(set(unknown)))
        return False, f"Unknown placeholder(s): {joined}."

    return True, None


def safe_format(template: str, mapping: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Format ``template`` with ``mapping`` without raising.

    Returns ``(result, error)``. On success ``error`` is ``None`` and
    ``result`` is the formatted string. On failure ``error`` describes
    the problem and ``result`` is the original ``template`` so callers
    can log the failure and fall back to a known-good default.
    """
    ok, err = validate_template(template)
    if not ok:
        return template, err

    try:
        return template.format(**mapping), None
    except (KeyError, IndexError, ValueError) as exc:
        return template, f"Template formatting failed: {exc}."


# ---------------------------------------------------------------------------
# Placeholder catalogue
# ---------------------------------------------------------------------------
# Single source of truth for every placeholder a user can drop into a
# filename / caption / metadata template. Each placeholder lives in exactly
# one group; scopes (filename_series, metadata_title, caption, …) opt into
# the groups they support. The admin and user UIs read this catalogue to
# render the Placeholder Reference screen; the runtime side reads
# ``allowed_fields_for(scope)`` to validate input.


@dataclass(frozen=True)
class Placeholder:
    name: str
    description: str
    example: str
    source: str


@dataclass(frozen=True)
class PlaceholderGroup:
    key: str
    title: str
    emoji: str
    fields: Tuple[Placeholder, ...]


def _ph(name: str, description: str, example: str, source: str) -> Placeholder:
    return Placeholder(name=name, description=description, example=example, source=source)


# BASIC — applies to every scope. Both PascalCase and lowercase aliases are
# registered so legacy metadata templates (``{title}``) keep working next
# to new PascalCase filename templates (``{Title}``).
_BASIC = PlaceholderGroup(
    key="BASIC",
    title="Basic",
    emoji="🏷",
    fields=(
        _ph("Title", "Media title (sanitised)", "Fallout", "self.title"),
        _ph("title", "Media title (sanitised)", "Fallout", "self.title"),
        _ph("Year", "Release year", "2024", "self.year"),
        _ph("year", "Release year", "2024", "self.year"),
        _ph("Channel", "Default Telegram channel", "@XTVglobal", "self.channel"),
        _ph("channel", "Default Telegram channel", "@XTVglobal", "self.channel"),
        _ph("Language", "Preferred language code", "en", "self.language"),
        _ph("language", "Preferred language code", "en", "self.language"),
        _ph("lang", "Preferred language code (alias)", "en", "self.language"),
        _ph("Uploader", "Telegram handle or id:N", "@davdxpx", "self.user_id"),
    ),
)

_EPISODE = PlaceholderGroup(
    key="EPISODE",
    title="Episode",
    emoji="📺",
    fields=(
        _ph("Season", "Season tag", "S01", "self.season"),
        _ph("SeasonNum", "Season number (zero-padded)", "01", "self.season"),
        _ph("Episode", "Episode tag", "E01", "self.episode"),
        _ph("EpisodeNum", "Episode number (zero-padded)", "01", "self.episode"),
        _ph("Season_Episode", "Season+Episode combined", "S01E01", "combined"),
        _ph("season_episode", "Season+Episode combined (alias)", "S01E01", "combined"),
        _ph("SeriesName", "Series name (series only)", "Fallout", "self.title"),
        _ph("series_name", "Series name (alias)", "Fallout", "self.title"),
        _ph("NumSeasons", "Total seasons (TMDb)", "2", "tmdb.number_of_seasons"),
        _ph("NumEpisodes", "Total episodes (TMDb)", "16", "tmdb.number_of_episodes"),
        _ph("FirstAirYear", "First air year", "2024", "tmdb.first_air_date"),
        _ph("season", "Season number (alias)", "01", "self.season"),
        _ph("episode", "Episode number (alias)", "01", "self.episode"),
    ),
)

_TECHNICAL = PlaceholderGroup(
    key="TECHNICAL",
    title="Technical",
    emoji="🎞",
    fields=(
        _ph("Quality", "Source quality", "1080p", "self.quality"),
        _ph("quality", "Source quality (alias)", "1080p", "self.quality"),
        _ph("Resolution", "Width×Height", "1920x1080", "probe.video[0]"),
        _ph("resolution", "Width×Height (alias)", "1920x1080", "probe.video[0]"),
        _ph("Width", "Frame width", "1920", "probe.video[0].width"),
        _ph("Height", "Frame height", "1080", "probe.video[0].height"),
        _ph("Duration", "HH:MM:SS", "01:52:03", "probe.format.duration"),
        _ph("duration", "HH:MM:SS (alias)", "01:52:03", "probe.format.duration"),
        _ph("DurationSec", "Duration in seconds", "6723", "probe.format.duration"),
        _ph("VideoCodec", "Video codec", "h264", "probe.video[0].codec_name"),
        _ph("video_codec", "Video codec (alias)", "h264", "probe.video[0].codec_name"),
        _ph("AudioCodec", "Primary audio codec", "eac3", "probe.audio[0].codec_name"),
        _ph("audio_codec", "Primary audio codec (alias)", "eac3", "probe.audio[0].codec_name"),
        _ph("AudioChannels", "Channel layout", "5.1", "probe.audio[0].channels"),
        _ph("audio_channels", "Channel layout (alias)", "5.1", "probe.audio[0].channels"),
        _ph("AudioLang", "Primary audio language", "en", "probe.audio[0].tags.language"),
        _ph("audio_lang", "Primary audio language (alias)", "en", "probe.audio[0].tags.language"),
        _ph("Bitrate", "Overall bitrate", "8.5 Mbps", "probe.format.bit_rate"),
        _ph("FrameRate", "Frames per second", "23.976", "probe.video[0].r_frame_rate"),
    ),
)

_SOURCE = PlaceholderGroup(
    key="SOURCE",
    title="Source",
    emoji="🎬",
    fields=(
        _ph("Source", "Release source", "WEB-DL", "pattern.source"),
        _ph("HDR", "HDR flavour", "DV", "pattern.hdr"),
        _ph("Edition", "Edition (Director's Cut, …)", "Extended", "pattern.edition"),
        _ph("Release", "Release group(s)", "RARBG", "pattern.release"),
        _ph("Extras", "Extras tags", "Commentary", "pattern.extras"),
        _ph("Specials", "Specials tags (legacy)", "REMUX", "self.data.specials"),
        _ph("Codec", "Detected codec (legacy)", "x265", "self.data.codec"),
        _ph("Audio", "Detected audio (legacy)", "DD+5.1", "self.data.audio"),
    ),
)

_TMDB = PlaceholderGroup(
    key="TMDB",
    title="TMDb",
    emoji="📖",
    fields=(
        _ph("TMDbId", "TMDb ID", "12345", "self.tmdb_id"),
        _ph("tmdb_id", "TMDb ID (alias)", "12345", "self.tmdb_id"),
        _ph("OriginalTitle", "Original title", "Fallout", "tmdb.original_title"),
        _ph("Overview", "Plot overview (trimmed per scope)", "In a post-apocalyptic …", "tmdb.overview"),
        _ph("overview", "Plot overview (alias)", "In a post-apocalyptic …", "tmdb.overview"),
        _ph("Rating", "TMDb rating (1 decimal)", "7.8", "tmdb.vote_average"),
        _ph("rating", "TMDb rating (alias)", "7.8", "tmdb.vote_average"),
        _ph("Runtime", "Runtime", "128m", "tmdb.runtime"),
        _ph("runtime", "Runtime (alias)", "128m", "tmdb.runtime"),
        _ph("Genres", "Genres joined", "Action.Sci-Fi", "tmdb.genres"),
        _ph("genres", "Genres joined (alias)", "Action.Sci-Fi", "tmdb.genres"),
        _ph("Tagline", "Tagline", "War never changes.", "tmdb.tagline"),
        _ph("tagline", "Tagline (alias)", "War never changes.", "tmdb.tagline"),
        _ph("OriginalLanguage", "Original language", "en", "tmdb.original_language"),
        _ph("Countries", "Production countries", "US.UK", "tmdb.production_countries"),
        _ph("Network", "Network (series only)", "Prime Video", "tmdb.networks"),
        _ph("network", "Network (alias)", "Prime Video", "tmdb.networks"),
        _ph("ReleaseDate", "Release or first-air date", "2024-04-10", "tmdb.release_date"),
        _ph("release_date", "Release date (alias)", "2024-04-10", "tmdb.release_date"),
    ),
)

_FILE = PlaceholderGroup(
    key="FILE",
    title="File",
    emoji="📁",
    fields=(
        _ph("Filename", "Original filename without extension", "Fallout.S01E01", "self.original_name"),
        _ph("filename", "Original filename (alias)", "Fallout.S01E01", "self.original_name"),
        _ph("Ext", "Output extension (with dot)", ".mkv", "self.output_path"),
        _ph("Size", "Human-readable file size", "1.5 GB", "os.path.getsize"),
        _ph("size", "File size (alias)", "1.5 GB", "os.path.getsize"),
        _ph("SizeBytes", "File size in bytes", "1610612736", "os.path.getsize"),
        _ph("Date", "Upload date", "2026-04-21", "datetime.utcnow"),
        _ph("Time", "Upload time (UTC)", "13:33", "datetime.utcnow"),
        _ph("Random", "Random 8-char token", "a9K2xLq0", "secrets.token_urlsafe"),
        _ph("random", "Random token (alias)", "a9K2xLq0", "secrets.token_urlsafe"),
        _ph("Hashtag", "Auto hashtag", "#FalloutS01", "derived"),
        _ph("hashtag", "Auto hashtag (alias)", "#FalloutS01", "derived"),
    ),
)

CATALOG: Dict[str, PlaceholderGroup] = {
    "BASIC": _BASIC,
    "EPISODE": _EPISODE,
    "TECHNICAL": _TECHNICAL,
    "SOURCE": _SOURCE,
    "TMDB": _TMDB,
    "FILE": _FILE,
}


# ---------------------------------------------------------------------------
# Scope → group mapping
# ---------------------------------------------------------------------------
# Scopes ending in ``_lower`` in the code below would be redundant; the
# lowercase aliases live in the same groups as their PascalCase siblings,
# and SCOPE_GROUPS simply enumerates which groups apply per scope. The
# lowercase/PascalCase split happens inside each scope via
# ``_SCOPE_CASE`` below.

SCOPE_GROUPS: Dict[str, Tuple[str, ...]] = {
    # Metadata scopes — lowercase conventions (matches DEFAULT_TEMPLATES).
    "metadata_title":       ("BASIC", "EPISODE", "TMDB"),
    "metadata_author":      ("BASIC",),
    "metadata_artist":      ("BASIC", "TMDB"),
    "metadata_video":       ("BASIC", "TECHNICAL", "TMDB"),
    "metadata_audio":       ("BASIC", "TECHNICAL", "EPISODE"),
    "metadata_subtitle":    ("BASIC", "EPISODE"),
    "metadata_comment":     ("BASIC", "TMDB", "EPISODE"),
    "metadata_copyright":   ("BASIC",),
    "metadata_description": ("BASIC", "TMDB", "EPISODE"),
    "metadata_genre":       ("TMDB",),
    "metadata_date":        ("BASIC", "TMDB"),
    "metadata_album":       ("BASIC",),
    "metadata_show":        ("BASIC", "EPISODE"),
    "metadata_network":     ("BASIC", "TMDB"),

    # Filename scopes — PascalCase conventions (matches DEFAULT_FILENAME_TEMPLATES).
    "filename_movies":          ("BASIC", "TECHNICAL", "SOURCE", "TMDB"),
    "filename_series":          ("BASIC", "EPISODE", "TECHNICAL", "SOURCE", "TMDB"),
    "filename_subs_movies":     ("BASIC", "TMDB"),
    "filename_subs_series":     ("BASIC", "EPISODE"),
    "filename_personal_video":  ("BASIC", "FILE", "TECHNICAL"),
    "filename_personal_photo":  ("BASIC", "FILE"),
    "filename_personal_file":   ("BASIC", "FILE"),
    "filename_general":         ("BASIC", "TECHNICAL", "SOURCE", "TMDB", "FILE"),

    # System filename scopes — lowercase, used by library storage.
    "system_filename_movies":   ("BASIC", "TMDB"),
    "system_filename_series":   ("BASIC", "EPISODE", "TMDB"),

    # Caption — everything goes.
    "caption": ("BASIC", "EPISODE", "TECHNICAL", "SOURCE", "TMDB", "FILE"),
}

# Case convention per scope: "pascal" → only PascalCase aliases are allowed,
# "lower" → only lowercase aliases, "both" → both accepted. The case filter
# is applied on top of SCOPE_GROUPS when building the allowed_fields set.
_SCOPE_CASE: Dict[str, str] = {
    "metadata_title":       "lower",
    "metadata_author":      "lower",
    "metadata_artist":      "lower",
    "metadata_video":       "lower",
    "metadata_audio":       "lower",
    "metadata_subtitle":    "lower",
    "metadata_comment":     "lower",
    "metadata_copyright":   "lower",
    "metadata_description": "lower",
    "metadata_genre":       "lower",
    "metadata_date":        "lower",
    "metadata_album":       "lower",
    "metadata_show":        "lower",
    "metadata_network":     "lower",
    "filename_movies":          "pascal",
    "filename_series":          "pascal",
    "filename_subs_movies":     "pascal",
    "filename_subs_series":     "pascal",
    "filename_personal_video":  "pascal",
    "filename_personal_photo":  "pascal",
    "filename_personal_file":   "pascal",
    "filename_general":         "both",
    "system_filename_movies":   "lower",
    "system_filename_series":   "lower",
    "caption":                  "both",
}


def _is_pascal(name: str) -> bool:
    # Pascal if first char upper; lowercase if first char lower. Treat
    # camelCase technical names like ``r_frame_rate`` as lowercase.
    return bool(name) and name[0].isupper()


# Curated "top hints" shown inline on the edit screen so users see the
# most common placeholders without tapping the Reference button.
SCOPE_TOP_HINTS: Dict[str, Tuple[str, ...]] = {
    "metadata_title":       ("title", "year", "season_episode", "overview", "rating"),
    "metadata_author":      ("title", "channel"),
    "metadata_artist":      ("title", "network"),
    "metadata_video":       ("video_codec", "resolution", "quality", "title"),
    "metadata_audio":       ("audio_codec", "audio_channels", "lang", "title"),
    "metadata_subtitle":    ("title", "lang", "season_episode"),
    "metadata_comment":     ("overview", "tagline", "title"),
    "metadata_copyright":   ("year", "channel"),
    "metadata_description": ("tagline", "overview", "title"),
    "metadata_genre":       ("genres",),
    "metadata_date":        ("release_date", "year"),
    "metadata_album":       ("title", "year"),
    "metadata_show":        ("title", "season_episode"),
    "metadata_network":     ("network", "title"),
    "filename_movies":          ("Title", "Year", "Quality", "Channel", "Resolution", "Source", "HDR", "Rating"),
    "filename_series":          ("Title", "Season_Episode", "Quality", "Channel", "Resolution", "Source", "HDR", "Rating"),
    "filename_subs_movies":     ("Title", "Year", "Language", "Channel"),
    "filename_subs_series":     ("Title", "Season_Episode", "Language"),
    "filename_personal_video":  ("Title", "Year", "Channel", "Resolution", "Duration"),
    "filename_personal_photo":  ("Title", "Year", "Channel", "Date"),
    "filename_personal_file":   ("Title", "Year", "Channel", "Date", "Ext"),
    "filename_general":         ("filename", "Title", "Year", "Quality", "Channel", "Source"),
    "system_filename_movies":   ("title", "year", "tmdb_id", "rating"),
    "system_filename_series":   ("series_name", "season", "episode", "tmdb_id"),
    "caption":                  ("filename", "size", "duration", "resolution", "rating", "hashtag", "overview"),
}


def allowed_fields_for(scope: str) -> frozenset[str]:
    """Return the set of placeholder names accepted for ``scope``.

    Combines ``SCOPE_GROUPS[scope]`` with the case convention from
    ``_SCOPE_CASE``. Raises ``KeyError`` for unknown scopes — callers
    must use scope names defined here.
    """
    groups = SCOPE_GROUPS[scope]
    case = _SCOPE_CASE.get(scope, "both")
    names: set[str] = set()
    for group_key in groups:
        for ph in CATALOG[group_key].fields:
            if case == "pascal" and not _is_pascal(ph.name):
                continue
            if case == "lower" and _is_pascal(ph.name):
                continue
            names.add(ph.name)
    return frozenset(names)


def placeholders_for(scope: str, group_key: Optional[str] = None) -> List[Placeholder]:
    """Return the placeholders available in ``scope``, optionally filtered
    to a single group. Used by the Reference screen."""
    groups = SCOPE_GROUPS[scope]
    case = _SCOPE_CASE.get(scope, "both")
    out: List[Placeholder] = []
    for gk in groups:
        if group_key is not None and gk != group_key:
            continue
        for ph in CATALOG[gk].fields:
            if case == "pascal" and not _is_pascal(ph.name):
                continue
            if case == "lower" and _is_pascal(ph.name):
                continue
            out.append(ph)
    return out


def groups_for(scope: str) -> List[PlaceholderGroup]:
    """Return the ordered groups allowed in ``scope``."""
    return [CATALOG[g] for g in SCOPE_GROUPS[scope]]


# ---------------------------------------------------------------------------
# Sample mapping for the Preview button
# ---------------------------------------------------------------------------
# Deterministic values so the preview renders the same way every time and
# the user can sanity-check what their template does without uploading a
# real file. Every placeholder across every scope needs a value here — a
# missing key turns into an empty string via safe_format's fallback.

SAMPLE_MAPPING: Dict[str, str] = {
    # BASIC
    "Title": "Fallout", "title": "Fallout",
    "Year": "2024", "year": "2024",
    "Channel": "@XTVglobal", "channel": "@XTVglobal",
    "Language": "en", "language": "en", "lang": "en",
    "Uploader": "@davdxpx",
    # EPISODE
    "Season": "S01", "SeasonNum": "01", "season": "01",
    "Episode": "E01", "EpisodeNum": "01", "episode": "01",
    "Season_Episode": "S01E01", "season_episode": "S01E01",
    "SeriesName": "Fallout", "series_name": "Fallout",
    "NumSeasons": "2", "NumEpisodes": "16",
    "FirstAirYear": "2024",
    # TECHNICAL
    "Quality": "1080p", "quality": "1080p",
    "Resolution": "1920x1080", "resolution": "1920x1080",
    "Width": "1920", "Height": "1080",
    "Duration": "01:52:03", "duration": "01:52:03", "DurationSec": "6723",
    "VideoCodec": "h264", "video_codec": "h264",
    "AudioCodec": "eac3", "audio_codec": "eac3",
    "AudioChannels": "5.1", "audio_channels": "5.1",
    "AudioLang": "en", "audio_lang": "en",
    "Bitrate": "8.5 Mbps", "FrameRate": "23.976",
    # SOURCE
    "Source": "WEB-DL", "HDR": "DV", "Edition": "Extended",
    "Release": "RARBG", "Extras": "Commentary",
    "Specials": "REMUX", "Codec": "x265", "Audio": "DD+5.1",
    # TMDB
    "TMDbId": "12345", "tmdb_id": "12345",
    "OriginalTitle": "Fallout",
    "Overview": "In a post-apocalyptic Los Angeles, the haves and have-nots clash.",
    "overview": "In a post-apocalyptic Los Angeles, the haves and have-nots clash.",
    "Rating": "7.8", "rating": "7.8",
    "Runtime": "128m", "runtime": "128m",
    "Genres": "Action.Sci-Fi", "genres": "Action.Sci-Fi",
    "Tagline": "War never changes.", "tagline": "War never changes.",
    "OriginalLanguage": "en", "Countries": "US.UK",
    "Network": "Prime Video", "network": "Prime Video",
    "ReleaseDate": "2024-04-10", "release_date": "2024-04-10",
    # FILE
    "Filename": "Fallout.S01E01", "filename": "Fallout.S01E01",
    "Ext": ".mkv",
    "Size": "1.5 GB", "size": "1.5 GB", "SizeBytes": "1610612736",
    "Date": "2026-04-21", "Time": "13:33",
    "Random": "a9K2xLq0", "random": "a9K2xLq0",
    "Hashtag": "#FalloutS01", "hashtag": "#FalloutS01",
}


def render_preview(scope: str, template: str) -> str:
    """Render ``template`` with ``SAMPLE_MAPPING``. Always returns a
    string: on malformed templates the error text becomes the preview
    so users see what they broke."""
    result, err = safe_format(template, SAMPLE_MAPPING)
    if err:
        return f"⚠️ {err}"
    return result


# ---------------------------------------------------------------------------
# Scope-aware truncation
# ---------------------------------------------------------------------------

# Placeholders that can realistically produce very long strings. Everything
# else falls through truncate_for_scope() untouched — {Title} or {Channel}
# are user-controlled and not worth clipping at render time.
_TRIM_NAMES: frozenset[str] = frozenset({
    "Overview", "overview",
    "Tagline", "tagline",
    "Genres", "genres",
    "Countries",
    "Networks", "network", "Network",
})

# Per-scope length cap (characters). ``None`` means "no cap".
def _scope_cap(scope: str) -> Optional[int]:
    if scope.startswith("filename_") or scope.startswith("system_filename_"):
        return 80
    if scope == "caption":
        return 400
    if scope.startswith("metadata_"):
        return None
    return 400  # conservative default


def truncate_for_scope(value: str, scope: str, placeholder_name: str) -> str:
    """Clip ``value`` to the scope-appropriate length when the placeholder
    is known to produce long strings. Filename scopes cap at 80 chars,
    caption at 400, metadata keeps the full value so ffmpeg tags get the
    whole overview/tagline.

    Non-string values (defensive) and short values pass through unchanged.
    """
    if not isinstance(value, str):
        return value
    if placeholder_name not in _TRIM_NAMES:
        return value
    cap = _scope_cap(scope)
    if cap is None or len(value) <= cap:
        return value
    return value[: cap - 1].rstrip() + "…"


def apply_trim(mapping: Dict[str, str], scope: str) -> Dict[str, str]:
    """Apply :func:`truncate_for_scope` across a full mapping. Used by
    ``TaskProcessor._build_fmt_dict`` right before format time."""
    return {k: truncate_for_scope(v, scope, k) for k, v in mapping.items()}


# ---------------------------------------------------------------------------
# Output filename length control
# ---------------------------------------------------------------------------
# Telegram truncates long document names in chat lists and several player
# apps choke on very long names, so output filenames should stay under 64
# characters including the extension whenever possible.

FILENAME_SOFT_LIMIT = 64

# When a rendered filename exceeds the limit, placeholders are blanked in
# this order (least important first) and the template re-rendered until the
# name fits. Title / Season+Episode / Quality / Year / Channel are never
# dropped automatically — they carry the identity of the file.
_LENGTH_DROP_ORDER: Tuple[str, ...] = (
    "Extras",
    "Release",
    "Edition",
    "Audio",
    "Codec",
    "Specials",
    "Source",
    "HDR",
)


def shorten_filename(
    base_name: str,
    ext: str = "",
    *,
    template: Optional[str] = None,
    fmt: Optional[Dict[str, Any]] = None,
    cleaner=None,
    limit: int = FILENAME_SOFT_LIMIT,
) -> str:
    """Keep ``base_name + ext`` at or under ``limit`` characters.

    Steps, stopping as soon as the name fits:
      1. Apply scene abbreviations ("Dolby.Vision" → "DV", …).
      2. If ``template``/``fmt`` are given, blank low-priority placeholders
         in ``_LENGTH_DROP_ORDER`` and re-render (running ``cleaner`` on
         each candidate so separator collapsing matches the main pipeline).
      3. Hard-truncate as a last resort, preserving the extension.

    Returns the (possibly shortened) base name WITHOUT the extension.
    """
    # Local import: utils.media.patterns has no dependencies of its own,
    # but importing it lazily keeps this module import-light for callers
    # that only need validate_template/safe_format.
    from utils.media.patterns import abbreviate_filename

    name = abbreviate_filename(base_name)
    if len(name) + len(ext) <= limit:
        return name

    if template and fmt:
        reduced = dict(fmt)
        for key in _LENGTH_DROP_ORDER:
            if not reduced.get(key):
                continue
            reduced[key] = ""
            rendered, err = safe_format(template, reduced)
            if err:
                break
            candidate = cleaner(rendered) if cleaner else rendered
            candidate = abbreviate_filename(candidate)
            if candidate:
                name = candidate
            if len(name) + len(ext) <= limit:
                return name

    if len(name) + len(ext) > limit:
        keep = max(8, limit - len(ext))
        name = name[:keep].rstrip("._- ")
    return name
