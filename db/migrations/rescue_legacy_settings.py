# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""rescue_legacy_settings migration.

The ``mediastudio_layout`` migration walked the old ``user_settings``
collection and split it into the new layout. What it did NOT do is
touch data that was already sitting in the new ``MediaStudio-Settings``
collection at any of these legacy document ids:

 * ``_id: "global_settings"`` — pre-shim writes, or data that survived
   the first migration because it never lived in ``user_settings``.
   Result: ``thumbnail_file_id``, ``thumbnail_binary``, ``templates``,
   ``filename_templates``, ``dumb_channels``, ``default_dumb_channel``
   etc. parked in a document that the shim's ``find_one`` never read
   (``global_settings`` is a virtual id, not a real merged doc) — so
   the bot appeared to "lose" that data on every redeploy.

 * ``_id: "public_mode_config"`` — in non-public deployments this doc
   is meaningless (its keys belong under the branding / force-sub /
   payments per-concern docs) but it sometimes exists from an earlier
   mode switch or from copy-pasted boilerplate.

 * ``_id: "user_<uid>"`` — pre-consolidation user-scoped settings docs
   that the older ``consolidate_nonpublic_settings`` migration merged
   into ``global_settings`` (which then became invisible, see above)
   instead of onto the real user doc.

This migration drains each of those legacy docs key-by-key through the
shim's write path, then removes them. Because every key goes through
``db.settings.update_one``, the shim takes care of the routing: global
keys land in per-concern docs, ``PERSONAL_KEYS`` get siphoned to the
CEO's ``personal_settings`` (non-public mode) or the user's doc (public
mode), and ``USER_TOP_LEVEL_KEYS`` — notably ``usage`` — land top-level
on the user doc so quota queries see them.

Idempotent, advisory-locked, takes a timestamped backup of the entire
settings collection before touching anything. Safe to re-run.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from config import Config
from db import schema
from db.migrations.helpers import backup_collection, collection_exists
from utils.telegram.log import get_logger

logger = get_logger("migrations.rescue_legacy_settings")

MIGRATION_ID = "rescue_legacy_settings"
_LOCK_TTL_SECONDS = 30 * 60  # 30 minutes


async def _read_migration_doc(settings_coll):
    return await settings_coll.find_one({"_id": schema.DOC_SCHEMA_MIGRATIONS}) or {}


async def _is_already_completed(settings_coll) -> bool:
    doc = await _read_migration_doc(settings_coll)
    entry = doc.get(MIGRATION_ID) or {}
    return bool(entry.get("completed_at"))


async def _acquire_lock(settings_coll, *, now: float) -> bool:
    doc = await _read_migration_doc(settings_coll)
    entry = doc.get(MIGRATION_ID) or {}
    started_at = entry.get("started_at")
    completed_at = entry.get("completed_at")

    if completed_at:
        return False

    if started_at and now - float(started_at) < _LOCK_TTL_SECONDS:
        logger.warning(
            "rescue_legacy_settings held by another instance since %s; skipping",
            started_at,
        )
        return False

    await settings_coll.update_one(
        {"_id": schema.DOC_SCHEMA_MIGRATIONS},
        {"$set": {f"{MIGRATION_ID}.started_at": now, f"{MIGRATION_ID}.host": "bot"}},
        upsert=True,
    )
    return True


async def _mark_completed(settings_coll, *, stats: dict) -> None:
    await settings_coll.update_one(
        {"_id": schema.DOC_SCHEMA_MIGRATIONS},
        {
            "$set": {
                f"{MIGRATION_ID}.completed_at": time.time(),
                f"{MIGRATION_ID}.stats": stats,
            }
        },
        upsert=True,
    )


async def _drain_doc_through_shim(
    db: Any, raw_doc: dict, virtual_id: str
) -> int:
    """Send the fields of ``raw_doc`` through the shim targeting
    ``virtual_id``. The shim splits PERSONAL_KEYS, USER_TOP_LEVEL_KEYS,
    per-concern docs and legacy_misc correctly.

    LIVE DATA WINS. The legacy doc is by definition stale — it was
    invisible to the running bot — so it must only ever FILL GAPS:

      * key missing from the live view          → write it whole
      * both live and legacy values are dicts   → write only the legacy
        subkeys the live dict doesn't have (dotted paths, so e.g. a
        stale ``templates`` dict can no longer clobber a freshly saved
        ``templates.caption``)
      * key exists live with a scalar           → skip the legacy value

    Returns the number of top-level keys rescued (fully or partially).
    """
    payload = {k: v for k, v in raw_doc.items() if k != "_id"}
    if not payload:
        return 0

    current = await db.settings.find_one({"_id": virtual_id}) or {}

    set_fields: dict = {}
    rescued = 0
    for key, value in payload.items():
        live = current.get(key)
        if key not in current:
            set_fields[key] = value
            rescued += 1
        elif isinstance(live, dict) and isinstance(value, dict):
            added = False
            for sub_key, sub_val in value.items():
                if sub_key not in live:
                    set_fields[f"{key}.{sub_key}"] = sub_val
                    added = True
            if added:
                rescued += 1
        # else: live scalar wins — drop the stale legacy value.

    if set_fields:
        await db.settings.update_one(
            {"_id": virtual_id}, {"$set": set_fields}, upsert=True
        )
    return rescued


