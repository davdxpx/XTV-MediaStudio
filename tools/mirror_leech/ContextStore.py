"""In-memory short-lived cache for Mirror-Leech picker state.

Telegram limits callback_data to 64 bytes, which isn't enough to carry a
full URL + selected uploader list across the button round-trips. Instead
callbacks pass a 6-char `ctx_id` that resolves back to the full payload
stored here.

Entries expire after 15 minutes automatically on read.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

_TTL_SECONDS = 15 * 60
_store: dict[str, tuple[float, "PickerContext"]] = {}


@dataclass
class PickerContext:
    user_id: int
    source: str
    candidate_downloader: Optional[str] = None
    selected_uploaders: list[str] = field(default_factory=list)
    origin_msg_id: Optional[int] = None
    origin_chat_id: Optional[int] = None
    # When the entry was created as a batch over MyFiles selection:
    file_ids: list[str] = field(default_factory=list)
    # When the entry was created from a multi-URL paste (`/ml url1 url2 …`):
    # every URL in the batch. `source` holds the first one for display.
    url_sources: list[str] = field(default_factory=list)


def new_id() -> str:
    return secrets.token_urlsafe(5)[:6]


def put(ctx: PickerContext) -> str:
    _evict_expired()
    cid = new_id()
    _store[cid] = (time.time(), ctx)
    return cid


def get(ctx_id: str) -> Optional[PickerContext]:
    entry = _store.get(ctx_id)
    if not entry:
        return None
    ts, ctx = entry
    if time.time() - ts > _TTL_SECONDS:
        _store.pop(ctx_id, None)
        return None
    return ctx


def drop(ctx_id: str) -> None:
    _store.pop(ctx_id, None)


def _evict_expired() -> None:
    now = time.time()
    stale = [k for k, (ts, _) in _store.items() if now - ts > _TTL_SECONDS]
    for k in stale:
        _store.pop(k, None)
