"""Tests for filename abbreviation, length limiting, and leading-episode
detection (utils/media/patterns.py + utils/template.py)."""

from utils.media.patterns import (
    abbreviate_filename,
    leading_episode_number,
)
from utils.template import FILENAME_SOFT_LIMIT, shorten_filename

# --- abbreviate_filename ----------------------------------------------------


def test_dolby_vision_abbreviates_to_dv():
    assert (
        abbreviate_filename("Movie.2024.2160p.Dolby.Vision.WEB-DL_[@ch]")
        == "Movie.2024.2160p.DV.WEB-DL_[@ch]"
    )


def test_abbreviation_is_separator_aware():
    assert abbreviate_filename("A Dolby Vision B") == "A DV B"
    assert abbreviate_filename("A_Dolby_Vision_B") == "A_DV_B"
    assert abbreviate_filename("A-Dolby-Vision-B") == "A-DV-B"


def test_abbreviation_case_insensitive():
    assert abbreviate_filename("dolby.vision") == "DV"


def test_compound_editions_abbreviate():
    assert abbreviate_filename("X.Extended.Edition.Y") == "X.Extended.Y"
    assert abbreviate_filename("X.Director's.Cut.Y") == "X.DC.Y"
    assert abbreviate_filename("X.Dual.Audio.Y") == "X.DUAL.Y"


def test_title_words_are_never_touched():
    name = "The.Vision.2024.1080p"  # 'Vision' alone must survive
    assert abbreviate_filename(name) == name


# --- leading_episode_number ---------------------------------------------------


def test_leading_episode_dot_space():
    assert leading_episode_number("51. The Immortal Legion.mkv") == 51


def test_leading_episode_dash():
    assert leading_episode_number("07 - Pilot.mkv") == 7
    assert leading_episode_number("7- Pilot.mkv") == 7


def test_leading_episode_paren():
    assert leading_episode_number("3) Homecoming.mkv") == 3


def test_scene_movie_names_do_not_match():
    # Dot without a space — classic scene naming, not an episode index.
    assert leading_episode_number("300.Rise.of.an.Empire.2014.1080p.mkv") is None


def test_four_digit_year_does_not_match():
    assert leading_episode_number("1917. Behind the Scenes.mkv") is None


def test_plain_number_title_does_not_match():
    assert leading_episode_number("22 Jump Street.mkv") is None


# --- shorten_filename -----------------------------------------------------------


def test_short_names_pass_through():
    assert shorten_filename("Movie.2024.1080p", ".mkv") == "Movie.2024.1080p"


def test_abbreviation_alone_can_fix_length():
    base = "A.Long.Movie.Title.Here.2024.2160p.Dolby.Vision.WEB-DL.x265"
    out = shorten_filename(base, ".mkv")
    assert "DV" in out
    assert "Dolby" not in out
    assert len(out) + len(".mkv") <= FILENAME_SOFT_LIMIT


def test_drops_low_priority_placeholders_when_too_long():
    template = "{Title}.{Year}.{Quality}.{Source}.{HDR}.{Release}.{Extras}_[{Channel}]"
    fmt = {
        "Title": "Some.Very.Long.Movie.Title.Indeed",
        "Year": "2024",
        "Quality": "2160p",
        "Source": "AMZN WEB-DL",
        "HDR": "HDR10+",
        "Release": "REMUX.PROPER",
        "Extras": "Dual Audio",
        "Channel": "@XTVglobal",
    }
    rendered, _ = __import__("utils.template", fromlist=["safe_format"]).safe_format(
        template, fmt
    )
    assert len(rendered) + 4 > FILENAME_SOFT_LIMIT  # sanity: starts too long
    out = shorten_filename(rendered, ".mkv", template=template, fmt=fmt)
    assert len(out) + len(".mkv") <= FILENAME_SOFT_LIMIT
    # Identity-carrying fields survive:
    assert "Some.Very.Long.Movie.Title.Indeed" in out
    assert "2160p" in out


def test_hard_truncate_as_last_resort():
    base = "X" * 200
    out = shorten_filename(base, ".mkv")
    assert len(out) + len(".mkv") <= FILENAME_SOFT_LIMIT


def test_extension_length_is_accounted_for():
    base = "B" * 62
    out = shorten_filename(base, ".mkv")
    assert len(out) + len(".mkv") <= FILENAME_SOFT_LIMIT
