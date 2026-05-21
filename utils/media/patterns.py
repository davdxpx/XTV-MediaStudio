# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""Filename metadata pattern library.

Central source of truth for the regex-based metadata detector used by
`utils.media.detect.analyze_filename` and the rename-flow confirm screen
in `plugins/flow.py`.

Design
------
* **Eight ordered groups** — SOURCE, HDR, AUDIO, EDITION, RELEASE, EXTRAS,
  QUALITY, CODEC. Each group has its own list of (regex, label) tuples.
* **Per-group priority** — patterns within a group are tried top-to-bottom,
  and a specific label beats a generic one because we consume matched
  character spans. That's how `AMZN WEB-DL` wins over `WEB-DL`, and
  `TrueHD Atmos` wins over plain `Atmos` when both would match the same
  substring.
* **Separator normalisation** — the `_SEP` class lets each pattern accept
  any of `.`, space, `_`, or `-` between tokens, so `WEB_DL`, `WEB-DL`,
  `WEB.DL`, and `WEB DL` all map to the same label.
* **Single vs. multi-pick** — `multi=False` groups stop after the first
  accepted match (SOURCE, HDR, AUDIO, EDITION, QUALITY, CODEC).
  `multi=True` groups (RELEASE, EXTRAS) collect every non-overlapping
  match.

The `detect_all()` entry point returns a dict keyed by group name with
a single string (single-pick) or a list of strings (multi-pick).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Any of `.`, whitespace, `_`, or `-`. Zero or one occurrence so both
# `WEB-DL` and `WEBDL` (rare but seen) hit.
_SEP = r"[\.\s_-]?"


