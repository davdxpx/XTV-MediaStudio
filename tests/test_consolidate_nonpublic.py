"""Regression tests for the consolidate_nonpublic_settings migration.

The critical property: the migration must NEVER wipe a user's live
``MediaStudio-users.<uid>.personal_settings``. The pre-fix version routed
its cleanup ``delete_one({"_id": "user_<uid>"})`` through the settings
shim, which rewrote it into an ``$unset`` of the CEO's personal_settings —
erasing dumb channels, thumbnails, templates, and ``setup_completed`` on
every boot while leaving the stale trigger doc in place. That is exactly
the "bot loses its configuration on every restart" bug.
"""

import types

import pytest
from mongomock_motor import AsyncMongoMockClient

from db import schema
from db.migrations.consolidate_nonpublic_settings import (
    run_consolidate_nonpublic_settings,
)
from db.shim import SettingsCollectionShim

CEO_ID = 1  # matches conftest's CEO_ID env default


@pytest.fixture
async def fake_db():
    client = AsyncMongoMockClient()
    mongo_db = client["test-maindb"]
    settings_coll = mongo_db[schema.SETTINGS_COLLECTION]
    users_coll = mongo_db[schema.USERS_COLLECTION]

    shim = SettingsCollectionShim(
        settings_coll, users_coll, ceo_id=CEO_ID, public_mode=False
    )

    db_obj = types.SimpleNamespace(
        settings=shim,
        users=users_coll,
        db=mongo_db,
        _invalidate_settings_cache=lambda user_id=None: None,
    )
    return db_obj, settings_coll, users_coll


async def _seed_live_state(users_coll):
    """The CEO's live configuration — must survive the migration."""
    await users_coll.insert_one(
        {
            "user_id": CEO_ID,
            "personal_settings": {
                "dumb_channels": {"-100123": "MoviesHub"},
                "thumbnail_mode": "auto",
                "thumbnail_file_id": "THUMB",
                "setup_completed": True,
                "templates": {"title": "{title}"},
            },
        }
    )


async def test_stale_user_doc_is_consumed_without_wiping_live_settings(fake_db):
    db_obj, settings_coll, users_coll = fake_db
    await _seed_live_state(users_coll)

    # Stale legacy doc physically inside MediaStudio-Settings — the shape
    # that used to trigger the every-boot wipe.
    await settings_coll.insert_one(
        {"_id": f"user_{CEO_ID}", "preferred_separator": "_", "legacy_only_key": 1}
    )

    removed = await run_consolidate_nonpublic_settings(db_obj)
    assert removed == 1

    # The stale raw doc is really gone from the settings collection...
    assert await settings_coll.find_one({"_id": f"user_{CEO_ID}"}) is None

    # ...and the CEO's live personal_settings are fully intact.
    ceo = await users_coll.find_one({"user_id": CEO_ID})
    personal = ceo.get("personal_settings") or {}
    assert personal.get("dumb_channels") == {"-100123": "MoviesHub"}
    assert personal.get("thumbnail_mode") == "auto"
    assert personal.get("thumbnail_file_id") == "THUMB"
    assert personal.get("setup_completed") is True
    assert personal.get("templates") == {"title": "{title}"}

    # Stale keys were merged in (they didn't exist on the live config).
    assert personal.get("preferred_separator") == "_"


async def test_second_boot_is_a_noop(fake_db):
    db_obj, settings_coll, users_coll = fake_db
    await _seed_live_state(users_coll)
    await settings_coll.insert_one({"_id": f"user_{CEO_ID}", "preferred_separator": "_"})

    assert await run_consolidate_nonpublic_settings(db_obj) == 1
    # The trigger doc is gone, so the next boot must consume nothing.
    assert await run_consolidate_nonpublic_settings(db_obj) == 0

    ceo = await users_coll.find_one({"user_id": CEO_ID})
    assert (ceo.get("personal_settings") or {}).get("dumb_channels") == {
        "-100123": "MoviesHub"
    }


async def test_live_values_win_over_stale_doc(fake_db):
    db_obj, settings_coll, users_coll = fake_db
    await _seed_live_state(users_coll)
    # Stale doc carries an OLD dumb_channels map — it must not clobber
    # the live one (deep-merge keeps existing keys).
    await settings_coll.insert_one(
        {
            "_id": f"user_{CEO_ID}",
            "dumb_channels": {"-100123": "OldName"},
            "setup_completed": False,
        }
    )

    await run_consolidate_nonpublic_settings(db_obj)

    ceo = await users_coll.find_one({"user_id": CEO_ID})
    personal = ceo.get("personal_settings") or {}
    assert personal.get("dumb_channels", {}).get("-100123") == "MoviesHub"
    assert personal.get("setup_completed") is True


async def test_noop_when_no_stale_docs(fake_db):
    db_obj, _, users_coll = fake_db
    await _seed_live_state(users_coll)
    assert await run_consolidate_nonpublic_settings(db_obj) == 0
    ceo = await users_coll.find_one({"user_id": CEO_ID})
    assert (ceo.get("personal_settings") or {}).get("setup_completed") is True
