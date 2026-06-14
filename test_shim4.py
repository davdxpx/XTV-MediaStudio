import asyncio
from config import Config
from db import schema
from db.shim import SettingsCollectionShim
from db.core import Database

class FakeColl:
    def __init__(self, data):
        self.data = data
    async def find_one(self, filter_):
        for d in self.data:
            match = True
            for k, v in filter_.items():
                if d.get(k) != v:
                    match = False
            if match:
                return d
        return None

async def main():
    Config.PUBLIC_MODE = False
    Config.CEO_ID = 12345

    settings_data = [
        {"_id": "dumb_channels_global", "dumb_channel_timeout": 3600}
    ]
    users_data = [
        {"user_id": 12345, "personal_settings": {"is_bot_setup_complete": True, "dumb_channels": {"-100": "test"}}}
    ]

    settings = FakeColl(settings_data)
    users = FakeColl(users_data)

    shim = SettingsCollectionShim(settings, users, public_mode=False, ceo_id=12345)

    doc = await shim.find_one({"_id": "global_settings"})
    print("Merged global doc:")
    print(doc.get("is_bot_setup_complete"))
    print(doc.get("dumb_channels"))

asyncio.run(main())
