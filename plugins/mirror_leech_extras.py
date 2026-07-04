# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""Mirror-Leech extras: hub screen, task history, repeat, setup assistant.

Production-readiness layer on top of the core Mirror-Leech stack:

 * `/ml` without a link now opens a **hub** — usage, live queue count,
   history stats, and jump buttons — instead of a bare usage string.
 * 🗂 **History** (`ml_history`, `/mlhistory`): the last finished tasks
   survive restarts (MediaStudio-ml-history via the finished-task hook)
   with per-entry status, destination links and duration.
 * 🔁 **Repeat** (`ml_rep_<id>`): one tap re-queues a historical task —
   same source, same destinations.
 * 🚀 **Setup Assistant** (`ml_setup`): shows every uploader with its
   configured-state at a glance and deep-links into the existing
   per-provider config + guide screens. First-time setup stops being a
   treasure hunt.

Callback namespace: ``ml_hub``, ``ml_history``, ``ml_rep_*``,
``ml_setup`` — disjoint from every pattern listed in
plugins/mirror_leech_ui.py's grammar table.
"""

from __future__ import annotations

import contextlib

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from tools.mirror_leech import History
from tools.mirror_leech.Tasks import ml_worker_pool, set_task_finished_hook
from tools.mirror_leech.UIChrome import frame_plain as frame
from tools.mirror_leech.uploaders import available_uploaders
from utils.telegram.log import get_logger

logger = get_logger("plugins.mirror_leech_extras")

# Persist every finished task — the hook fires from the worker pool once a
# task reaches a terminal state (done / failed / cancelled).
set_task_finished_hook(History.record)

_STATUS_ICON = {
    "done": "✅",
    "failed": "❌",
    "cancelled": "🚫",
}


async def _feature_enabled() -> bool:
    from db import db

    toggles = await db.get_setting("feature_toggles", {}) if db else {}
    if isinstance(toggles, dict):
        return bool(toggles.get("mirror_leech", False))
    return False


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------

async def _hub_content(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    active = ml_worker_pool.active_count(user_id)
    hstats = await History.stats(user_id)

    configured = 0
    total_prov = 0
    for cls in available_uploaders():
        total_prov += 1
        with contextlib.suppress(Exception):
            if await cls().is_configured(user_id):
                configured += 1

    lines = [
        "Send `/ml <link>` to mirror something — or paste **several links "
        "in one message** to queue a whole batch (max 10).",
        "",
        "**Supported:** direct HTTP(S) · yt-dlp pages · Telegram refs · RSS",
        "",
        f"**Active now:** `{active}` task(s)",
        f"**History:** `{hstats['total']}` run(s) — "
        f"✅ {hstats['done']} · ❌ {hstats['failed']}",
        f"**Destinations ready:** `{configured}/{total_prov}`",
    ]

    rows = [
        [
            InlineKeyboardButton("📊 Queue", callback_data="ml_queue"),
            InlineKeyboardButton("🗂 History", callback_data="ml_history"),
        ],
        [InlineKeyboardButton("⚙️ Settings & Destinations", callback_data="ml_cfg")],
    ]
    # Nudge fresh users towards setup while only the zero-config Telegram
    # destination is ready.
    if configured <= 1:
        rows.append(
            [InlineKeyboardButton("🚀 Setup Assistant", callback_data="ml_setup")]
        )
    else:
        rows.append(
            [InlineKeyboardButton("🧭 Destination Overview", callback_data="ml_setup")]
        )
    return frame("☁️ **Mirror-Leech**", "\n".join(lines)), InlineKeyboardMarkup(rows)


async def render_hub(client: Client, message: Message) -> None:
    """Called by mirror_leech_ui.ml_command when `/ml` has no argument."""
    text, markup = await _hub_content(message.from_user.id)
    await message.reply_text(text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^ml_hub$"))
async def ml_hub(client: Client, cq: CallbackQuery) -> None:
    await cq.answer()
    text, markup = await _hub_content(cq.from_user.id)
    with contextlib.suppress(MessageNotModified):
        await cq.message.edit_text(text, reply_markup=markup)


# ---------------------------------------------------------------------------
# History + Repeat
# ---------------------------------------------------------------------------

async def _render_history(client: Client, user_id: int, edit_target) -> None:
    entries = await History.recent(user_id, limit=12)

    if not entries:
        body = (
            "No finished tasks yet.\n"
            "Run `/ml <link>` — every completed transfer lands here, "
            "restart-proof, for 30 days."
        )
        rows = [[InlineKeyboardButton("← Back", callback_data="ml_hub")]]
    else:
        body = (
            "Your last transfers (kept 30 days).\n"
            "Tap an entry to **re-run it** with the same destinations."
        )
        rows = []
        for e in entries:
            icon = _STATUS_ICON.get(e.get("status"), "▫️")
            src = e.get("source", "?")
            # Compact display: strip scheme, keep the tail end users
            # recognise (filename / video id).
            disp = src.split("://", 1)[-1]
            if len(disp) > 34:
                disp = "…" + disp[-33:]
            age = History.age_str(e.get("created_at"))
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{icon} {disp} · {age}",
                        callback_data=f"ml_rep_{e['_id']}",
                    )
                ]
            )
        rows.append([InlineKeyboardButton("← Back", callback_data="ml_hub")])

    text = frame("🗂 **Mirror-Leech — History**", body)
    markup = InlineKeyboardMarkup(rows)
    if isinstance(edit_target, CallbackQuery):
        with contextlib.suppress(MessageNotModified):
            await edit_target.message.edit_text(text, reply_markup=markup)
    else:
        await edit_target.reply_text(text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^ml_history$"))
async def ml_history(client: Client, cq: CallbackQuery) -> None:
    await cq.answer()
    await _render_history(client, cq.from_user.id, cq)


@Client.on_message(filters.command("mlhistory") & filters.private)
async def ml_history_command(client: Client, message: Message) -> None:
    if not await _feature_enabled():
        return
    await _render_history(client, message.from_user.id, message)


@Client.on_callback_query(filters.regex(r"^ml_rep_([0-9a-f]{24})$"))
async def ml_repeat(client: Client, cq: CallbackQuery) -> None:
    from tools.mirror_leech.ProgressRender import (
        render_task_text,
        update_progress_message,
    )
    from tools.mirror_leech.Runner import run_task
    from tools.mirror_leech.Tasks import MLTask

    user_id = cq.from_user.id
    entry = await History.get(cq.data.rsplit("_", 1)[-1])
    if not entry or entry.get("user_id") != user_id:
        await cq.answer("History entry not found.", show_alert=True)
        return
    if not entry.get("uploader_ids"):
        await cq.answer("This entry has no destinations to repeat.", show_alert=True)
        return

    # Same duplicate guard as a fresh /ml.
    dupe = next(
        (
            t
            for t in ml_worker_pool.list_for_user(user_id)
            if t.source == entry.get("source")
            and t.status in ("queued", "downloading", "uploading")
        ),
        None,
    )
    if dupe:
        await cq.answer(
            f"Already running as task {dupe.id}.", show_alert=True
        )
        return

    await cq.answer("🔁 Re-queued.")

    task = MLTask.new(
        user_id=user_id,
        source=entry.get("source", ""),
        downloader_id=entry.get("downloader_id") or "",
        uploader_ids=list(entry.get("uploader_ids") or []),
    )
    task.message_chat_id = cq.message.chat.id
    progress = await client.send_message(
        cq.message.chat.id,
        render_task_text(task),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⏹ Cancel", callback_data=f"ml_cancel_{task.id}")]]
        ),
    )
    task.message_id = progress.id

    async def _runner(t: MLTask) -> None:
        try:
            await run_task(
                t,
                client,
                progress_cb=lambda current: update_progress_message(client, current),
            )
        except Exception as exc:
            t.status = "failed"
            t.error = str(exc)
            await update_progress_message(client, t)
            raise
        await update_progress_message(client, t)

    try:
        ml_worker_pool.enqueue(task, _runner)
        logger.info("ml_repeat queued task %s (from history) for %s", task.id, user_id)
    except Exception as exc:
        logger.exception("ml_repeat enqueue failed")
        with contextlib.suppress(Exception):
            await progress.edit_text(f"❌ Could not start task: `{exc}`")


# ---------------------------------------------------------------------------
# Setup assistant
# ---------------------------------------------------------------------------

@Client.on_callback_query(filters.regex(r"^ml_setup$"))
async def ml_setup(client: Client, cq: CallbackQuery) -> None:
    await cq.answer()
    user_id = cq.from_user.id

    rows: list[list[InlineKeyboardButton]] = []
    ready = 0
    total = 0
    for cls in available_uploaders():
        total += 1
        configured = False
        with contextlib.suppress(Exception):
            configured = await cls().is_configured(user_id)
        if configured:
            ready += 1
        mark = "✅" if configured else "⚙️"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark} {cls.display_name}",
                    callback_data=f"ml_cfg_up_{cls.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("← Back", callback_data="ml_hub")])

    body = (
        f"**{ready}/{total}** destinations are ready to receive files.\n\n"
        "✅ = configured & usable · ⚙️ = needs credentials\n\n"
        "Tap a destination to open its config screen — every provider "
        "has a 📖 step-by-step guide and a paste-your-token flow. "
        "**Telegram** works out of the box, so you can start mirroring "
        "immediately and add cloud storage later."
    )
    with contextlib.suppress(MessageNotModified):
        await cq.message.edit_text(
            frame("🚀 **Mirror-Leech — Setup Assistant**", body),
            reply_markup=InlineKeyboardMarkup(rows),
        )
