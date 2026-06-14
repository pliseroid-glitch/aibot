"""Async client for unlimited.surf API.

The API takes a single `message` string and (in our tests) does NOT continue
conversations server-side — passing back a chatId starts a fresh chat. So we
encode prior turns ourselves: a short ROLE-tagged transcript is prepended to
the new user message before sending.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import aiohttp

import config


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.API_KEY}",
        "Content-Type": "application/json",
    }


async def fetch_models() -> list[dict]:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            config.MODELS_URL,
            headers=_headers(),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            r.raise_for_status()
            payload = await r.json()
    models = [m for m in payload.get("data", []) if m.get("available", True)]
    models.sort(key=lambda m: (m.get("provider", ""), m.get("tier", ""), m.get("name", "")))
    return models


def build_message(
    new_user_text: str,
    history: list[dict] | None = None,
    system: str | None = None,
) -> str:
    """Pack system prompt + prior turns + new user text into one string.

    The API only takes a single `message` field, so we encode the whole
    transcript as a labelled block.  Models handle this format reliably.
    """
    if not history and not system:
        return new_user_text
    parts: list[str] = []
    if system:
        parts.append(f"[SYSTEM]\n{system}")
    if history:
        parts.append("[CONVERSATION SO FAR]")
        for turn in history:
            role = turn.get("role", "user").upper()
            content = turn.get("content", "")
            parts.append(f"{role}: {content}")
    parts.append(f"USER: {new_user_text}")
    parts.append("ASSISTANT:")
    return "\n\n".join(parts)


async def stream_chat(
    message: str,
    model: str,
    effort: str,
    cancel_event: asyncio.Event | None = None,
) -> AsyncIterator[dict]:
    """Stream chat completion.

    Yields one dict per SSE `data:` line:
      {"status": "..."} | {"delta": "..."} | {"finish": true} |
      {"done": true} | {"error": "..."}

    If `cancel_event` is set during iteration, the stream is aborted cleanly.
    """
    body = {"message": message, "model": model, "effort": effort}
    timeout = aiohttp.ClientTimeout(total=None, sock_read=300, sock_connect=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(config.CHAT_URL, headers=_headers(), json=body) as r:
                if r.status != 200:
                    text = await r.text()
                    yield {"error": f"HTTP {r.status}: {text[:500]}"}
                    return
                async for raw in r.content:
                    if cancel_event is not None and cancel_event.is_set():
                        yield {"cancelled": True}
                        return
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    payload = line[5:].strip() if line.startswith("data:") else line
                    if not payload:
                        continue
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    yield obj
    except asyncio.TimeoutError:
        yield {"error": "connection timed out"}
    except aiohttp.ClientError as e:
        yield {"error": f"network error: {e}"}
