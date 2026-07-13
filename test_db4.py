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
            "dumb_channels": {"-100": "test"}
        }
    }
    users_coll.update_one.return_value = None

    db.settings = SettingsCollectionShim(settings_coll, users_coll, public_mode=False, ceo_id=12345)
    db.users = users_coll

    await db.update_setting("is_bot_setup_complete", True, user_id=12345)
    print("Users coll args:", users_coll.update_one.call_args_list)

asyncio.run(main())
