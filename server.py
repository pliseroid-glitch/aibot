"""Run bot + serve webapp on the same aiohttp server.

Railway requires listening on $PORT for health checks.
This module starts an aiohttp web server for the mini app
and launches the bot polling in the background.
"""

import asyncio
import logging
import os

import aiohttp
from aiohttp import web

import bot as bot_module
import config

log = logging.getLogger("server")


async def start_bot() -> None:
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    b = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    log.info("Bot starting...")
    try:
        await bot_module.dp.start_polling(b, allowed_updates=bot_module.dp.resolve_used_update_types())
    finally:
        await b.session.close()


async def on_startup(app: web.Application) -> None:
    asyncio.create_task(start_bot())


async def proxy_chat(request: web.Request) -> web.StreamResponse:
    """Proxy /api/chat requests to unlimited.surf to avoid CORS and hide API key."""
    body = await request.json()
    headers = {
        "Authorization": f"Bearer {config.API_KEY}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=None, sock_read=300, sock_connect=30)
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(config.CHAT_URL, headers=headers, json=body) as r:
            if r.status != 200:
                text = await r.text()
                await resp.write(f"data: {text[:500]}\n\n".encode())
                await resp.write_eof()
                return resp
            async for chunk in r.content:
                if chunk:
                    await resp.write(chunk)
    await resp.write_eof()
    return resp


async def proxy_models(request: web.Request) -> web.Response:
    """Proxy /api/models to unlimited.surf."""
    headers = {"Authorization": f"Bearer {config.API_KEY}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(config.MODELS_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json()
    return web.json_response(data)


def create_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    # API proxy
    app.router.add_post("/api/chat", proxy_chat)
    app.router.add_get("/api/models", proxy_models)
    # Static files for mini app
    webapp_dir = os.path.join(os.path.dirname(__file__), "webapp")
    app.router.add_static("/", webapp_dir, name="webapp", show_index=True)
    return app


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    log.info("Starting server on port %d", port)
    web.run_app(create_app(), host="0.0.0.0", port=port)
