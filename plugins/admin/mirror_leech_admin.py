# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# Telegram Channel @XTVbots
# Developed for the 𝕏TV Network @XTVglobal
# Backup Channel @XTVhome
# Contact on Telegram @davdxpx
# --------------------------------------------------------------------------
"""Admin-panel Mirror-Leech configuration.

Root screen shows master toggle + SECRETS_KEY state. Onboarding copy
appears only when the feature isn't ready yet; once configured the
screen collapses into a concise blockquote-style summary with a drill-
down into the provider availability list.
"""

from __future__ import annotations

import contextlib

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from db import db
from plugins.admin.core import is_admin
from tools.mirror_leech import Secrets
from tools.mirror_leech.downloaders import all_downloaders
from tools.mirror_leech.uploaders import all_uploaders
from utils.telegram.log import get_logger

logger = get_logger("plugins.admin.mirror_leech")


async def _feature_enabled() -> bool:
    toggles = await db.get_feature_toggles() or {}
    return bool(toggles.get("mirror_leech", False))


def _provider_counts() -> tuple[int, int, int, int]:
    """Return (downloaders_ready, downloaders_off, uploaders_ready, uploaders_off)."""
    dl_ready = dl_off = 0
    for cls in all_downloaders():
        avail = cls.available() if hasattr(cls, "available") else True
        if avail:
            dl_ready += 1
        else:
            dl_off += 1
    up_ready = up_off = 0
    for cls in all_uploaders():
        if cls.available():
            up_ready += 1
        else:
            up_off += 1
    return dl_ready, dl_off, up_ready, up_off


def _render_text_ready(enabled: bool) -> str:
    dl_ready, dl_off, up_ready, up_off = _provider_counts()
    return (
        "☁️ **Mirror-Leech Config**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"**Feature toggle:** {'✅ enabled' if enabled else '⏸ disabled'}\n"
        "**SECRETS_KEY:** ✅ set\n\n"
        f"> 📥 {dl_ready} downloaders ready"
        + (f" · {dl_off} off\n" if dl_off else "\n")
        + f"> 📤 {up_ready} uploaders ready"
        + (f" · {up_off} off\n" if up_off else "\n")
        + "> 🔐 Credentials encrypted at rest\n"
        + "> 🧩 Fused with MyFiles single + multi-select"
    )


# Precise SECRETS_KEY state lines — "missing" used to be shown for ALL
# failure modes, sending admins hunting for an env var they had already
# set when the real problem was a quoted/invalid key or a missing
# `cryptography` package.
_SECRETS_STATE_LINES = {
    "ok": "✅ set",
    "missing": "❌ missing — env var `SECRETS_KEY` is not set",
    "invalid": (
        "⚠️ set but INVALID — not a Fernet key.\n"
        "  ↳ Regenerate below, or check for typos/quotes in the env value."
    ),
    "no-crypto": (
        "⚠️ set, but the `cryptography` package is not installed.\n"
        "  ↳ `pip install cryptography` on the host, then restart."
    ),
}


def _render_text_onboarding(enabled: bool, secrets_state: str) -> str:
    kb = _SECRETS_STATE_LINES.get(secrets_state, "❌ missing")
    toggle = "✅ enabled" if enabled else "❌ disabled"
    return (
        "☁️ **Mirror-Leech Config**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"**Feature toggle:** {toggle}\n"
        f"**SECRETS_KEY:** {kb}\n\n"
        "What this unlocks:\n"
        "> 🔗 Mirror any HTTP(S) / yt-dlp / RSS / Telegram file\n"
        "> ☁️ Fan out to GDrive · rclone · MEGA · GoFile · Pixeldrain · Telegram · DDL · Dropbox · OneDrive · Box · S3 · B2 · WebDAV · Seafile\n"
        "> 🧩 “☁️ Mirror-Leech Options” on every MyFiles entry\n"
        "> 📊 `/mlqueue` to track + cancel running jobs\n\n"
        "**Setup steps**\n"
        "1. Tap the **🎲 Generate SECRETS_KEY** button below.\n"
        "2. Add the printed key to the bot's env and restart.\n"
        "3. Come back and tap **✅ Enable Mirror-Leech**."
    )


def _render_keyboard(enabled: bool, secrets_ok: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if not secrets_ok:
        rows.append(
            [
                InlineKeyboardButton(
                    "🎲 Generate SECRETS_KEY", callback_data="ml_admin_gen_secrets"
                )
            ]
        )

    if enabled:
        rows.append(
            [InlineKeyboardButton("🚫 Disable Mirror-Leech", callback_data="ml_admin_toggle")]
        )
    elif secrets_ok:
        rows.append(
            [InlineKeyboardButton("✅ Enable Mirror-Leech", callback_data="ml_admin_toggle")]
        )
    else:
        rows.append(
            [InlineKeyboardButton("🔒 Needs SECRETS_KEY", callback_data="ml_admin")]
        )

    rows.append(
        [InlineKeyboardButton("📦 Show providers", callback_data="ml_admin_providers")]
    )
    rows.append([InlineKeyboardButton("↻ Refresh", callback_data="ml_admin")])
    rows.append([InlineKeyboardButton("← Back", callback_data="admin_system_health")])
    return InlineKeyboardMarkup(rows)


async def _render_root(callback_query: CallbackQuery) -> None:
    enabled = await _feature_enabled()
    secrets_state = Secrets.diagnose()
    secrets_ok = secrets_state == "ok"
    text = (
        _render_text_ready(enabled)
        if secrets_ok and enabled
        else _render_text_onboarding(enabled, secrets_state)
    )
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            text, reply_markup=_render_keyboard(enabled, secrets_ok)
        )


