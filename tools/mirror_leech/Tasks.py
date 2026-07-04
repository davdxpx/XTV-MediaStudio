"""Mirror-Leech task model and worker pool.

An `MLTask` is the unit of work — one source URL / file ref → one local
download → one or more uploads to configured destinations. `ml_worker_pool`
runs tasks with per-user + global concurrency caps and updates a shared
progress message while they run.

This module is deliberately framework-agnostic (no Pyrogram imports) so
it can be unit-tested without a real Telegram client.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from utils.telegram.log import get_logger

logger = get_logger("mirror_leech.tasks")

TaskStatus = Literal[
    "queued", "downloading", "uploading", "done", "failed", "cancelled"
]

# Optional hook invoked (fire-and-forget) with the finished MLTask after it
# reaches a terminal state. The UI layer injects History.record here so this
# module stays framework- and DB-agnostic for unit tests.
_task_finished_hook: Optional[Callable[["MLTask"], Any]] = None


def set_task_finished_hook(fn: Optional[Callable[["MLTask"], Any]]) -> None:
    global _task_finished_hook
    _task_finished_hook = fn


def _fire_finished_hook(task: "MLTask") -> None:
    if _task_finished_hook is None:
        return
    try:
        res = _task_finished_hook(task)
        if hasattr(res, "__await__"):
            asyncio.ensure_future(res)
    except Exception:
        logger.debug("task_finished_hook raised", exc_info=True)


@dataclass
class UploadResult:
    """Outcome of a single uploader running on a completed download."""

    uploader_id: str
    ok: bool
    url: Optional[str] = None
    message: str = ""


@dataclass
class MLContext:
    """Execution handle handed to downloader / uploader implementations.

    Carries the task's cancel event, progress reporter, temp-dir path, and
    user/session info the provider needs. Implementations should call
    `ctx.progress(done, total)` frequently while streaming bytes.
    """

    task_id: str
    user_id: int
    source: str
    temp_dir: Path
    cancel_event: asyncio.Event
    report_progress: Callable[[float, float, float], None]  # (done_bytes, total_bytes, speed_bps)
    report_status: Callable[[TaskStatus], None]
    source_kind: Optional[str] = None

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def progress(self, done: float, total: float, speed_bps: float = 0.0) -> None:
        self.report_progress(done, total, speed_bps)

    def status(self, new_status: TaskStatus) -> None:
        self.report_status(new_status)

    def template_vars(self, local_path: Optional[Path] = None) -> dict:
        """Return the variable dict for folder-template rendering.

        Bundles per-task metadata (user / task / source kind) with the
        time-dependent snapshot and, when `local_path` is provided, the
        filename parts. Keeps uploaders from having to repeat the
        bookkeeping every time they resolve a destination path.
        """
        from tools.mirror_leech.TemplateEngine import now_vars

        vars: dict = {
            "user_id": self.user_id,
            "task_id": self.task_id,
            "source_kind": self.source_kind or "",
        }
        vars.update(now_vars())
        if local_path is not None:
            vars["original_name"] = local_path.stem
            vars["ext"] = local_path.suffix.lstrip(".").lower()
        return vars

    def resolve_path(self, template: str, local_path: Optional[Path] = None) -> str:
        """Render `template` with this context's variable set. Shortcut
        for `render_template(template, ctx.template_vars(...))`."""
        from tools.mirror_leech.TemplateEngine import render_template

        return render_template(template, self.template_vars(local_path))


@dataclass
class MLTask:
    """One Mirror-Leech job."""

    id: str
    user_id: int
    source: str
    downloader_id: str
    uploader_ids: list[str]

    status: TaskStatus = "queued"
    progress_fraction: float = 0.0
    speed_bps: float = 0.0
    eta_sec: int = 0

    message_chat_id: Optional[int] = None
    message_id: Optional[int] = None

    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    error: Optional[str] = None
    results: list[UploadResult] = field(default_factory=list)

    @classmethod
    def new(
        cls,
        user_id: int,
        source: str,
        downloader_id: str,
        uploader_ids: list[str],
    ) -> "MLTask":
        return cls(
            id=uuid.uuid4().hex[:12],
            user_id=user_id,
            source=source,
            downloader_id=downloader_id,
            uploader_ids=list(uploader_ids),
        )


class MLWorkerPool:
    """In-memory task registry + concurrency-bounded runner.

    Production use will wrap `enqueue` from the callback handlers. The
    pool does not persist tasks across restarts (by design — bots crash
    mid-transfer all the time and users just retry).
    """

    def __init__(
        self,
        *,
        max_concurrent_per_user: int = 2,
        max_global_concurrent: int = 10,
    ) -> None:
        self.max_concurrent_per_user = max_concurrent_per_user
        self.max_global_concurrent = max_global_concurrent
        # Lazy-initialized on first enqueue inside the running event loop.
        # Creating Semaphores at module-import time would bind them to a
        # loop that is not the one Pyrogram later runs on, silently stalling
        # every `async with sem` in production.
        self._global_sem: asyncio.Semaphore | None = None
        self._user_sems: dict[int, asyncio.Semaphore] = {}
        self._tasks: dict[str, MLTask] = {}
        self._running: dict[str, asyncio.Task] = {}

    def _ensure_global_sem(self) -> asyncio.Semaphore:
        if self._global_sem is None:
            self._global_sem = asyncio.Semaphore(self.max_global_concurrent)
        return self._global_sem

    # ----- lookup ---------------------------------------------------------

    def get(self, task_id: str) -> Optional[MLTask]:
        return self._tasks.get(task_id)

    def list_for_user(self, user_id: int) -> list[MLTask]:
        return sorted(
            (t for t in self._tasks.values() if t.user_id == user_id),
            key=lambda t: t.created_at,
            reverse=True,
        )

    def active_count(self, user_id: int) -> int:
        return sum(
            1
            for t in self._tasks.values()
            if t.user_id == user_id and t.status in ("queued", "downloading", "uploading")
        )

    # ----- execution ------------------------------------------------------

    def _user_sem(self, user_id: int) -> asyncio.Semaphore:
        sem = self._user_sems.get(user_id)
        if sem is None:
            sem = asyncio.Semaphore(self.max_concurrent_per_user)
            self._user_sems[user_id] = sem
        return sem

    def enqueue(
        self,
        task: MLTask,
        runner: Callable[[MLTask], Any],
    ) -> None:
        """Register `task` and start an asyncio.Task that awaits the
        semaphores then invokes `runner(task)`. `runner` is an async
        callable supplied by the controller (dependency injection keeps
        this module framework-agnostic)."""
        self._tasks[task.id] = task
        logger.info(
            "Enqueue MLTask %s: user=%s downloader=%s uploaders=%s src=%s",
            task.id, task.user_id, task.downloader_id,
            task.uploader_ids, task.source[:80],
        )

        global_sem = self._ensure_global_sem()
        user_sem = self._user_sem(task.user_id)

        async def _wrapped():
            try:
                async with global_sem, user_sem:
                    task.started_at = time.time()
                    logger.info("MLTask %s acquired slots, running", task.id)
                    try:
                        await runner(task)
                        if task.status not in ("failed", "cancelled"):
                            task.status = "done"
                    except asyncio.CancelledError:
                        task.status = "cancelled"
                        raise
                    except Exception as exc:
                        logger.exception("MLTask %s failed", task.id)
                        task.status = "failed"
                        task.error = str(exc)
                    finally:
                        task.finished_at = time.time()
                        self._running.pop(task.id, None)
                        _fire_finished_hook(task)
            except asyncio.CancelledError:
                task.status = "cancelled"
                task.finished_at = time.time()
                self._running.pop(task.id, None)
                raise
            except Exception as exc:
                # Defense in depth: catch setup errors (semaphore acquisition,
                # etc.) that would otherwise crash the asyncio Task silently.
                logger.exception(
                    "MLTask %s crashed before runner: %s", task.id, exc
                )
                task.status = "failed"
                task.error = f"pipeline setup failed: {exc}"
                task.finished_at = time.time()
                self._running.pop(task.id, None)
                _fire_finished_hook(task)

        at = asyncio.create_task(_wrapped())

        def _log_done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error(
                    "MLTask %s asyncio-level exception: %r", task.id, exc
                )

        at.add_done_callback(_log_done)
        self._running[task.id] = at

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        task.cancel_event.set()
        running = self._running.get(task_id)
        if running and not running.done():
            running.cancel()
        return True

    async def shutdown(self) -> None:
        """Cancel all running tasks — called from main.py on shutdown."""
        for task_id in list(self._running.keys()):
            self.cancel(task_id)
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)


# Singleton instance imported by the rest of the subsystem.
ml_worker_pool = MLWorkerPool()
