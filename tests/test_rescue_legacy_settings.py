"""Regression tests for rescue_legacy_settings: live data must always win.

The original drain wrote each legacy top-level key with a whole-value
``$set``. For dict-valued keys (``templates``, ``filename_templates``,
``dumb_channels``, …) that CLOBBERED the live dict — e.g. a freshly
saved ``templates.caption`` was overwritten by the stale legacy
``templates`` dict that didn't contain the key. These tests pin the
merge semantics: fill gaps only, never replace live values.
"""

import types

import pytest
from mongomock_motor import AsyncMongoMockClient

from db import schema
from db.migrations.rescue_legacy_settings import run_rescue_legacy_settings
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


async def test_stale_templates_dict_does_not_clobber_live_caption(fake_db):
    db_obj, settings_coll, users_coll = fake_db

    # Live state: the user just saved a caption template.
    await users_coll.insert_one(
        {
            "user_id": CEO_ID,
            "personal_settings": {
                "templates": {"caption": "{Title} — {Size}", "title": "LIVE"},
            },
        }
    )
    # Stale raw global_settings doc from a pre-shim deployment: carries an
    # OLD templates dict without caption, plus a key the live view lacks.
    await settings_coll.insert_one(
        {
            "_id": "global_settings",
            "templates": {"title": "STALE", "author": "@old"},
            "preferred_separator": "_",
        }
    )

    result = await run_rescue_legacy_settings(db_obj)
    assert result["status"] == "completed"

    ceo = await users_coll.find_one({"user_id": CEO_ID})
    templates = (ceo.get("personal_settings") or {}).get("templates") or {}
    # Live values survive:
    assert templates.get("caption") == "{Title} — {Size}"
    assert templates.get("title") == "LIVE"
    # Gap-filling still works for subkeys the live dict lacked:
    assert templates.get("author") == "@old"

    # Scalar keys missing live are rescued too.
    merged = await db_obj.settings.find_one({"_id": "global_settings"})
    assert merged.get("preferred_separator") == "_"

    # The stale raw doc is gone.
    assert await settings_coll.find_one({"_id": "global_settings"}) is None or (
        await settings_coll.find_one({"_id": "global_settings"})
    ).get("templates") is None


async def test_live_scalar_wins_over_stale_scalar(fake_db):
    db_obj, settings_coll, users_coll = fake_db
    await users_coll.insert_one(
        {
            "user_id": CEO_ID,
            "personal_settings": {"preferred_separator": ".", "setup_completed": True},
        }
    )
    await settings_coll.insert_one(
        {
            "_id": "global_settings",
            "preferred_separator": "_",
            "setup_completed": False,
        }
    )

    await run_rescue_legacy_settings(db_obj)

    ceo = await users_coll.find_one({"user_id": CEO_ID})
    personal = ceo.get("personal_settings") or {}
    assert personal.get("preferred_separator") == "."
    assert personal.get("setup_completed") is True


async def test_rescue_runs_only_once(fake_db):
    db_obj, settings_coll, _ = fake_db
    await settings_coll.insert_one(
        {"_id": "global_settings", "preferred_separator": "_"}
    )

    first = await run_rescue_legacy_settings(db_obj)
    assert first["status"] == "completed"
    second = await run_rescue_legacy_settings(db_obj)
    assert second["status"] == "already_done"