async def _rescue_virtual_doc(db: Any, doc_id: str) -> dict:
    """Drain one legacy virtual doc (``global_settings`` /
    ``public_mode_config``) from the real settings collection through the
    shim, then remove the now-empty raw document.
    """
    raw = await db.settings.real.find_one({"_id": doc_id})
    if not raw:
        return {"found": False, "keys": 0}

    # Remove the raw doc BEFORE draining. The virtual-doc merge includes
    # the raw ``global_settings`` doc as a fallback layer, so reading the
    # live state while the raw doc still exists would show the stale
    # values as "live" and the gap-filling merge would rescue nothing.
    # The full-collection backup taken by the caller makes this safe —
    # ``raw`` is held in memory for the drain below.
    with contextlib.suppress(Exception):
        await db.settings.real.delete_one({"_id": doc_id})

    rescued = await _drain_doc_through_shim(db, raw, doc_id)

    logger.info(
        "rescue_legacy_settings: drained %s (%d keys) and removed the raw doc",
        doc_id,
        rescued,
    )
    return {"found": True, "keys": rescued, "sample_keys": sorted(raw.keys())[:10]}


async def _rescue_user_docs(db: Any) -> dict:
    """Drain legacy ``_id: "user_<uid>"`` docs. These carry ``usage`` and
    other top-level fields that the shim now routes correctly thanks to
    USER_TOP_LEVEL_KEYS — so a single shim write lands quota data on the
    user doc top-level instead of nested under ``personal_settings``.
    """
    try:
        cursor = db.settings.real.find({"_id": {"$regex": r"^user_\d+$"}})
    except Exception as e:
        logger.warning("Could not enumerate user_* docs: %s", e)
        return {"users": 0, "keys": 0}

    user_docs = []
    async for doc in cursor:
        user_docs.append(doc)

    if not user_docs:
        return {"users": 0, "keys": 0}

    total_keys = 0
    for doc in user_docs:
        doc_id = doc.get("_id")
        rescued = await _drain_doc_through_shim(db, doc, doc_id)
        total_keys += rescued
        logger.info(
            "rescue_legacy_settings: drained %s (%d keys)", doc_id, rescued
        )

    # Remove the legacy docs in one sweep now that the data has been
    # siphoned onto MediaStudio-users.
    with contextlib.suppress(Exception):
        await db.settings.real.delete_many({"_id": {"$regex": r"^user_\d+$"}})

    return {"users": len(user_docs), "keys": total_keys}


async def run_rescue_legacy_settings(db: Any) -> dict:
    """Entry point. Idempotent.

    Takes a backup of the entire MediaStudio-Settings collection before
    touching anything so the change is fully reversible.
    """
    if db.settings is None or db.users is None:
        logger.warning("DB collections unavailable; skipping rescue.")
        return {"status": "skipped", "reason": "db-unavailable"}

    settings_coll = db.settings.real

    if await _is_already_completed(settings_coll):
        logger.info("rescue_legacy_settings migration already completed; skipping.")
        return {"status": "already_done"}

    now = time.time()
    if not await _acquire_lock(settings_coll, now=now):
        return {"status": "locked"}

    try:
        logger.info("rescue_legacy_settings migration starting")

        # Full settings backup under MediaStudio-Settings__backup_rescue_<ts>.
        # The helper handles collection-already-exists gracefully.
        backup = None
        if await collection_exists(db.db, schema.SETTINGS_COLLECTION):
            backup = await backup_collection(
                db.db,
                schema.SETTINGS_COLLECTION,
                backup_suffix=f"_backup_rescue_{int(now)}",
            )
            logger.info("rescue_legacy_settings: backup taken -> %s", backup)

        global_stats = await _rescue_virtual_doc(
            db, schema.VIRTUAL_GLOBAL_SETTINGS
        )
        public_stats = {"found": False, "keys": 0}
        if not Config.PUBLIC_MODE:
            # public_mode_config is meaningful in public deployments, so we
            # only drain it in non-public mode where its keys are orphans.
            public_stats = await _rescue_virtual_doc(
                db, schema.VIRTUAL_PUBLIC_MODE_CONFIG
            )

        user_stats = await _rescue_user_docs(db)

        # Invalidate the core.Database in-memory cache so the next read
        # reflects the new layout immediately.
        with contextlib.suppress(Exception):
            db._invalidate_settings_cache()

        stats = {
            "backup": backup,
            "global_settings": global_stats,
            "public_mode_config": public_stats,
            "user_docs": user_stats,
        }
        await _mark_completed(settings_coll, stats=stats)
        logger.info("rescue_legacy_settings migration completed: %s", stats)
        return {"status": "completed", **stats}
    except Exception:
        logger.exception(
            "rescue_legacy_settings migration FAILED — leaving lock in place"
        )
        raise
