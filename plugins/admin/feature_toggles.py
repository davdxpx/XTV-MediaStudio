# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# Telegram Channel @XTVbots
# Developed for the 𝕏TV Network @XTVglobal
# Backup Channel @XTVhome
# Contact on Telegram @davdxpx
# --------------------------------------------------------------------------
"""
Feature Toggles & Access Limits admin domain.

Houses the Settings / Access & Limits top-level entry, the quick
system toggles (premium / deluxe / trial / myfiles) surfaced in that
menu, the Global Feature Toggles submenu with its individual
`admin_gtoggle_*` flips, the Per-Plan Limits submenu entry, and the
read-only `admin_global_daily_egress` display.

The rendering of the Access & Limits menu and the Feature Toggles
submenu used to be triggered via recursive `admin_callback` self-calls
(`callback_query.data = "admin_access_limits"; await admin_callback(...)`).
After the split those self-calls would cross module boundaries, so we
use local `_render_*` helpers instead.

The `admin_global_daily_egress` branch here only renders the preview
screen; the text-input flow that backs the "Change" button
(`awaiting_global_egress`) is registered with the shared
``text_dispatcher`` and handled in ``premium.py``.
"""

import contextlib

from pyrogram import Client, ContinuePropagation, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from db import db
from plugins.admin.core import get_admin_access_limits_menu, is_admin
from utils.telegram.log import get_logger

logger = get_logger("plugins.admin.feature_toggles")


async def _render_access_limits(callback_query: CallbackQuery):
    reply_markup = await get_admin_access_limits_menu()
    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            "🔒 **Settings & Controls**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Toggle system features on/off and configure plans, limits, and tools.",
            reply_markup=reply_markup,
        )


async def _render_feature_toggles(callback_query: CallbackQuery):
    from utils.tmdb.gate import is_tmdb_available

    toggles = await db.get_feature_toggles()
    tmdb_on = is_tmdb_available()
    audio_en = toggles.get("audio_editor", True)
    conv_en = toggles.get("file_converter", True)
    wm_en = toggles.get("watermarker", True)
    sub_en = toggles.get("subtitle_extractor", True)
    trim_en = toggles.get("video_trimmer", True)
    info_en = toggles.get("media_info", True)
    voice_en = toggles.get("voice_converter", True)
    vnote_en = toggles.get("video_note_converter", True)
    yt_en = toggles.get("youtube_tool", True)
    four_k_en = toggles.get("4k_enhancement", True)
    batch_pro_en = toggles.get("batch_processing_pro", True)

    def emoji(state):
        return "✅" if state else "❌"

    tmdb_row = (
        "🎬 **TMDb:** ✅ Configured"
        if tmdb_on
        else "🎬 **TMDb:** ❌ Missing (optional — tap __TMDb Status__ in the main menu)"
    )
    text = (
        "⚙️ **Global Feature Toggles**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{tmdb_row}\n\n"
        "Enable or disable features globally (master switch).\n"
        "> Disabled features are hidden for **all** users & plans.\n"
        "> Per-plan overrides can be set in Per-Plan Settings.\n\n"
        "> **Performance Impact:**\n"
        "> • **File Converter:** High CPU & RAM\n"
        "> • **Watermarker / Trimmer:** Medium CPU\n"
        "> • **Audio Editor / Voice:** Low CPU\n"
        "> • **Media Info:** Minimal CPU\n\n"
        "Click a feature below to toggle:"
    )

    buttons = [
        [
            InlineKeyboardButton(
                f"{emoji(conv_en)} 🔀 File Converter",
                callback_data="admin_gtoggle_file_converter",
            ),
            InlineKeyboardButton(
                f"{emoji(sub_en)} 📝 Subtitle Extractor",
                callback_data="admin_gtoggle_subtitle_extractor",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{emoji(wm_en)} ©️ Watermarker",
                callback_data="admin_gtoggle_watermarker",
            ),
            InlineKeyboardButton(
                f"{emoji(audio_en)} 🎵 Audio Editor",
                callback_data="admin_gtoggle_audio_editor",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{emoji(trim_en)} ✂️ Video Trimmer",
                callback_data="admin_gtoggle_video_trimmer",
            ),
            InlineKeyboardButton(
                f"{emoji(info_en)} ℹ️ Media Info",
                callback_data="admin_gtoggle_media_info",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{emoji(voice_en)} 🎙️ Voice Converter",
                callback_data="admin_gtoggle_voice_converter",
            ),
            InlineKeyboardButton(
                f"{emoji(vnote_en)} ⭕ Video Note",
                callback_data="admin_gtoggle_video_note_converter",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{emoji(yt_en)} ▶️ YouTube Tool",
                callback_data="admin_gtoggle_youtube_tool",
            )
        ],
        [
            InlineKeyboardButton(
                f"{emoji(four_k_en)} 📺 4K Enhancement",
                callback_data="admin_gtoggle_4k_enhancement",
            ),
            InlineKeyboardButton(
                f"{emoji(batch_pro_en)} 📦 Batch Pro",
                callback_data="admin_gtoggle_batch_processing_pro",
            ),
        ],
        [
            InlineKeyboardButton(
                "🗂 MyFiles Features ›",
                callback_data="admin_ftmenu_myfiles",
            ),
            InlineKeyboardButton(
                "☁️ Mirror-Leech Features ›",
                callback_data="admin_ftmenu_mirrorleech",
            ),
        ],
        [
            InlineKeyboardButton(
                "← Back to Settings", callback_data="admin_access_limits"
            )
        ],
    ]

    with contextlib.suppress(MessageNotModified):
        await callback_query.message.edit_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )


