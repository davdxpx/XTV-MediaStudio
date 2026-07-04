"""Telegram uploader — re-uploads the file back into Telegram.

Target destination is configurable per-user (DM by default, or a channel
id the user saved). When the file is bigger than 2 GB the upload is
delegated to the XTV Pro userbot (same tunnel path that /start already
uses for large uploads).
"""

from __future__ import annotations

from pathlib import Path

from tools.mirror_leech import Accounts
from tools.mirror_leech.Tasks import MLContext, UploadResult
from tools.mirror_leech.uploaders import Uploader, register_uploader
from utils.telegram.log import get_logger

logger = get_logger("mirror_leech.telegram_uploader")

_TG_BOT_LIMIT = 2 * 1024 * 1024 * 1024  # 2 GB


@register_uploader
class TelegramUploader(Uploader):
    id = "telegram"
    display_name = "Telegram (DM / channel)"
    needs_credentials = False

    async def is_configured(self, user_id: int) -> bool:
        return True  # falls back to sending to the user's DM

    async def test_connection(self, user_id: int) -> tuple[bool, str]:
        return True, "Telegram upload uses the bot's session — always ready."

    async def upload(self, ctx: MLContext, local_path: Path) -> UploadResult:
        client = getattr(ctx, "client", None)
        if client is None:
            return UploadResult(self.id, ok=False, message="Bot client unavailable")

        ctx.status("uploading")
        account = await Accounts.get_account(ctx.user_id, self.id)
        dest = account.get("destination") or "dm"
        chat_id = ctx.user_id if dest == "dm" else int(dest)

        size = local_path.stat().st_size

        # Pick bot vs userbot based on file size. `app.user_bot` is attached
        # in main.py when the XTV Pro session is active.
        uploader = client
        if size > _TG_BOT_LIMIT and getattr(client, "user_bot", None) is not None:
            uploader = client.user_bot

        sent = await uploader.send_document(
            chat_id,
            str(local_path),
            caption=local_path.name,
        )

        # Archive the result into MyFiles so mirrored files live in the
        # library like every other output. Only possible for bot-client
        # uploads — the bot can't copy messages the Pro userbot sent in
        # a chat it isn't part of. Best-effort: failures never break the
        # mirror task itself.
        if uploader is client:
            try:
                from utils.myfiles.store import save_message_to_myfiles

                await save_message_to_myfiles(
                    client,
                    ctx.user_id,
                    sent,
                    file_name=local_path.name,
                    tool_name="mirror_leech",
                )
            except Exception as e:
                logger.debug(f"MyFiles archive of mirrored file failed: {e}")

        link = None
        try:
            if getattr(sent.chat, "username", None):
                link = f"https://t.me/{sent.chat.username}/{sent.id}"
        except Exception:
            pass
        return UploadResult(self.id, ok=True, url=link or "sent to chat")
