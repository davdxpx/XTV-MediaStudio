"""Tests for the Mirror-Leech production-hardening layer: URL batch
extraction, filename sanitation, and history-doc building."""

import time
import types

from tools.mirror_leech.Controller import MAX_BATCH_URLS, extract_urls
from tools.mirror_leech.downloaders.HTTPDownloader import sanitize_filename
from tools.mirror_leech.History import build_history_doc

# --- extract_urls ------------------------------------------------------------


def test_extract_urls_multiline_and_spaces():
    text = "https://a.com/f1.mkv\nhttps://b.com/f2.mkv https://c.com/f3.mkv"
    assert extract_urls(text) == [
        "https://a.com/f1.mkv",
        "https://b.com/f2.mkv",
        "https://c.com/f3.mkv",
    ]


def test_extract_urls_dedupes_preserving_order():
    text = "https://a.com/x https://b.com/y https://a.com/x"
    assert extract_urls(text) == ["https://a.com/x", "https://b.com/y"]


def test_extract_urls_strips_trailing_punctuation():
    assert extract_urls("check https://a.com/file.mkv, thanks") == [
        "https://a.com/file.mkv"
    ]


def test_extract_urls_caps_at_limit():
    text = " ".join(f"https://h.com/{i}" for i in range(MAX_BATCH_URLS + 5))
    assert len(extract_urls(text)) == MAX_BATCH_URLS


def test_extract_urls_empty():
    assert extract_urls("") == []
    assert extract_urls("no links here") == []


# --- sanitize_filename ----------------------------------------------------------


def test_sanitize_strips_path_traversal():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("..\\..\\win\\boot.ini") == "boot.ini"


def test_sanitize_strips_drive_prefix():
    assert sanitize_filename("C:evil.exe") == "evil.exe"


def test_sanitize_strips_nulls_and_control_chars():
    assert sanitize_filename("fi\x00le\x01.mkv") == "file.mkv"


def test_sanitize_empty_and_dot_only_fall_back():
    assert sanitize_filename("") == "download.bin"
    assert sanitize_filename("...") == "download.bin"
    assert sanitize_filename("../..") == "download.bin"


def test_sanitize_keeps_normal_names():
    assert sanitize_filename("Movie.2024.1080p.mkv") == "Movie.2024.1080p.mkv"


def test_sanitize_caps_length():
    assert len(sanitize_filename("x" * 500 + ".mkv")) <= 200


# --- build_history_doc ------------------------------------------------------------


def _fake_task(**overrides):
    result = types.SimpleNamespace(uploader_id="telegram", ok=True, url="https://t.me/x/1")
    base = dict(
        id="abc123def456",
        user_id=42,
        source="https://example.com/file.mkv",
        downloader_id="http",
        uploader_ids=["telegram", "gdrive"],
        status="done",
        error=None,
        started_at=time.time() - 30,
        finished_at=time.time(),
        results=[result],
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_history_doc_shape():
    doc = build_history_doc(_fake_task())
    assert doc["user_id"] == 42
    assert doc["task_id"] == "abc123def456"
    assert doc["status"] == "done"
    assert doc["uploader_ids"] == ["telegram", "gdrive"]
    assert doc["results"] == [
        {"uploader": "telegram", "ok": True, "url": "https://t.me/x/1"}
    ]
    assert 25 <= doc["duration_sec"] <= 35
    assert doc["error"] is None
    assert doc["created_at"] is not None


def test_history_doc_truncates_long_source_and_error():
    doc = build_history_doc(
        _fake_task(source="https://x.com/" + "a" * 1000, error="e" * 1000, status="failed")
    )
    assert len(doc["source"]) <= 500
    assert len(doc["error"]) <= 300


def test_history_doc_handles_missing_timing():
    doc = build_history_doc(_fake_task(started_at=None, finished_at=None))
    assert doc["duration_sec"] is None