# ---------------------------------------------------------------------------
# MyFiles feature toggles sub-screen
# ---------------------------------------------------------------------------

# (key, short label, one-line description)
_MYFILES_SUBFEATURES: list[tuple[str, str, str]] = [
    ("myfiles_trash",    "🗑 Trash / Recycle",
     "Soft-delete files into a recycle bin; restore or purge individually."),
    ("myfiles_audit",    "🧾 Audit Log",
     "Admin-side log of every MyFiles action (rename, delete, share, …)."),
    ("myfiles_tags",     "#️⃣ Tags",
     "Per-file tagging with +tag/-tag editor, tag cloud + tag-scoped lists."),
    ("myfiles_versions", "📜 Versioning",
     "Keep multiple versions per file, restore older ones on demand."),
    ("myfiles_quotas",   "💾 Per-User Quotas",
     "Storage usage header + block uploads above the plan's byte limit."),
    ("myfiles_search",   "🔎 Advanced Search",
     "DSL search (tag:, ext:, size:>, before:, after:) + filename regex."),
    ("myfiles_sharing",  "🔗 Granular Sharing",
     "Per-link access mode, expiry, view cap and optional password."),
    ("myfiles_activity", "📊 Activity Feed",
     "User-facing timeline of views / downloads / edits / shares."),
    ("myfiles_bulk",     "📦 Bulk Operations",
     "Bulk tag / pin / unpin rows on the multi-select toolbar."),
    ("myfiles_nested",   "🪜 Nested Folders",
     "Folders inside folders with breadcrumb navigation."),
    ("myfiles_smart",    "🧠 Smart Collections",
     "Saved-query folders that stay in sync via the search DSL."),
    ("myfiles_insights", "📈 Insights",
     "Stats dashboard, expiring-soon rescue, pinned view, duplicate "
     "cleanup + series completeness reports."),
    ("myfiles_tools_bridge", "🛠 Tools Bridge",
     "Open a stored file directly in any tool (re-rename, convert, "
     "trim, …) without re-uploading it."),
]

_MIRROR_LEECH_SUBFEATURES: list[tuple[str, str, str]] = [
    ("mirror_leech",            "☁️ Mirror-Leech Master",
     "Enables the /ml entrypoint and the whole Mirror-Leech stack."),
    ("mirror_leech_gallery_dl", "🖼 Gallery-DL / Social",
     "Fetch social-media galleries (Instagram, Reddit, Twitter, …)."),
    ("mirror_leech_mediaplat",  "☁ Cloud-Host Scraper",
     "Direct-link scraping for Mediafire, Pixeldrain, GoFile, KrakenFiles."),
    ("mirror_leech_instant",    "🔗 Instant-Share",
     "Hot path for re-mirroring the bot's own DDL links."),
]

_PAGE_SIZE = 4


