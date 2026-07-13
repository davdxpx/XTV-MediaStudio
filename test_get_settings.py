import asyncio
from config import Config
from db.core import Database
from unittest.mock import AsyncMock

async def main():
    Config.PUBLIC_MODE = False
    db = Database()

    # We want to see how get_settings behaves without PUBLIC_MODE
    db.settings = AsyncMock()

    await db.get_settings(user_id=12345)

    print(db.settings.find_one.call_args_list)

asyncio.run(main())
