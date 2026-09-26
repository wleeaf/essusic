"""Private, short-lived setup sessions issued through an owner-only slash command."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from music.credentials import CredentialError
from music.providers import SourceError

STATIC = Path(__file__).parent / "static"
COOKIE = "essusic_setup"


class SetupPortal:
    def __init__(self, bot, cog, base_url: str):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or (
                parsed.scheme == "http"
                and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise ValueError(
                "WEB_BASE_URL must be an HTTPS origin (HTTP is allowed only on localhost)."
            )
        if not cog.credentials.enabled:
            raise ValueError("CREDENTIALS_KEY is required for owner setup.")
        self.bot, self.cog = bot, cog
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        default_port = 443 if parsed.scheme == "https" else 80
        self.origin = f"{parsed.scheme}://{host}" + (
            f":{port}" if port and port != default_port else ""
        )
        self.secure = parsed.scheme == "https"
        self.grants = {}
        self.sessions = {}
        self.test_times = {}

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def purge(self):
        now = time.monotonic()
        for collection in (self.grants, self.sessions):
            for key, value in list(collection.items()):
                if value["expires"] <= now:
                    del collection[key]
        self.test_times = {
            key: value for key, value in self.test_times.items() if value > now
        }

    def revoke(self, guild_id):
        for collection in (self.grants, self.sessions):
            for key, value in list(collection.items()):
                if value["guild_id"] == guild_id:
                    del collection[key]

    def issue(self, guild_id, owner_id):
        self.purge()
        guild = self.bot.get_guild(guild_id)
        if guild is None or guild.owner_id != owner_id:
            raise CredentialError("Only the current server owner can open setup.")
        self.revoke(guild_id)
        if len(self.grants) + len(self.sessions) >= 10000:
            raise CredentialError("Setup is busy. Try again shortly.")
        token = secrets.token_urlsafe(32)
        self.grants[self.digest(token)] = {
            "guild_id": guild_id,
            "owner_id": owner_id,
            "expires": time.monotonic() + 300,
        }
        return f"{self.origin}/setup#{token}"

    def verify_owner(self, session):
        guild = self.bot.get_guild(session["guild_id"])
        if guild is None or guild.owner_id != session["owner_id"]:
            self.revoke(session["guild_id"])
            raise web.HTTPUnauthorized(
                text="Setup expired. The current owner must run /setup again."
            )
        return guild

    def session(self, request, *, mutation=False):
        self.purge()
        session = self.sessions.get(self.digest(request.cookies.get(COOKIE, "")))
        if not session:
            raise web.HTTPUnauthorized(
                text="Setup expired. Run /setup in Discord for a new link."
            )
        self.verify_owner(session)
        if mutation:
            self.check_origin(request)
            if not hmac.compare_digest(
                request.headers.get("X-CSRF-Token", "").encode(),
                session["csrf"].encode(),
            ):
                raise web.HTTPForbidden(text="Refresh setup and try again.")
        return session

    def check_origin(self, request):
        if request.headers.get("Origin") != self.origin:
            raise web.HTTPForbidden(text="Open setup using the link from Discord.")

    async def body(self, request):
        if request.content_type != "application/json":
            raise web.HTTPBadRequest(text="A JSON request is required.")
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="Invalid request.") from None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Invalid request.")
        return body

    async def exchange(self, request):
        self.check_origin(request)
        body = await self.body(request)
        token = body.get("token")
        if not isinstance(token, str) or len(token) > 128:
            raise web.HTTPUnauthorized(text="Invalid setup link.")
        self.purge()
        grant = self.grants.pop(self.digest(token), None)
        if not grant:
            raise web.HTTPUnauthorized(
                text="This link expired or was already used. Run /setup again."
            )
        self.verify_owner(grant)
        session_token = secrets.token_urlsafe(32)
        self.sessions[self.digest(session_token)] = {
            **grant,
            "expires": time.monotonic() + 900,
            "csrf": secrets.token_urlsafe(32),
        }
        response = web.json_response({"success": True})
        response.set_cookie(
            COOKIE,
            session_token,
            max_age=900,
            httponly=True,
            secure=self.secure,
            samesite="Strict",
            path="/setup",
        )
        return response

    async def status(self, request):
        session = self.session(request)
        guild = self.verify_owner(session)
        return web.json_response(
            {
                "guild": {"id": str(guild.id), "name": guild.name},
                "sources": self.cog.credentials.status(guild.id),
                "csrf": session["csrf"],
            }
        )

    async def source(self, request):
        session = self.session(request, mutation=True)
        provider = request.match_info["provider"]
        if provider not in {"youtube", "spotify"}:
            raise web.HTTPNotFound(text="Unknown source.")
        guild_id = session["guild_id"]
        if request.method == "DELETE":
            self.cog.credentials.delete(guild_id, provider)
        else:
            body = await self.body(request)
            self.cog.credentials.save(
                guild_id, provider, body, owner_id=session["owner_id"]
            )
        self.cog.media.invalidate(guild_id)
        # Changing YouTube credentials also revokes any already-resolved audio.
        if provider == "youtube":
            await self.cog.revoke_source_playback(guild_id)
        return web.json_response({"sources": self.cog.credentials.status(guild_id)})

    async def test(self, request):
        session = self.session(request, mutation=True)
        provider = request.match_info["provider"]
        if provider not in {"youtube", "spotify"}:
            raise web.HTTPNotFound(text="Unknown source.")
        key = (session["guild_id"], provider)
        if self.test_times.get(key, 0) > time.monotonic():
            raise web.HTTPTooManyRequests(
                text="Wait a few seconds before checking again."
            )
        self.test_times[key] = time.monotonic() + 15
        result = await self.cog.media.test(session["guild_id"], provider)
        self.session(
            request, mutation=True
        )  # ownership/session may change while the provider runs
        return web.json_response(result)

    async def logout(self, request):
        self.session(request, mutation=True)
        self.sessions.pop(self.digest(request.cookies.get(COOKIE, "")), None)
        response = web.json_response({"success": True})
        response.del_cookie(COOKIE, path="/setup")
        return response

    def register(self, app):
        app.router.add_get("/setup", self.page)
        for filename in ("setup.js", "setup.css"):
            app.router.add_get("/setup/" + filename, self.asset)
        app.router.add_post("/setup/session", self.exchange)
        app.router.add_get("/setup/status", self.status)
        app.router.add_delete("/setup/session", self.logout)
        app.router.add_put("/setup/sources/{provider}", self.source)
        app.router.add_delete("/setup/sources/{provider}", self.source)
        app.router.add_post("/setup/sources/{provider}/test", self.test)

    async def page(self, request):
        return web.Response(
            text=(STATIC / "setup.html").read_text(), content_type="text/html"
        )

    async def asset(self, request):
        filename = request.path.rsplit("/", 1)[-1]
        return web.Response(
            text=(STATIC / filename).read_text(),
            content_type="text/javascript" if filename.endswith(".js") else "text/css",
        )


@web.middleware
async def setup_security(request, handler):
    if request.path != "/setup" and not request.path.startswith("/setup/"):
        return await handler(request)
    try:
        response = await handler(request)
    except (CredentialError, SourceError) as exc:
        response = web.json_response({"message": str(exc)}, status=400)
    except web.HTTPException as exc:
        response = web.json_response({"message": exc.text}, status=exc.status)
    except Exception:
        # Do not put request bodies or third-party responses in logs or HTML errors.
        response = web.json_response(
            {"message": "Setup could not complete the request. Try again."}, status=500
        )
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        }
    )
    return response