async def _render_subtoggle_screen(
    cq: CallbackQuery,
    title: str,
    keys: list[tuple[str, str, str]],
    menu_id: str,
    page: int = 0,
    back_cb: str = "admin_feature_toggles",
) -> None:
    """Paginated toggle screen. `menu_id` identifies the list ("myfiles"
    or "mirrorleech") so pagination callbacks can re-render the right
    sub-screen after flipping a toggle."""
    toggles = await db.get_feature_toggles()
    total_pages = max(1, (len(keys) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    chunk = keys[start : start + _PAGE_SIZE]

    # Body: per-feature name + status + description.
    body_lines: list[str] = [
        "> Tap a feature below to enable or disable it.",
        "> Disabled features vanish from the user UI.",
        "",
    ]
    for key, label, desc in chunk:
        on = bool(toggles.get(key, False))
        mark = "✅" if on else "❌"
        body_lines.append(f"{mark} **{label}**")
        body_lines.append(f"   `{desc}`")
        body_lines.append("")

    buttons: list[list[InlineKeyboardButton]] = []
    for key, label, _desc in chunk:
        on = bool(toggles.get(key, False))
        buttons.append([InlineKeyboardButton(
            f"{'✅' if on else '❌'} {label}",
            callback_data=f"admin_ftog_{key}__p{page}",
        )])

    # Pagination strip.
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "⬅ Prev", callback_data=f"admin_ftpage_{menu_id}_{page - 1}"
        ))
    nav.append(InlineKeyboardButton(
        f"{page + 1} / {total_pages}", callback_data="noop"
    ))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(
            "Next ➡", callback_data=f"admin_ftpage_{menu_id}_{page + 1}"
        ))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("← Back", callback_data=back_cb)])

    text = (
        f"{title}  ·  Page {page + 1}/{total_pages}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        + "\n".join(body_lines).rstrip()
        + "\n\n━━━━━━━━━━━━━━━━━━━━"
    )
    with contextlib.suppress(MessageNotModified):
        await cq.message.edit_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )


def _subtoggle_menu(menu_id: str):
    if menu_id == "myfiles":
        return ("🗂 **MyFiles Feature Toggles**", _MYFILES_SUBFEATURES)
    if menu_id == "mirrorleech":
        return ("☁️ **Mirror-Leech Feature Toggles**", _MIRROR_LEECH_SUBFEATURES)
    return None


