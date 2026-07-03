# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""Consolidate non-public per-user settings docs into global_settings.

Background
----------
Before the MediaStudio layout migration, ``Database._get_doc_id()`` in
non-public mode effectively ignored ``user_id`` and always addressed the
global settings document. The layout migration accidentally dropped that
special-case: if a caller passed ``user_id`` in non-public mode, writes
suddenly went to a ``user_<id>`` doc and reads split depending on who
asked.

Observed symptom
----------------
A single-tenant private bot had its dumb-channel list saved under
``user_<CEO_ID>`` (because the rename flow calls ``get_dumb_channels``
with ``user_id``), while ``/admin → Dumb Channels`` reads
``global_settings`` and therefore showed an empty list.

Fix
---
``_get_doc_id`` now forces ``global_settings`` in non-public mode. This
migration rescues any pre-existing per-user docs by deep-merging their
content into ``global_settings`` and then removing the per-user doc, so
both flows converge on the same source of truth.

Idempotent. Safe to call every boot. No-op when:
- ``PUBLIC_MODE=True`` (per-user docs are intentional there)
- no ``user_*`` docs exist
"""

from __future__ import annotations

import contextlib
from typing import Any

from config import Config
from utils.telegram.log import get_logger

logger = get_logger("migrations.consolidate_nonpublic_settings")


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Merge ``overlay`` into ``base`` in place without overwriting keys
    that already exist in ``base``. Nested dicts are merged recursively.
    Non-dict values in ``base`` win over overlay — we preferentially keep
    whatever's already in the global doc."""
    for key, value in overlay.items():
        if key == "_id":
            continue
        if key not in base:
            base[key] = value
            continue
        if isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
    return base


async def run_consolidate_nonpublic_settings(db: Any) -> int:
    """Merge ``user_*`` settings docs into ``global_settings`` in non-public mode.

    Returns the number of user docs that were consumed.
    """
    if Config.PUBLIC_MODE:
        logger.info("PUBLIC_MODE=True — skipping non-public consolidation.")
        return 0

    if db.settings is None:
        logger.warning("Settings collection unavailable; skipping consolidation.")
        return 0

    try:
        # Enumerate the RAW settings collection, not the shim. The shim's
        # `.find()` happens to pass through today, but being explicit keeps
        # this migration honest: it only ever consumes stale `user_<id>`
        # documents that physically live in MediaStudio-Settings.
        cursor = db.settings.real.find({"_id": {"$regex": r"^user_\d+$"}})
    except Exception as e:
        logger.warning(f"Could not enumerate user_* docs: {e}")
        return 0

    user_docs = []
    async for doc in cursor:
        user_docs.append(doc)

    if not user_docs:
        logger.info("No user_* settings docs found — nothing to consolidate.")
        return 0

    # Read the current global doc (or start fresh).
    global_doc = await db.settings.find_one({"_id": "global_settings"}) or {}
    merged_any = False

    for doc in user_docs:
        doc_id = doc.get("_id")
        logger.info(
            f"Consolidating {doc_id} → global_settings "
            f"(keys: {sorted(k for k in doc if k != '_id')})"
        )
        before_keys = set(global_doc.keys())
        _deep_merge(global_doc, doc)
        after_keys = set(global_doc.keys())
        added = after_keys - before_keys
        if added:
            merged_any = True
            logger.info(f"  added to global: {sorted(added)}")

    if merged_any:
        # Write the merged result back to global_settings.
        global_doc["_id"] = "global_settings"
        try:
            await db.settings.update_one(
                {"_id": "global_settings"},
                {"$set": {k: v for k, v in global_doc.items() if k != "_id"}},
                upsert=True,
            )
            logger.info("global_settings updated with merged user data.")
        except Exception as e:
            logger.error(f"Failed to write merged global_settings: {e}")
            return 0

    # Remove consumed per-user docs so they don't resurface if callers
    # somehow bypass `_get_doc_id`.
    #
    # CRITICAL: delete via the RAW collection. Routing this delete through
    # the shim rewrote `{"_id": "user_<uid>"}` into an `$unset` of
    # `MediaStudio-users.<uid>.personal_settings` — i.e. it wiped the
    # user's LIVE settings (dumb channels, thumbnails, templates,
    # setup_completed) while leaving the stale trigger doc in
    # MediaStudio-Settings untouched. Because this migration runs on every
    # boot, that combination erased the bot's configuration on every
    # restart and forced the setup wizard to rerun.
    removed = 0
    for doc in user_docs:
        doc_id = doc.get("_id")
        try:
            await db.settings.real.delete_one({"_id": doc_id})
            removed += 1
        except Exception as e:
            logger.warning(f"Could not remove {doc_id}: {e}")

    # Invalidate any in-memory cache so the next reader sees the merged doc.
    with contextlib.suppress(Exception):
        db._invalidate_settings_cache()

    logger.info(
        f"Non-public consolidation done: merged {len(user_docs)} user doc(s), "
        f"removed {removed}."
    )
    return removed


# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# Telegram Channel @XTVbots
# Developed for the 𝕏TV Network @XTVglobal
# Backup Channel @XTVhome
# Contact on Telegram @davdxpx
# --------------------------------------------------------------------------
