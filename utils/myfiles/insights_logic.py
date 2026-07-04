# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""Pure logic for the MyFiles Insights screens.

No I/O, no Pyrogram — everything here operates on plain file docs so it
can be unit-tested (tests/test_myfiles_insights.py) and reused by both
the plugin handlers and any future exports.
"""

from __future__ import annotations

import contextlib
import re


def dupe_key(name: str) -> str:
    """Normalise a filename for duplicate grouping: lowercase, extension
    stripped, separators collapsed to single spaces."""
    stem = (name or "").rsplit(".", 1)[0].lower()
    return re.sub(r"[\s._\-]+", " ", stem).strip()


def episodes_of(f: dict) -> list[int]:
    """Best-effort episode number(s) of a file doc — explicit fields
    first, then an SxxEyy scan of the filename."""
    ep = f.get("episode")
    out: list[int] = []
    if isinstance(ep, list):
        for e in ep:
            with contextlib.suppress(Exception):
                out.append(int(e))
    elif ep is not None:
        with contextlib.suppress(Exception):
            out.append(int(ep))
    if out:
        return out
    m = re.search(r"[sS]\d{1,2}[eE](\d{1,3})", f.get("file_name", ""))
    if m:
        out.append(int(m.group(1)))
    return out


def season_of(f: dict) -> int | None:
    s = f.get("season")
    if s is not None:
        with contextlib.suppress(Exception):
            return int(str(s).lstrip("sS"))
    m = re.search(r"[sS](\d{1,2})", f.get("file_name", ""))
    if m:
        return int(m.group(1))
    return None


def build_completeness_report(files: list[dict]) -> list[str]:
    """Per-season coverage lines with missing episodes. Episodes are
    assumed to run 1..max(seen); gaps inside that range are missing."""
    seasons: dict[int, set[int]] = {}
    for f in files:
        season = season_of(f)
        if season is None:
            continue
        eps = episodes_of(f)
        seasons.setdefault(season, set()).update(eps)

    lines: list[str] = []
    for season in sorted(seasons):
        eps = seasons[season]
        if not eps:
            lines.append(f"`S{season:02d}` — episodes unknown")
            continue
        top = max(eps)
        missing = [e for e in range(1, top + 1) if e not in eps]
        if missing:
            miss_str = ", ".join(f"E{e:02d}" for e in missing[:12])
            if len(missing) > 12:
                miss_str += f" (+{len(missing) - 12})"
            lines.append(
                f"`S{season:02d}` — **{len(eps)}/{top}** · missing: {miss_str}"
            )
        else:
            lines.append(f"`S{season:02d}` — **{len(eps)}/{top}** ✅ complete")
    return lines
