# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""plugins.myfiles — MyFiles feature package.

Re-exports the shared core helpers so callers keep the compact
``from plugins.myfiles import get_myfiles_state`` form.

Importing this package side-effect-registers the Pyrogram handlers in
``handlers`` and ``extras`` (Pyrogram plugin-discovery loads submodules
automatically — this explicit import makes the intent obvious for
static analyzers and for readers who don't know about the discovery).

Mode: BOTH (public + non-public).
"""

from plugins.myfiles.core import (
    build_files_list_keyboard,
    get_myfiles_main_menu,
    get_myfiles_state,
    safe_edit_or_send,
    set_myfiles_state,
)

from . import (
    extras,  # noqa: F401  (registers @Client handlers)
    handlers,  # noqa: F401  (registers @Client handlers)
    insights,  # noqa: F401  (registers @Client handlers)
)

__all__ = [
    "build_files_list_keyboard",
    "get_myfiles_main_menu",
    "get_myfiles_state",
    "safe_edit_or_send",
    "set_myfiles_state",
]
