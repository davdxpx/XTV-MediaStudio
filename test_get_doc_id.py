import asyncio
from config import Config
from db.core import Database
from unittest.mock import AsyncMock

async def main():
    Config.PUBLIC_MODE = False
    Config.CEO_ID = 12345
    db = Database()

    # We want to see how is_bot_setup_complete behaves without PUBLIC_MODE
    db.settings = AsyncMock()

    await db.update_setting("is_bot_setup_complete", True, user_id=12345)

    print(db.settings.update_one.call_args_list)

asyncio.run(main())
