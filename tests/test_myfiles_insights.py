"""Tests for the pure MyFiles Insights logic (utils/myfiles/insights_logic.py)."""

from utils.myfiles.insights_logic import (
    build_completeness_report,
    dupe_key,
    episodes_of,
    season_of,
)

# --- dupe_key ----------------------------------------------------------------


def test_dupe_key_ignores_extension_and_separators():
    assert dupe_key("Fallout.S01E01.1080p.mkv") == dupe_key("fallout s01e01 1080p.mp4")


def test_dupe_key_differs_for_different_names():
    assert dupe_key("Fallout.S01E01.mkv") != dupe_key("Fallout.S01E02.mkv")


def test_dupe_key_empty_name():
    assert dupe_key("") == ""
    assert dupe_key(None) == ""


# --- episode/season extraction --------------------------------------------------


def test_episodes_of_explicit_field():
    assert episodes_of({"episode": 5}) == [5]
    assert episodes_of({"episode": [1, 2]}) == [1, 2]


def test_episodes_of_from_filename():
    assert episodes_of({"file_name": "Show.S02E07.mkv"}) == [7]


def test_season_of_variants():
    assert season_of({"season": 3}) == 3
    assert season_of({"season": "S03"}) == 3
    assert season_of({"file_name": "Show.S04E01.mkv", "season": None}) == 4
    assert season_of({"file_name": "A Movie (2024).mkv"}) is None


# --- completeness report ---------------------------------------------------------


def _doc(season, episode, name="x.mkv"):
    return {"season": season, "episode": episode, "file_name": name}


def test_completeness_complete_season():
    files = [_doc(1, e) for e in (1, 2, 3)]
    lines = build_completeness_report(files)
    assert len(lines) == 1
    assert "S01" in lines[0]
    assert "3/3" in lines[0]
    assert "complete" in lines[0]


def test_completeness_reports_missing_episodes():
    files = [_doc(1, 1), _doc(1, 2), _doc(1, 5)]
    lines = build_completeness_report(files)
    assert "2/5" not in lines[0]  # 3 present of range 5
    assert "3/5" in lines[0]
    assert "E03" in lines[0]
    assert "E04" in lines[0]


def test_completeness_multi_episode_files_count():
    files = [_doc(2, [1, 2]), _doc(2, 3)]
    lines = build_completeness_report(files)
    assert "S02" in lines[0]
    assert "complete" in lines[0]


def test_completeness_multiple_seasons_sorted():
    files = [_doc(2, 1), _doc(1, 1)]
    lines = build_completeness_report(files)
    assert lines[0].startswith("`S01`")
    assert lines[1].startswith("`S02`")


def test_completeness_no_season_data():
    assert build_completeness_report([{"file_name": "Movie.2024.mkv"}]) == []
