import asyncio
from config import Config
from db import schema
from db.shim import SettingsCollectionShim
from db.core import Database

class FakeColl:
    def __init__(self, data):
        self.data = data
    async def update_one(self, filter_, update, **kwargs):
        pass

async def main():
    Config.PUBLIC_MODE = False
    Config.CEO_ID = 12345

    settings = FakeColl([])
    users = FakeColl([])

    db = Database()
    db.settings = SettingsCollectionShim(settings, users, public_mode=False, ceo_id=12345)

    # Try adding dumb channel
    await db.add_dumb_channel("-1001", "test", user_id=None)

    # Check cache
    print("Cache:", db._settings_cache)

asyncio.run(main())
