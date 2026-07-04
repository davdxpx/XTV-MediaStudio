"""Streaming HTTP(S) downloader for Mirror-Leech.

Production hardening (v1.6.3):
 * 3 attempts with exponential backoff — cloud hosts drop keep-alives
   mid-stream all the time.
 * HTTP Range **resume**: when the server advertises ``Accept-Ranges``
   the retry continues from the bytes already on disk instead of
   restarting a multi-GB download from zero.
 * ``Content-Length`` verification — a short read (proxy cut the
   stream) fails loudly instead of handing a truncated file to the
   uploaders.
 * Filename sanitation — ``Content-Disposition`` is attacker-controlled;
   path separators and drive prefixes are stripped so a malicious
   header can never escape the task temp dir.
 * Disk-space pre-check against the advertised size.
 * Browser-like User-Agent — several DDL hosts reject the default
   aiohttp UA with 403.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from tools.mirror_leech.downloaders import Downloader, register_downloader
from tools.mirror_leech.Tasks import MLContext
from utils.telegram.log import get_logger

logger = get_logger("mirror_leech.http")

_HTTP_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
_CHUNK = 1024 * 64
_MAX_FILENAME_LEN = 200
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 2.0  # 2s, 4s
_DISK_HEADROOM = 256 * 1024 * 1024  # keep 256 MB free beyond the file

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def sanitize_filename(name: str) -> str:
    """Make an attacker-controllable filename safe to join to a dir:
    strip directories, drive letters, control chars and null bytes; never
    return an empty or dot-only name."""
    name = (name or "").replace("\x00", "")
    # Both separators: Content-Disposition may carry either style.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"^[A-Za-z]:", "", name)
    name = "".join(ch for ch in name if ord(ch) >= 32)
    name = name.strip(". ")
    if not name:
        return "download.bin"
    return name[:_MAX_FILENAME_LEN]


def _derive_filename(source: str, resp) -> str:
    """Pick a sensible local filename from Content-Disposition / the URL."""
    disp = resp.headers.get("Content-Disposition", "")
    # RFC 5987 filename*= takes precedence over filename=
    match = re.search(r"filename\*=(?:UTF-8''|utf-8'')?\"?([^\";]+)", disp)
    if not match:
        match = re.search(r'filename="?([^";]+)', disp)
    if match:
        name = sanitize_filename(unquote(match.group(1)))
        if name != "download.bin":
            return name
    path = urlparse(source).path
    return sanitize_filename(unquote(os.path.basename(path)))


def _check_disk_space(dest_dir: Path, expected_bytes: float) -> None:
    if expected_bytes <= 0:
        return
    try:
        free = shutil.disk_usage(str(dest_dir)).free
    except OSError:
        return
    if free < expected_bytes + _DISK_HEADROOM:
        raise RuntimeError(
            f"Not enough disk space: need ~{int(expected_bytes / 1024 / 1024)} MB, "
            f"only {int(free / 1024 / 1024)} MB free."
        )


@register_downloader
class HTTPDownloader(Downloader):
    id = "http"
    display_name = "Direct URL (HTTP / HTTPS)"

    @classmethod
    async def accepts(cls, source: str, context: dict) -> bool:
        return bool(_HTTP_SCHEME.match(source))

    async def download(self, ctx: MLContext) -> Path:
        import aiohttp  # lazy: keep import cost off the worker-pool critical path

        ctx.status("downloading")

        dest_dir = ctx.temp_dir
        dest_dir.mkdir(parents=True, exist_ok=True)

        dest: Path | None = None
        expected_total = 0.0
        supports_resume = False
        last_exc: Exception | None = None

        for attempt in range(_MAX_ATTEMPTS):
            if ctx.cancelled():
                raise asyncio.CancelledError()
            if attempt:
                delay = _BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "HTTPDownloader retry %d/%d in %.0fs after: %s (%s)",
                    attempt + 1, _MAX_ATTEMPTS, delay, last_exc, ctx.source[:80],
                )
                await asyncio.sleep(delay)

            already = dest.stat().st_size if (dest and dest.exists()) else 0
            resume = bool(attempt and supports_resume and already > 0)
            headers = {"User-Agent": _UA}
            if resume:
                headers["Range"] = f"bytes={already}-"

            try:
                timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)
                async with aiohttp.ClientSession(timeout=timeout) as session, session.get(
                    ctx.source, allow_redirects=True, headers=headers
                ) as resp:
                    if resume and resp.status != 206:
                        # Server ignored the Range header — start over.
                        resume = False
                        already = 0
                    resp.raise_for_status()

                    if dest is None:
                        filename = _derive_filename(ctx.source, resp)
                        dest = dest_dir / filename

                    supports_resume = (
                        "bytes" in (resp.headers.get("Accept-Ranges") or "").lower()
                        or resp.status == 206
                    )

                    length = float(resp.headers.get("Content-Length") or 0)
                    if not resume:
                        expected_total = length
                    elif length:
                        # 206 responses advertise the REMAINING length.
                        expected_total = already + length
                    _check_disk_space(dest_dir, max(0.0, expected_total - already))

                    downloaded = float(already)
                    started = time.time()
                    mode = "ab" if resume else "wb"
                    with dest.open(mode) as f:
                        async for chunk in resp.content.iter_chunked(_CHUNK):
                            if ctx.cancelled():
                                logger.info("HTTPDownloader cancelled: %s", ctx.task_id)
                                raise asyncio.CancelledError()
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded += len(chunk)
                            elapsed = max(time.time() - started, 1e-3)
                            ctx.progress(
                                downloaded,
                                expected_total,
                                (downloaded - already) / elapsed,
                            )

                    # Short-read guard: a proxy or host that cut the
                    # stream early produces a truncated file that would
                    # otherwise be uploaded as if it were complete.
                    final_size = dest.stat().st_size
                    if expected_total > 0 and final_size < expected_total:
                        raise RuntimeError(
                            f"incomplete download: got {final_size} of "
                            f"{int(expected_total)} bytes"
                        )
                    if final_size == 0:
                        raise RuntimeError("server returned an empty file")
                    return dest
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                continue

        raise last_exc if last_exc else RuntimeError("HTTPDownloader: unreachable")
