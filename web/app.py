"""Embedded web dashboard API for Essusic.

Requires ``aiohttp`` (already a dependency for lyrics).
Shares the same bot process — direct access to MusicCog state.

Set WEB_PORT and WEB_API_TOKEN to enable the API. The token grants access to
all bot guilds; this is an operator API, not a Discord user dashboard.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from typing import TYPE_CHECKING

import aiohttp.web as web

if TYPE_CHECKING:
    from discord.ext import commands

log = logging.getLogger(__name__)

routes = web.RouteTableDef()
BOT_KEY = web.AppKey("bot", object)
TOKEN_KEY = web.AppKey("api_token", str)


def _get_cog(request: web.Request):
    bot: commands.Bot = request.app[BOT_KEY]
    cog = bot.get_cog("MusicCog")
    if cog is None:
        raise web.HTTPServiceUnavailable(text="MusicCog not loaded")
    return cog


@web.middleware
async def require_token(request: web.Request, handler):
    if request.path != "/health":
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {request.app[TOKEN_KEY]}"
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise web.HTTPUnauthorized(text="A valid bearer token is required")
    return await handler(request)


def _get_guild(request: web.Request):
    try:
        guild_id = int(request.match_info["guild_id"])
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid guild ID")
    guild = request.app[BOT_KEY].get_guild(guild_id)
    if guild is None:
        raise web.HTTPNotFound(text="Guild not found")
    return guild


# ── Health ───────────────────────────────────────────────────────────────

@routes.get("/health")
async def health(request: web.Request) -> web.Response:
    bot = request.app[BOT_KEY]
    return web.json_response({
        "status": "ok",
        "guilds": len(bot.guilds),
        "shards": bot.shard_count or 1,
    })


# ── Queue ────────────────────────────────────────────────────────────────

@routes.get("/api/guilds/{guild_id}/queue")
async def get_queue(request: web.Request) -> web.Response:
    cog = _get_cog(request)
    guild_id = _get_guild(request).id
    gq = cog.queues.get(guild_id)

    def _track(t):
        return {"title": t.title, "url": t.url, "duration": t.duration,
                "requester": t.requester}

    data = {
        "current": _track(gq.current) if gq.current else None,
        "queue": [_track(t) for t in gq.queue],
        "volume": gq.volume,
        "loop_mode": gq.loop_mode.name,
        "filter": gq.filter_name,
        "autoplay": gq.autoplay,
        "radio_mode": gq.radio_mode,
    }
    return web.json_response(data)


@routes.post("/api/guilds/{guild_id}/skip")
async def skip(request: web.Request) -> web.Response:
    cog = _get_cog(request)
    guild_id = _get_guild(request).id
    bot = request.app[BOT_KEY]
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise web.HTTPNotFound(text="Guild not found")
    vc = guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        cog._skip(guild)
        return web.json_response({"status": "skipped"})
    raise web.HTTPBadRequest(text="Nothing is playing")


@routes.post("/api/guilds/{guild_id}/volume")
async def set_volume(request: web.Request) -> web.Response:
    cog = _get_cog(request)
    guild_id = _get_guild(request).id
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="Invalid JSON body")
    level = body.get("level") if isinstance(body, dict) else None
    if type(level) is not int or not 1 <= level <= 100:
        raise web.HTTPBadRequest(text="Volume must be 1-100")

    gq = cog.queues.get(guild_id)
    gq.volume = level / 100

    bot = request.app[BOT_KEY]
    guild = bot.get_guild(guild_id)
    if guild:
        vc = guild.voice_client
        if vc and vc.source and hasattr(vc.source, "volume"):
            vc.source.volume = gq.volume

    cog.queues.save_settings()
    return web.json_response({"volume": level})


@routes.get("/api/guilds/{guild_id}/playlists")
async def get_playlists(request: web.Request) -> web.Response:
    cog = _get_cog(request)
    guild_id = _get_guild(request).id
    playlists = cog.playlists.list_all(guild_id)
    result = []
    for pl in playlists:
        result.append({
            "name": pl["name"],
            "track_count": len(pl.get("tracks", [])),
            "created_by": pl.get("created_by", ""),
        })
    return web.json_response(result)


@routes.get("/api/guilds/{guild_id}/stats")
async def get_stats(request: web.Request) -> web.Response:
    cog = _get_cog(request)
    guild_id = _get_guild(request).id
    data = cog.history.server_stats(guild_id)
    # Convert Counter tuples to serializable format
    data["top_tracks"] = [{"title": t, "count": c} for t, c in data["top_tracks"]]
    data["top_users"] = [{"user_id": u, "count": c} for u, c in data["top_users"]]
    return web.json_response(data)


# ── Server lifecycle ─────────────────────────────────────────────────────

def create_app(bot: commands.Bot, token: str) -> web.Application:
    if not token or not token.strip():
        raise ValueError("WEB_API_TOKEN must be set to enable the web API")
    app = web.Application(middlewares=[require_token])
    app[BOT_KEY] = bot
    app[TOKEN_KEY] = token
    app.router.add_routes(routes)
    return app


async def start_web_server(bot: commands.Bot, port: int = 8080) -> web.AppRunner:
    app = create_app(bot, os.getenv("WEB_API_TOKEN", ""))
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, os.getenv("WEB_HOST", "127.0.0.1"), port)
        await site.start()
    except Exception:
        await runner.cleanup()
        raise
    return runner
