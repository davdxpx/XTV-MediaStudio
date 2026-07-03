# --------------------------------------------------------------------------
# Developed by 𝕏0L0™ (@davdxpx) | © 2026 XTV Network Global
# Don't Remove Credit
# --------------------------------------------------------------------------
"""plugins.flow — the rename flow, split into logical submodules.

Before this refactor the whole rename / upload / confirm pipeline
lived in a single 3,500-line ``plugins/flow.py``. The file was split
into the following modules, each owning one concern:

 * ``sessions``            — file / batch state, expiry + debounce timers
 * ``type_selection``      — General / Movie / Series / Personal / Subtitle buttons
 * ``search``              — text input router + TMDb search + manual title
 * ``tmdb_selection``      — TMDb card pick, manual entry, send-as, ready-file
 * ``destinations``        — dest folder + dumb channel + language + cancel
 * ``upload``              — file upload dispatcher + batch orchestrator
 * ``archive``             — zip/rar/7z extraction flow
 * ``confirmation_screen`` — auto-detection + confirm screen + change menus
 * ``pickers``             — Codec / Audio / Specials multi-page pickers

Every submodule is imported below so its Pyrogram decorators run at
import time and register their handlers on the shared Client.

External callers (``main.py``, ``plugins/start.py``, various tools)
used to ``from plugins.flow import <X>``. We preserve those imports
by re-exporting the same symbols from here — no call-site changes
were needed in the rest of the codebase.
"""

# Submodule imports — each runs its @Client.on_* decorators at import
# time so Pyrogram picks up the handlers. ``sessions`` is side-effect-
# only (no decorators) but every other submodule depends on its shared
# dicts, so we always import it alongside the rest.
from plugins.flow import (
    archive,  # noqa: F401
    batch_actions,  # noqa: F401
    confirmation_screen,  # noqa: F401
    destinations,  # noqa: F401
    pickers,  # noqa: F401
    search,  # noqa: F401
    sessions,  # noqa: F401
    tmdb_selection,  # noqa: F401
    type_selection,  # noqa: F401
    upload,  # noqa: F401
)

# -- Re-exports for existing external call sites -----------------------------
# Anything listed below is imported today by the rest of the codebase
# via ``from plugins.flow import X``. Keeping the re-exports means the
# split is invisible to callers — no downstream changes needed.
#
#   main.py                 → cleanup_stale_* (sessions)
#   plugins/start.py        → handle_start_renaming / _type_general /
#                              _type_personal (type_selection)
#   Other plugins / tools   → file_sessions, update_confirmation_message,
#                              process_ready_file, format_episode_str, …
from plugins.flow.confirmation_screen import (  # noqa: F401
    handle_auto_detection,
    update_auto_detected_message,
    update_confirmation_message,
)
from plugins.flow.sessions import (  # noqa: F401
    batch_sessions,
    batch_status_msgs,
    batch_tasks,
    cleanup_stale_debounce_entries,
    cleanup_stale_file_sessions,
    file_sessions,
    format_episode_str,
)
from plugins.flow.tmdb_selection import process_ready_file  # noqa: F401
from plugins.flow.type_selection import (  # noqa: F401
    handle_start_renaming,
    handle_type_general,
    handle_type_personal,
)
