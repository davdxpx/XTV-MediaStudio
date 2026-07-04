"""Glue between the task model and the provider implementations.

`run_task` executes one MLTask through the full pipeline:
   controller.pick_downloader → downloader.download → for uploader in
   task.uploader_ids: uploader.upload → cleanup temp dir.

The worker-pool code (Tasks.MLWorkerPool) stays agnostic of downloaders
and uploaders — it just schedules `runner(task)`. This module wires the
two sides together and is the only thing that needs a Pyrogram client to
drive progress-message edits.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from config import Config
from tools.mirror_leech.Controller import UnsupportedSourceError, pick_downloader
from tools.mirror_leech.downloaders import downloader_by_id
from tools.mirror_leech.Tasks import MLContext, MLTask, UploadResult
from tools.mirror_leech.uploaders import uploader_by_id
from utils.telegram.log import get_logger

logger = get_logger("mirror_leech.runner")

_TEMP_ROOT = Path(Config.DOWNLOAD_DIR) / "ml"


async def _resolve_downloader(task: MLTask):
    if task.downloader_id:
        cls = downloader_by_id(task.downloader_id)
        if cls is None:
            raise UnsupportedSourceError(
                f"Unknown downloader id '{task.downloader_id}'"
            )
        return cls
    cls = await pick_downloader(task.source)
    task.downloader_id = cls.id
    return cls


def _make_context(task: MLTask, client: Any, temp_dir: Path) -> MLContext:
    def _progress(done: float, total: float, speed: float) -> None:
        task.progress_fraction = (done / total) if total else 0.0
        task.speed_bps = speed
        task.eta_sec = int((total - done) / speed) if speed > 0 else 0

    def _status(s) -> None:
        task.status = s

    ctx = MLContext(
        task_id=task.id,
        user_id=task.user_id,
        source=task.source,
        temp_dir=temp_dir,
        cancel_event=task.cancel_event,
        report_progress=_progress,
        report_status=_status,
        source_kind=task.downloader_id or None,
    )
    # Some providers (TelegramDownloader / TelegramUploader) need the bot
    # client; attach it as an out-of-band attribute so the typed MLContext
    # surface stays clean for tests.
    ctx.client = client
    return ctx


async def run_task(task: MLTask, client: Any, progress_cb: Optional[Any] = None) -> None:
    """Execute `task` and mutate its fields to reflect the outcome.

    `progress_cb(task)` is invoked on every status change + periodically
    during the download (the downloader itself throttles). Callers
    typically pass `lambda t: ProgressRender.update_progress_message(client, t)`.
    """
    logger.info(
        "run_task start: task=%s downloader=%s source=%s uploaders=%s",
        task.id, task.downloader_id, task.source[:80], task.uploader_ids,
    )
    try:
        _TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.exception(
            "run_task %s: cannot create temp root %s", task.id, _TEMP_ROOT
        )
        task.status = "failed"
        task.error = f"temp dir unavailable: {exc}"
        raise

    # Baseline disk guard: refuse to even start when the volume is nearly
    # full — a doomed multi-GB download would only make things worse for
    # every other running task. Size-aware checks happen again inside the
    # downloaders once Content-Length is known.
    try:
        free = shutil.disk_usage(str(_TEMP_ROOT)).free
        if free < 512 * 1024 * 1024:
            task.status = "failed"
            task.error = (
                f"Server storage almost full ({int(free / 1024 / 1024)} MB free). "
                "Try again later."
            )
            raise RuntimeError(task.error)
    except RuntimeError:
        raise
    except Exception:
        pass  # disk_usage unavailable — proceed and let the download try
    temp_dir = Path(tempfile.mkdtemp(prefix=f"{task.id}-", dir=str(_TEMP_ROOT)))
    ctx = _make_context(task, client, temp_dir)

    # Inject progress side-effect so every report triggers the UI edit too.
    original_progress = ctx.report_progress

    def _piped_progress(done, total, speed):
        original_progress(done, total, speed)
        if progress_cb:
            try:
                # Callers may hand us a coroutine function or a sync fn.
                res = progress_cb(task)
                if hasattr(res, "__await__"):
                    import asyncio

                    asyncio.ensure_future(res)
            except Exception as exc:
                logger.debug("progress_cb raised: %s", exc)

    ctx.report_progress = _piped_progress

    local_file: Optional[Path] = None
    try:
        downloader_cls = await _resolve_downloader(task)
        logger.info(
            "run_task %s: resolved downloader=%s", task.id, downloader_cls.id
        )
        downloader = downloader_cls()
        local_file = await downloader.download(ctx)
        logger.info(
            "run_task %s: download finished -> %s", task.id, local_file
        )

        if task.cancel_event.is_set():
            task.status = "cancelled"
            return

        task.status = "uploading"
        if progress_cb:
            try:
                res = progress_cb(task)
                if hasattr(res, "__await__"):
                    import asyncio

                    await res
            except Exception:
                pass

        for uploader_id in task.uploader_ids:
            if task.cancel_event.is_set():
                task.results.append(
                    UploadResult(uploader_id, ok=False, message="cancelled")
                )
                continue
            cls = uploader_by_id(uploader_id)
            if cls is None:
                task.results.append(
                    UploadResult(uploader_id, ok=False, message="unknown uploader")
                )
                continue
            try:
                result = await cls().upload(ctx, local_file)
            except Exception as exc:
                logger.exception("Uploader %s crashed", uploader_id)
                result = UploadResult(uploader_id, ok=False, message=str(exc))
            task.results.append(result)

        # Task.status = "done" is set by the worker pool if no result sets
        # an earlier terminal state.

        # Best-effort webhook dispatch when every uploader finished (or
        # none were scheduled). Failures are silent — webhook delivery
        # must not poison the task outcome.
        if not task.cancel_event.is_set():
            with contextlib.suppress(Exception):
                from tools.mirror_leech import Webhooks

                payload = Webhooks.build_task_payload(
                    task.id, task.source, task.results
                )
                await Webhooks.fire(
                    task.user_id, Webhooks.EVENT_UPLOAD_DONE, payload
                )

        # QuotaCache becomes stale after a successful upload; invalidate
        # so the next /settings open re-polls in the background.
        if any(r.ok for r in task.results):
            with contextlib.suppress(Exception):
                from tools.mirror_leech import QuotaCache

                QuotaCache.invalidate(task.user_id)
    finally:
        # Always clean up — the file is either fully handed off upstream
        # (so losing the local copy is fine) or a partial download that
        # the user can retry.
        with contextlib.suppress(Exception):
            shutil.rmtree(temp_dir, ignore_errors=True)
