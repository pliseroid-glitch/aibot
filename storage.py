"""JSON-file storage for per-scope settings, history and rate limits.

A *scope* identifies a conversation:
    u:<user_id>   - a user's private chat with the bot
    g:<chat_id>   - a group chat (whole group shares one running context)

For inline mode we don't use scope (no follow-up needed).

Stored per scope:
    model       - gateway-* id
    effort      - low | medium | high
    system      - optional custom system prompt
    history     - list of {role, content} pairs (capped at HISTORY_MAX)
    msg_count   - lifetime number of chat requests
    last_used   - unix timestamp of last request

Rate limiting lives in-memory only (not persisted across restarts).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from typing import Any

import aiofiles

import config

_lock = asyncio.Lock()

HISTORY_MAX = 20


async def _load() -> dict:
    if not os.path.exists(config.STATE_FILE):
        return {}
    try:
        async with aiofiles.open(config.STATE_FILE, "r", encoding="utf-8") as f:
            return json.loads(await f.read())
    except (json.JSONDecodeError, OSError):
        return {}


async def _save(data: dict) -> None:
    tmp = config.STATE_FILE + ".tmp"
    async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
        await f.write(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, config.STATE_FILE)


# ----- scope helpers -----

def user_scope(user_id: int) -> str:
    return f"u:{user_id}"


def chat_scope(chat_id: int) -> str:
    return f"g:{chat_id}"


# ----- scope CRUD -----

async def get_scope(scope: str) -> dict:
    async with _lock:
        return (await _load()).get(scope, {})


async def update_scope(scope: str, **fields: Any) -> dict:
    async with _lock:
        data = await _load()
        entry = data.get(scope, {})
        entry.update(fields)
        data[scope] = entry
        await _save(data)
        return entry


async def get_model(scope: str) -> str:
    return (await get_scope(scope)).get("model") or config.DEFAULT_MODEL


async def get_effort(scope: str) -> str:
    return (await get_scope(scope)).get("effort") or config.DEFAULT_EFFORT


async def get_system(scope: str) -> str | None:
    return (await get_scope(scope)).get("system")


async def get_history(scope: str) -> list[dict]:
    return list((await get_scope(scope)).get("history") or [])


async def append_history(scope: str, user_text: str, assistant_text: str) -> None:
    """Add one user/assistant pair; trim to HISTORY_MAX pairs."""
    async with _lock:
        data = await _load()
        entry = data.get(scope, {})
        hist = list(entry.get("history") or [])
        hist.append({"role": "user", "content": user_text})
        hist.append({"role": "assistant", "content": assistant_text})
        if len(hist) > HISTORY_MAX * 2:
            hist = hist[-HISTORY_MAX * 2:]
        entry["history"] = hist
        entry["msg_count"] = entry.get("msg_count", 0) + 1
        entry["last_used"] = int(time.time())
        data[scope] = entry
        await _save(data)


async def clear_history(scope: str) -> None:
    async with _lock:
        data = await _load()
        entry = data.get(scope, {})
        entry["history"] = []
        data[scope] = entry
        await _save(data)


# ----- in-memory rate limiter -----

_rate_buckets: dict[int, deque] = {}
_rate_lock = asyncio.Lock()


async def check_rate(user_id: int) -> tuple[bool, int]:
    """Return (allowed, seconds_until_next_slot)."""
    now = time.monotonic()
    cutoff = now - 60.0
    async with _rate_lock:
        bucket = _rate_buckets.get(user_id)
        if bucket is None:
            bucket = deque()
            _rate_buckets[user_id] = bucket
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= config.RATE_LIMIT_PER_MIN:
            wait = int(60.0 - (now - bucket[0])) + 1
            return False, max(1, wait)
        bucket.append(now)
        return True, 0