@dataclass(frozen=True)
class PatternGroup:
    name: str
    multi: bool = False
    patterns: list[tuple[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------
# SOURCE — streaming service + release-type or physical disc.
# Streaming-service-prefixed variants MUST come first: "AMZN WEB-DL" has to
# match and consume its span before the plain "WEB-DL" pattern is tried.
# --------------------------------------------------------------------------
SOURCE = PatternGroup(
    name="source",
    multi=False,
    patterns=[
        # Streaming service + WEB-DL
        (rf"\bAMZN{_SEP}WEB{_SEP}?DL\b", "AMZN WEB-DL"),
        (rf"\bNF{_SEP}WEB{_SEP}?DL\b", "NF WEB-DL"),
        (rf"\bDSNP{_SEP}WEB{_SEP}?DL\b", "DSNP WEB-DL"),
        (rf"\bATVP{_SEP}WEB{_SEP}?DL\b", "ATVP WEB-DL"),
        (rf"\bAPTV{_SEP}WEB{_SEP}?DL\b", "APTV WEB-DL"),
        (rf"\bHULU{_SEP}WEB{_SEP}?DL\b", "HULU WEB-DL"),
        (rf"\bHMAX{_SEP}WEB{_SEP}?DL\b", "HMAX WEB-DL"),
        (rf"\bMAX{_SEP}WEB{_SEP}?DL\b", "MAX WEB-DL"),
        (rf"\bPMTP{_SEP}WEB{_SEP}?DL\b", "PMTP WEB-DL"),
        (rf"\bPCOK{_SEP}WEB{_SEP}?DL\b", "PCOK WEB-DL"),
        (rf"\bSTAN{_SEP}WEB{_SEP}?DL\b", "STAN WEB-DL"),
        (rf"\bCR{_SEP}WEB{_SEP}?DL\b", "CR WEB-DL"),
        (rf"\bCRAV{_SEP}WEB{_SEP}?DL\b", "CRAV WEB-DL"),
        (rf"\bFUNI{_SEP}WEB{_SEP}?DL\b", "FUNI WEB-DL"),
        (rf"\biT{_SEP}WEB{_SEP}?DL\b", "iT WEB-DL"),
        (rf"\biP{_SEP}WEB{_SEP}?DL\b", "iP WEB-DL"),
        # Streaming service + WEBRip
        (rf"\bAMZN{_SEP}WEBRip\b", "AMZN WEBRip"),
        (rf"\bNF{_SEP}WEBRip\b", "NF WEBRip"),
        (rf"\bDSNP{_SEP}WEBRip\b", "DSNP WEBRip"),
        (rf"\bATVP{_SEP}WEBRip\b", "ATVP WEBRip"),
        (rf"\bHULU{_SEP}WEBRip\b", "HULU WEBRip"),
        (rf"\bHMAX{_SEP}WEBRip\b", "HMAX WEBRip"),
        # Generic web
        (rf"\bWEB{_SEP}?DL\b", "WEB-DL"),
        (rf"\bWEBRip\b", "WEBRip"),
        (rf"\bWEB{_SEP}Rip\b", "WEBRip"),
        (rf"\bWEB\b", "WEB"),
        # UHD BluRay / UHD / BluRay Remux (the Remux label lives in
        # RELEASE, so here we just flag the disc family).
        (rf"\bUHD{_SEP}BluRay\b", "UHD BluRay"),
        (rf"\bUHD{_SEP}BD\b", "UHD BluRay"),
        (rf"\bBluRay\b", "BluRay"),
        (rf"\bBlu{_SEP}Ray\b", "BluRay"),
        (rf"\bBlueRay\b", "BluRay"),
        (rf"\bBDR\b", "BluRay"),
        (rf"\bBR\b", "BluRay"),
        (rf"\bBDRip\b", "BDRip"),
        (rf"\bBD{_SEP}Rip\b", "BDRip"),
        (rf"\bBRRip\b", "BRRip"),
        (rf"\bBR{_SEP}Rip\b", "BRRip"),
        (rf"\bWEB{_SEP}?Cap\b", "WEBCap"),
        # DVD / DVDRip
        (rf"\bDVDRip\b", "DVDRip"),
        (rf"\bDVD{_SEP}Rip\b", "DVDRip"),
        (rf"\bDVD{_SEP}5\b", "DVD5"),
        (rf"\bDVD{_SEP}9\b", "DVD9"),
        (rf"\bDVD\b", "DVD"),
        # TV captures
        (rf"\bHDTV\b", "HDTV"),
        (rf"\bPDTV\b", "PDTV"),
        (rf"\bSDTV\b", "SDTV"),
        (rf"\bTVRip\b", "TVRip"),
        (rf"\bTV{_SEP}Rip\b", "TVRip"),
        # Analog / ancient
        (rf"\bVHS{_SEP}Rip\b", "VHSRip"),
        (rf"\bVHS\b", "VHS"),
        (rf"\bLaserDisc\b", "LaserDisc"),
        (rf"\bLD\b", "LaserDisc"),
        # Other rips
        (rf"\bHDRip\b", "HDRip"),
        (rf"\bHD{_SEP}Rip\b", "HDRip"),
        (rf"\bSCR\b", "SCR"),
        (rf"\bDVDScr\b", "DVDScr"),
        (rf"\bR5\b", "R5"),
        (rf"\bCAM\b", "CAM"),
        (rf"\bCAMRip\b", "CAMRip"),
        (rf"\bHDCAM\b", "HDCAM"),
        (rf"\bHDTC\b", "HDTC"),
        (rf"\bTC\b", "TC"),
        (rf"\bTS\b", "TS"),
        (rf"\bHDTS\b", "HDTS"),
        (rf"\bPPV\b", "PPV"),
    ],
)


# --------------------------------------------------------------------------
# HDR — dynamic-range flag. Dolby Vision profile variants come first so
# "DV P5" / "DV P7" beat a plain "DV" match on the same span.
# --------------------------------------------------------------------------
HDR = PatternGroup(
    name="hdr",
    multi=False,
    patterns=[
        (rf"\bDV{_SEP}P8\b", "DV P8"),
        (rf"\bDV{_SEP}P7\b", "DV P7"),
        (rf"\bDV{_SEP}P5\b", "DV P5"),
        (rf"\bDoVi\b", "Dolby Vision"),
        (rf"\bDolby{_SEP}Vision\b", "Dolby Vision"),
        (rf"\bDV\b", "Dolby Vision"),
        (rf"\bHDR10\+\b", "HDR10+"),
        (rf"\bHDR10Plus\b", "HDR10+"),
        (rf"\bHDR10\b", "HDR10"),
        (rf"\bHLG\b", "HLG"),
        (rf"\bPQ\b", "PQ"),
        (rf"\bHDR\b", "HDR"),
        (rf"\b10bit\b", "10bit"),
        (rf"\b12bit\b", "12bit"),
        (rf"\bSDR\b", "SDR"),
    ],
)


# --------------------------------------------------------------------------
# AUDIO — codec + layout combinations. "TrueHD Atmos" and "DD+ Atmos"
# lead so the compound label wins when both tokens are side-by-side.
# --------------------------------------------------------------------------
AUDIO = PatternGroup(
    name="audio",
    multi=True,
    patterns=[
        # Multi-audio tags (higher priority in the group)
        (rf"\bDual{_SEP}Audio\b", "Dual Audio"),
        (rf"\bMulti{_SEP}Audio\b", "Multi Audio"),
        (rf"\bDUAL\b", "DUAL"),
        (rf"\bMULTI\b", "Multi"),
        # Atmos combos (need to beat plain Atmos / TrueHD / DD+)
        (rf"\bTrueHD{_SEP}Atmos\b", "TrueHD Atmos"),
        (rf"\bDD\+{_SEP}Atmos\b", "DD+ Atmos"),
        (rf"\bDDP{_SEP}Atmos\b", "DDP Atmos"),
        (rf"\bEAC3{_SEP}Atmos\b", "EAC3 Atmos"),
        # DTS family
        (rf"\bDTS{_SEP}HD{_SEP}MA\b", "DTS-HD MA"),
        (rf"\bDTS{_SEP}HD\b", "DTS-HD"),
        (rf"\bDTS{_SEP}X\b", "DTS:X"),
        (rf"\bDTS{_SEP}ES\b", "DTS-ES"),
        # High-end Dolby
        (rf"\bAtmos\b", "Atmos"),
        (rf"\bTrueHD\b", "TrueHD"),
        # Digital Plus family with channels
        (rf"\bDDP{_SEP}?5\.1\b", "DDP5.1"),
        (rf"\bDDP{_SEP}?7\.1\b", "DDP7.1"),
        (rf"\bDDP{_SEP}?2\.0\b", "DDP2.0"),
        (rf"\bDDP\b", "DDP"),
        (rf"\bDD\+\b", "DD+"),
        (rf"\bEAC3\b", "EAC3"),
        # AC3 / DD
        (rf"\bDD{_SEP}?5\.1\b", "DD5.1"),
        (rf"\bDD{_SEP}?7\.1\b", "DD7.1"),
        (rf"\bDD{_SEP}?2\.0\b", "DD2.0"),
        (rf"\bAC3\b", "AC3"),
        (rf"\bDD\b", "DD"),
        # DTS (generic — after DTS-HD etc.)
        (rf"\bDTS\b", "DTS"),
        # AAC variants
        (rf"\bAAC{_SEP}?5\.1\b", "AAC 5.1"),
        (rf"\bAAC{_SEP}?2\.0\b", "AAC 2.0"),
        (rf"\bAAC{_SEP}?LC\b", "AAC-LC"),
        (rf"\bAAC\b", "AAC"),
        # Lossless
        (rf"\bFLAC\b", "FLAC"),
        (rf"\bALAC\b", "ALAC"),
        (rf"\bLPCM\b", "LPCM"),
        (rf"\bPCM\b", "PCM"),
        # Lossy generic
        (rf"\bMP3\b", "MP3"),
        (rf"\bOPUS\b", "OPUS"),
        (rf"\bOGG\b", "OGG"),
        (rf"\bVorbis\b", "Vorbis"),
    ],
)


# --------------------------------------------------------------------------
# EDITION — cut / release edition. Multi-word editions come first so
# "Director's Cut" doesn't collapse to a lone "Director" word.
# --------------------------------------------------------------------------
EDITION = PatternGroup(
    name="edition",
    multi=False,
    patterns=[
        (rf"\bExtended{_SEP}Edition\b", "Extended Edition"),
        (rf"\bExtended{_SEP}Cut\b", "Extended Cut"),
        (rf"\bDirector'?s{_SEP}Cut\b", "Director's Cut"),
        (rf"\bUltimate{_SEP}Edition\b", "Ultimate Edition"),
        (rf"\bCollector'?s{_SEP}Edition\b", "Collector's Edition"),
        (rf"\bSpecial{_SEP}Edition\b", "Special Edition"),
        (rf"\bAnniversary{_SEP}Edition\b", "Anniversary Edition"),
        (rf"\bFinal{_SEP}Cut\b", "Final Cut"),
        (rf"\bTheatrical{_SEP}Cut\b", "Theatrical Cut"),
        (rf"\bIMAX{_SEP}Enhanced\b", "IMAX Enhanced"),
        (rf"\bOpen{_SEP}Matte\b", "Open Matte"),
        (rf"\bExtended\b", "Extended"),
        (rf"\bUnrated\b", "Unrated"),
        (rf"\bUncut\b", "Uncut"),
        (rf"\bUncensored\b", "Uncensored"),
        (rf"\bRemastered\b", "Remastered"),
        (rf"\bRestored\b", "Restored"),
        (rf"\bIMAX\b", "IMAX"),
        (rf"\bTheatrical\b", "Theatrical"),
    ],
)


# --------------------------------------------------------------------------
# RELEASE — multi-pick. PROPER + REPACK is a legit combination.
# --------------------------------------------------------------------------
RELEASE = PatternGroup(
    name="release",
    multi=True,
    patterns=[
        (rf"\bCriterion{_SEP}Collection\b", "Criterion"),
        (rf"\bCriterion\b", "Criterion"),
        (rf"\bREMUX\b", "REMUX"),
        (rf"\bReal{_SEP}PROPER\b", "Real PROPER"),
        (rf"\bPROPER\b", "PROPER"),
        (rf"\bREPACK\b", "REPACK"),
        (rf"\bRERIP\b", "RERIP"),
        (rf"\bINTERNAL\b", "INTERNAL"),
        (rf"\bLIMITED\b", "LIMITED"),
        (rf"\bREAD{_SEP}NFO\b", "READ.NFO"),
    ],
)


# --------------------------------------------------------------------------
# EXTRAS — multi-pick. Dubbing / sub flags. "Dual Audio" is more
# specific than "DUAL" and beats it when both would match.
# --------------------------------------------------------------------------
EXTRAS = PatternGroup(
    name="extras",
    multi=True,
    patterns=[
        (rf"\bDubbed\b", "Dubbed"),
        (rf"\bMicDub\b", "MicDub"),
        (rf"\bLineDub\b", "LineDub"),
        (rf"\bSubbed\b", "Subbed"),
        (rf"\bHardSubs?\b", "HardSubs"),
        (rf"\bSoftSubs?\b", "SoftSubs"),
        (rf"\bHardCoded\b", "HardCoded"),
        (rf"\bHC\b", "HardCoded"),
        (rf"\bEnglish{_SEP}Subs?\b", "EngSubs"),
        (rf"\bMultiSubs?\b", "MultiSubs"),
        # Bare "DL" (Dual-Language / download tag). Previously required
        # a negative look-behind to exclude "WEB-DL", but the SOURCE
        # group now consumes "WEB-DL" first so whatever's left is safe.
        (rf"\bDL\b", "DL"),
    ],
)


# --------------------------------------------------------------------------
# QUALITY — resolution / vertical pixel count. "4K" maps to 2160p.
# --------------------------------------------------------------------------
QUALITY = PatternGroup(
    name="quality",
    multi=False,
    patterns=[
        (rf"\b2160p\b", "2160p"),
        (rf"\b4K\b", "2160p"),
        (rf"\bUHD\b", "2160p"),
        (rf"\b1440p\b", "1440p"),
        (rf"\b1080p\b", "1080p"),
        (rf"\b1080i\b", "1080i"),
        (rf"\b720p\b", "720p"),
        (rf"\b576p\b", "576p"),
        (rf"\b540p\b", "540p"),
        (rf"\b480p\b", "480p"),
        (rf"\b360p\b", "360p"),
        (rf"\b240p\b", "240p"),
    ],
)


# --------------------------------------------------------------------------
# CODEC — video codec. Aliases collapse to a single canonical label so
# template output stays consistent (H.265 → HEVC, H.264 → x264 etc.).
# --------------------------------------------------------------------------
CODEC = PatternGroup(
    name="codec",
    multi=False,
    patterns=[
        (rf"\bx265\b", "x265"),
        (rf"\bh{_SEP}?265\b", "HEVC"),
        (rf"\bHEVC\b", "HEVC"),
        (rf"\bx264\b", "x264"),
        (rf"\bh{_SEP}?264\b", "x264"),
        (rf"\bAVC\b", "AVC"),
        (rf"\bAV1\b", "AV1"),
        (rf"\bVP9\b", "VP9"),
        (rf"\bVP8\b", "VP8"),
        (rf"\bMPEG{_SEP}?2\b", "MPEG-2"),
        (rf"\bMPEG{_SEP}?4\b", "MPEG-4"),
        (rf"\bVC{_SEP}?1\b", "VC-1"),
        (rf"\bXviD\b", "XviD"),
        (rf"\bDivX\b", "DivX"),
    ],
)


ALL_GROUPS: list[PatternGroup] = [
    SOURCE, HDR, AUDIO, EDITION, RELEASE, EXTRAS, QUALITY, CODEC,
]


# --------------------------------------------------------------------------
# Compiled cache — built once per module import.
# --------------------------------------------------------------------------
_COMPILED: dict[str, list[tuple[re.Pattern[str], str]]] = {
    g.name: [(re.compile(rx, re.IGNORECASE), label) for rx, label in g.patterns]
    for g in ALL_GROUPS
}
_GROUP_BY_NAME: dict[str, PatternGroup] = {g.name: g for g in ALL_GROUPS}


def _spans_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return not (a[1] <= b[0] or b[1] <= a[0])


def _match_group(
    name: str, text: str, used_spans: list[tuple[int, int]] | None = None,
) -> list[tuple[str, tuple[int, int]]]:
    """Collect (label, span) hits for a group, skipping overlaps.

    When `used_spans` is provided, spans from earlier groups are treated
    as already-consumed — so once SOURCE matches "AMZN WEB-DL" at span
    (10,20), EXTRAS won't re-match "DL" at span (17,19). The caller is
    expected to mutate `used_spans` in place so subsequent groups see
    the updated list.
    """
    group = _GROUP_BY_NAME[name]
    compiled = _COMPILED[name]
    accepted: list[tuple[str, tuple[int, int]]] = []
    local_used: list[tuple[int, int]] = list(used_spans) if used_spans else []
    new_spans: list[tuple[int, int]] = []
    for rx, label in compiled:
        for m in rx.finditer(text):
            span = m.span()
            if any(_spans_overlap(span, used) for used in local_used):
                continue
            accepted.append((label, span))
            local_used.append(span)
            new_spans.append(span)
            if not group.multi:
                if used_spans is not None:
                    used_spans.extend(new_spans)
                return accepted
            break  # one hit per pattern is enough for multi groups
    if used_spans is not None:
        used_spans.extend(new_spans)
    return accepted


def detect_group(
    name: str, filename: str, used_spans: list[tuple[int, int]] | None = None,
) -> list[str] | str | None:
    """Return the labels matched for a single group.

    Single-pick groups return either the matched label or None.
    Multi-pick groups always return a (possibly empty) list, preserving
    the order in which patterns fired.
    """
    if name not in _GROUP_BY_NAME:
        raise KeyError(f"unknown pattern group: {name}")
    hits = _match_group(name, filename, used_spans)
    labels = [label for label, _ in hits]
    group = _GROUP_BY_NAME[name]
    if group.multi:
        return labels
    return labels[0] if labels else None


def detect_all(filename: str) -> dict[str, list[str] | str | None]:
    """Detect every group in one pass.

    Groups are evaluated in the order of `ALL_GROUPS` and share a
    single `used_spans` list so that a more-specific match in an
    earlier group (SOURCE "AMZN WEB-DL") prevents a later group
    (EXTRAS) from re-matching a sub-span (stray "DL").
    """
    if not filename:
        return {g.name: ([] if g.multi else None) for g in ALL_GROUPS}
    used_spans: list[tuple[int, int]] = []
    result: dict[str, list[str] | str | None] = {}
    for group in ALL_GROUPS:
        result[group.name] = detect_group(group.name, filename, used_spans)
    return result


def flatten_specials(groups: dict[str, list[str] | str | None]) -> list[str]:
    """Collapse detected groups into a flat `specials` list.

    Mirrors the legacy contract of `analyze_filename`: codec / audio /
    quality are exposed as their own fields, everything else goes into
    a single ordered list de-duplicated by label. Ordering follows the
    group list: SOURCE → HDR → EDITION → RELEASE → EXTRAS.
    """
    flat: list[str] = []
    for name in ("source", "hdr", "edition", "release", "extras"):
        value = groups.get(name)
        if value is None:
            continue
        if isinstance(value, list):
            flat.extend(value)
        else:
            flat.append(value)
    return list(dict.fromkeys(flat))


# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# Telegram Channel @XTVbots
# Developed for the 𝕏TV Network @XTVglobal
# Backup Channel @XTVhome
# Contact on Telegram @davdxpx
# --------------------------------------------------------------------------
