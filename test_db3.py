import asyncio
from unittest.mock import AsyncMock, patch
from db.core import Database
from db.shim import SettingsCollectionShim
from config import Config

async def main():
    Config.PUBLIC_MODE = False
    Config.CEO_ID = 12345
    db = Database()
    settings_coll = AsyncMock()
    users_coll = AsyncMock()

    settings_coll.find_one.return_value = {"_id": "global_settings", "is_bot_setup_complete": False}
    users_coll.find_one.return_value = {
        "user_id": 12345,
        "personal_settings": {
            "is_bot_setup_complete": True,
            "dumb_channels": {"-100": "test"}
        }
    }
    users_coll.update_one.return_value = None

    db.settings = SettingsCollectionShim(settings_coll, users_coll, public_mode=False, ceo_id=12345)
    db.users = users_coll

    res = await db.get_setting("is_bot_setup_complete", user_id=12345)
    print("Settings is_bot_setup_complete:", res)

asyncio.run(main())
