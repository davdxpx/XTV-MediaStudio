# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""Mirror-Leech finished-task history.

The in-memory worker pool forgets every task on restart, so users had no
record of what ran, what failed, or where their uploads went. Every task
that reaches a terminal state is now recorded here (via the
``set_task_finished_hook`` injection point in Tasks.py) and powers:

 * the 🗂 History screen (`ml_history`) — last runs with status + links
 * one-tap 🔁 Repeat — re-queue the same source → destinations

Docs are TTL-pruned after 30 days. All writes are best-effort: a Mongo
hiccup must never poison a finished task.
"""

from __future__ import annotations

import contextlib
import datetime
import time
from typing import Any, Optional

from utils.telegram.log import get_logger

logger = get_logger("mirror_leech.history")

_HISTORY_TTL_DAYS = 30
_MAX_SOURCE_LEN = 500
_indexes_ready = False


def build_history_doc(task: Any) -> dict:
    """Pure transform: MLTask → history document. Unit-tested."""
    duration = None
    if task.started_at and task.finished_at:
        duration = max(0.0, task.finished_at - task.started_at)
    return {
        "user_id": task.user_id,
        "task_id": task.id,
        "source": (task.source or "")[:_MAX_SOURCE_LEN],
        "downloader_id": task.downloader_id or "",
        "uploader_ids": list(task.uploader_ids or []),
        "status": task.status,
        "error": (task.error or "")[:300] or None,
        "duration_sec": duration,
        "results": [
            {"uploader": r.uploader_id, "ok": bool(r.ok), "url": r.url}
            for r in (task.results or [])
        ],
        "created_at": datetime.datetime.utcnow(),
    }


async def ensure_indexes() -> None:
    """Create the TTL + per-user indexes once per process. Best-effort."""
    global _indexes_ready
    if _indexes_ready:
        return
    from db import db

    if db is None or getattr(db, "ml_history", None) is None:
        return
    with contextlib.suppress(Exception):
        await db.ml_history.create_index(
            "created_at", expireAfterSeconds=_HISTORY_TTL_DAYS * 86400
        )
        await db.ml_history.create_index([("user_id", 1), ("created_at", -1)])
        _indexes_ready = True


async def record(task: Any) -> None:
    """Persist a finished MLTask. Registered as the pool's finished-hook."""
    from db import db

    if db is None or getattr(db, "ml_history", None) is None:
        return
    try:
        await ensure_indexes()
        await db.ml_history.insert_one(build_history_doc(task))
    except Exception as e:
        logger.debug(f"history record failed for {getattr(task, 'id', '?')}: {e}")


async def recent(user_id: int, limit: int = 15) -> list[dict]:
    from db import db

    if db is None or getattr(db, "ml_history", None) is None:
        return []
    try:
        cursor = (
            db.ml_history.find({"user_id": user_id})
            .sort("created_at", -1)
            .limit(limit)
        )
        return await cursor.to_list(length=limit)
    except Exception as e:
        logger.debug(f"history recent failed: {e}")
        return []


async def get(entry_id) -> Optional[dict]:
    from bson import ObjectId

    from db import db

    if db is None or getattr(db, "ml_history", None) is None:
        return None
    with contextlib.suppress(Exception):
        return await db.ml_history.find_one({"_id": ObjectId(str(entry_id))})
    return None


async def stats(user_id: int) -> dict:
    """Tiny aggregate for the hub screen: {total, done, failed, last_at}."""
    from db import db

    out = {"total": 0, "done": 0, "failed": 0, "last_at": None}
    if db is None or getattr(db, "ml_history", None) is None:
        return out
    try:
        pipeline = [
            {"$match": {"user_id": user_id}},
            {
                "$group": {
                    "_id": "$status",
                    "count": {"$sum": 1},
                    "last": {"$max": "$created_at"},
                }
            },
        ]
        async for row in db.ml_history.aggregate(pipeline):
            out["total"] += row["count"]
            if row["_id"] == "done":
                out["done"] += row["count"]
            elif row["_id"] == "failed":
                out["failed"] += row["count"]
            if row.get("last") and (
                out["last_at"] is None or row["last"] > out["last_at"]
            ):
                out["last_at"] = row["last"]
    except Exception as e:
        logger.debug(f"history stats failed: {e}")
    return out


def age_str(created_at) -> str:
    """Human 'x ago' for history rows."""
    try:
        delta = time.time() - created_at.timestamp()
    except Exception:
        return ""
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"
