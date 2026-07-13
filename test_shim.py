import asyncio
from config import Config
from db import schema
from db.shim import SettingsCollectionShim
from db.core import Database
from unittest.mock import AsyncMock, patch

async def main():
    Config.PUBLIC_MODE = False
    Config.CEO_ID = 12345

    db = Database()
    db.settings = AsyncMock()
    db.users = AsyncMock()

    shim = SettingsCollectionShim(db.settings, db.users, public_mode=False, ceo_id=12345)

    await shim.update_one(
        {"_id": "user_12345"},
        {"$set": {"is_bot_setup_complete": True}}
    )

    print("CEO Users Call args:")
    for call in db.users.update_one.call_args_list:
        print(call)

asyncio.run(main())