@Client.on_callback_query(
    filters.regex(
        r"^(admin_access_limits$|admin_quick_toggle_(?:premium|deluxe|trial|myfiles)$"
        r"|admin_feature_toggles$|admin_gtoggle_|admin_per_plan_limits$"
        r"|admin_global_daily_egress$"
        r"|admin_ftmenu_(?:myfiles|mirrorleech)$"
        r"|admin_ftpage_(?:myfiles|mirrorleech)_\d+$"
        r"|admin_ftog_)"
    )
)
async def feature_toggles_cb(client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if not is_admin(user_id):
        raise ContinuePropagation
    data = callback_query.data

    # --- Quick System Toggles (Settings menu) ---
    if data == "admin_quick_toggle_premium":
        config = await db.get_public_config()
        enabled = config.get("premium_system_enabled", False)
        await db.update_public_config("premium_system_enabled", not enabled)
        await callback_query.answer(
            f"Premium System {'Disabled' if enabled else 'Enabled'}",
            show_alert=True,
        )
        await _render_access_limits(callback_query)
        return

    if data == "admin_quick_toggle_deluxe":
        config = await db.get_public_config()
        enabled = config.get("premium_deluxe_enabled", False)
        await db.update_public_config("premium_deluxe_enabled", not enabled)
        await callback_query.answer(
            f"Deluxe Plan {'Disabled' if enabled else 'Enabled'}",
            show_alert=True,
        )
        await _render_access_limits(callback_query)
        return

    if data == "admin_quick_toggle_trial":
        config = await db.get_public_config()
        enabled = config.get("premium_trial_enabled", False)
        await db.update_public_config("premium_trial_enabled", not enabled)
        await callback_query.answer(
            f"Trial Mode {'Disabled' if enabled else 'Enabled'}",
            show_alert=True,
        )
        await _render_access_limits(callback_query)
        return

    if data == "admin_quick_toggle_myfiles":
        enabled = await db.get_setting("myfiles_enabled", default=False)
        await db.update_setting("myfiles_enabled", not enabled)
        await callback_query.answer(
            f"MyFiles System {'Disabled' if enabled else 'Enabled'}",
            show_alert=True,
        )
        await _render_access_limits(callback_query)
        return

    # --- Access & Limits root ---
    if data == "admin_access_limits":
        await callback_query.answer()
        await _render_access_limits(callback_query)
        return

    # --- Feature Toggles submenu ---
    if data == "admin_feature_toggles":
        await callback_query.answer()
        await _render_feature_toggles(callback_query)
        return

    if data.startswith("admin_gtoggle_"):
        feature = data.replace("admin_gtoggle_", "")
        toggles = await db.get_feature_toggles()
        current_state = toggles.get(feature, True)
        new_state = not current_state
        await db.update_feature_toggle(feature, new_state)
        await callback_query.answer(
            f"{'Enabled' if new_state else 'Disabled'} {feature.replace('_', ' ').title()}.",
            show_alert=True,
        )
        await _render_feature_toggles(callback_query)
        return

    # --- MyFiles / Mirror-Leech sub-feature screens ---
    if data == "admin_ftmenu_myfiles":
        await callback_query.answer()
        title, keys = _subtoggle_menu("myfiles")
        await _render_subtoggle_screen(
            callback_query, title, keys, menu_id="myfiles", page=0,
        )
        return

    if data == "admin_ftmenu_mirrorleech":
        await callback_query.answer()
        title, keys = _subtoggle_menu("mirrorleech")
        await _render_subtoggle_screen(
            callback_query, title, keys, menu_id="mirrorleech", page=0,
        )
        return

    if data.startswith("admin_ftpage_"):
        rest = data.removeprefix("admin_ftpage_")
        menu_id, _, page_str = rest.rpartition("_")
        menu = _subtoggle_menu(menu_id)
        if menu is None:
            await callback_query.answer("Unknown menu.", show_alert=True)
            return
        try:
            page = int(page_str)
        except ValueError:
            page = 0
        await callback_query.answer()
        title, keys = menu
        await _render_subtoggle_screen(
            callback_query, title, keys, menu_id=menu_id, page=page,
        )
        return

    if data.startswith("admin_ftog_"):
        # Callback format: admin_ftog_<feature_key>__p<page>
        payload = data.removeprefix("admin_ftog_")
        feature, sep, page_str = payload.partition("__p")
        if not feature:
            # Defensive: empty feature would mean a malformed callback.
            # Answer so the Telegram spinner stops instead of hanging.
            await callback_query.answer("Malformed toggle callback.",
                                        show_alert=True)
            return
        try:
            page = int(page_str) if sep else 0
        except ValueError:
            page = 0
        toggles = await db.get_feature_toggles()
        current_state = bool(toggles.get(feature, False))
        new_state = not current_state
        logger.info(
            "feature toggle flip: key=%s current=%s new=%s user=%s page=%s",
            feature, current_state, new_state, user_id, page,
        )
        await db.update_feature_toggle(feature, new_state)
        await callback_query.answer(
            f"{'Enabled' if new_state else 'Disabled'} "
            f"{feature.replace('_', ' ').title()}",
            show_alert=False,
        )
        # Re-render the correct submenu page.
        if feature.startswith("myfiles"):
            title, keys = _subtoggle_menu("myfiles")
            await _render_subtoggle_screen(
                callback_query, title, keys, menu_id="myfiles", page=page,
            )
        elif feature.startswith("mirror_leech"):
            title, keys = _subtoggle_menu("mirrorleech")
            await _render_subtoggle_screen(
                callback_query, title, keys, menu_id="mirrorleech", page=page,
            )
        else:
            await _render_feature_toggles(callback_query)
        return

    # --- Per-Plan Limits entry ---
    if data == "admin_per_plan_limits":
        await callback_query.answer()
        text = (
            "⚙️ **Per-Plan Settings**\n\n"
            "> Select a subscription tier below to view its current quotas, "
            "features, pricing, and to modify its settings."
        )
        buttons = [
            [InlineKeyboardButton("🆓 Manage Free Plan", callback_data="admin_edit_plan_free")],
            [InlineKeyboardButton("🌟 Manage Standard Plan", callback_data="admin_edit_plan_standard")],
            [InlineKeyboardButton("💎 Manage Deluxe Plan", callback_data="admin_edit_plan_deluxe")],
            [InlineKeyboardButton("← Back to Settings", callback_data="admin_access_limits")],
        ]
        with contextlib.suppress(MessageNotModified):
            await callback_query.message.edit_text(
                text, reply_markup=InlineKeyboardMarkup(buttons)
            )
        return

    # --- Global Daily Egress preview ---
    if data == "admin_global_daily_egress":
        await callback_query.answer()
        current_val = await db.get_global_daily_egress_limit()
        with contextlib.suppress(MessageNotModified):
            await callback_query.message.edit_text(
                f"🌍 **Edit Global Daily Egress Limit**\n\n"
                f"Current: `{current_val}` MB\n\n"
                "Click below to change it.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✏️ Change", callback_data="prompt_global_daily_egress"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "← Back to Settings",
                                callback_data="admin_access_limits",
                            )
                        ],
                    ]
                ),
            )
        return
