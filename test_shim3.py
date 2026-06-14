import asyncio
from config import Config
from db import schema
from db.shim import SettingsCollectionShim
from db.core import Database
from unittest.mock import AsyncMock

async def main():
    Config.PUBLIC_MODE = False
    Config.CEO_ID = 12345

    db = Database()
    db.settings = AsyncMock()
    db.users = AsyncMock()

    # Mock some data returning from users collection
    db.users.find_one.return_value = {
        "user_id": 12345,
        "personal_settings": {
            "is_bot_setup_complete": True,
            "dumb_channels": {"-100123": "Test"}
        }
    }

    shim = SettingsCollectionShim(db.settings, db.users, public_mode=False, ceo_id=12345)

    doc = await shim.find_one({"_id": "global_settings"})
    print(f"is_bot_setup_complete in merged doc: {doc.get('is_bot_setup_complete')}")
    print(f"dumb_channels in merged doc: {doc.get('dumb_channels')}")

asyncio.run(main())