async def _render_providers(callback_query: CallbackQuery) -> None:
    lines = ["📦 **Providers**", "━━━━━━━━━━━━━━━━━━━━", "", "**Downloaders**"]
    for cls in all_downloaders():
        avail = cls.available() if hasattr(cls, "available") else True
        marker = "✅" if avail else "🚫"
        lines.append(f"{marker} `{cls.id}` — {cls.display_name}")
    lines.append("")
    lines.append("**Uploaders**")
    for cls in all_uploaders():
        marker = "✅" if cls.available() else "🚫"
        binary_hint = (
            f" (needs `{cls.binary_required}`)"
            if cls.binary_required and not cls.available()
            else ""
        )
        pkg_hint = (
            f" (needs Python `{cls.python_import_required}`)"
            if cls.python_import_required and not cls.available()
            else ""
        )
        lines.append(
            f"{marker} `{cls.id}` — {cls.display_name}{binary_hint}{pkg_hint}"
        )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("← Back to Mirror-Leech", callback_data="ml_admin")]]
    )
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text("\n".join(lines), reply_markup=keyboard)


@Client.on_callback_query(filters.regex(r"^ml_admin$"))
async def ml_admin_callback(client: Client, callback_query: CallbackQuery) -> None:
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("Not authorised.", show_alert=True)
        return
    await _render_root(callback_query)
    with contextlib.suppress(Exception):
        await callback_query.answer()


@Client.on_callback_query(filters.regex(r"^ml_admin_providers$"))
async def ml_admin_providers(client: Client, callback_query: CallbackQuery) -> None:
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("Not authorised.", show_alert=True)
        return
    await _render_providers(callback_query)
    with contextlib.suppress(Exception):
        await callback_query.answer()


@Client.on_callback_query(filters.regex(r"^ml_admin_gen_secrets$"))
async def ml_admin_generate_secrets(
    client: Client, callback_query: CallbackQuery
) -> None:
    """Generate a fresh Fernet key in one tap and walk the operator through
    installing it. The bot CANNOT write env vars at runtime on hosted
    platforms, so the flow is: generate → show → operator pastes into
    env / Render-secret-vars / fly secrets → restart."""
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("Not authorised.", show_alert=True)
        return

    if Secrets.is_available():
        await callback_query.answer(
            "SECRETS_KEY is already set — generating a new one would invalidate "
            "every stored credential.",
            show_alert=True,
        )
        return

    try:
        new_key = Secrets.generate_key()
    except Exception as exc:
        logger.exception("Fernet key generation failed")
        await callback_query.answer(f"Couldn't generate a key: {exc}", show_alert=True)
        return

    instructions = (
        "🔐 **Your new SECRETS_KEY**\n\n"
        f"`{new_key}`\n\n"
        "**Install it** (pick one that matches your host):\n"
        "> • `.env` file: add `SECRETS_KEY=<paste>`\n"
        "> • Render / Railway / Koyeb / Zeabur: add it under Env Vars\n"
        "> • Heroku: `heroku config:set SECRETS_KEY=<paste>`\n"
        "> • Fly.io: `fly secrets set SECRETS_KEY=<paste>`\n"
        "> • Docker: rebuild / re-run with `-e SECRETS_KEY=<paste>`\n\n"
        "Then restart the bot and come back here to enable Mirror-Leech.\n\n"
        "⚠️ **Back this key up.** Losing it means every user has to re-link "
        "their providers."
    )

    try:
        await callback_query.message.reply_text(instructions)
    except Exception as exc:
        logger.warning("Could not post generated key: %s", exc)
    await callback_query.answer(
        "Key generated — copy it from the message below.", show_alert=True
    )


@Client.on_callback_query(filters.regex(r"^ml_admin_toggle$"))
async def ml_admin_toggle(client: Client, callback_query: CallbackQuery) -> None:
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("Not authorised.", show_alert=True)
        return

    current = await _feature_enabled()
    if not current and not Secrets.is_available():
        await callback_query.answer(
            "Set SECRETS_KEY on the host before enabling the feature.",
            show_alert=True,
        )
        return

    await db.update_feature_toggle("mirror_leech", not current)
    await callback_query.answer(
        "Enabled." if not current else "Disabled."
    )
    await _render_root(callback_query)
