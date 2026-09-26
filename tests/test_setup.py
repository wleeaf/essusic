import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from music.credentials import CredentialError, CredentialStore
from music.providers import MediaService
from web.app import create_app
from web.setup import SetupPortal
from test_sources import COOKIES, KEY


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = CredentialStore(Path(self.tmp.name), KEY)
        self.guild = SimpleNamespace(id=1, owner_id=100, name="Owner’s server")
        self.other = SimpleNamespace(id=2, owner_id=200, name="Another server")
        self.cog = SimpleNamespace(
            credentials=self.store,
            media=MediaService(self.store),
            revoke_source_playback=AsyncMock(),
        )
        self.bot = SimpleNamespace(
            get_guild=lambda gid: {1: self.guild, 2: self.other}.get(gid),
            get_cog=lambda name: self.cog,
            guilds=[self.guild, self.other],
            shard_count=1,
        )
        self.portal = SetupPortal(self.bot, self.cog, "http://localhost:8080")
        self.client = TestClient(
            TestServer(create_app(self.bot, "", self.portal)),
            cookie_jar=CookieJar(unsafe=True),
        )
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.headers = {"Origin": self.portal.origin}

    async def login(self, guild=1, owner=100):
        token = self.portal.issue(guild, owner).split("#")[1]
        response = await self.client.post(
            "/setup/session", json={"token": token}, headers=self.headers
        )
        self.assertEqual(response.status, 200)
        response = await self.client.get("/setup/status")
        status = await response.json()
        self.headers["X-CSRF-Token"] = status["csrf"]
        return token

    async def test_public_page_has_security_headers_but_no_server_data(self):
        response = await self.client.get("/setup")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn(
            "frame-ancestors 'none'", response.headers["Content-Security-Policy"]
        )
        self.assertNotIn(self.guild.name, await response.text())
        self.assertEqual((await self.client.get("/setup/status")).status, 401)
        self.assertEqual(
            (
                await self.client.get(
                    "/api/guilds/1/queue", headers={"Authorization": "Bearer "}
                )
            ).status,
            401,
        )

    async def test_owner_verification_one_use_grant_and_cookie_flags(self):
        with self.assertRaises(CredentialError):
            self.portal.issue(1, 200)
        token = await self.login()
        response = await self.client.post(
            "/setup/session", json={"token": token}, headers=self.headers
        )
        self.assertEqual(response.status, 401)
        self.assertNotIn(token, str(self.portal.grants))
        cookie = next(iter(self.client.session.cookie_jar))
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertEqual(cookie["path"], "/setup")
        self.assertEqual(
            (await (await self.client.get("/setup/status")).json())["guild"]["id"], "1"
        )

    async def test_expired_grants_and_sessions_are_rejected(self):
        token = self.portal.issue(1, 100).split("#")[1]
        self.portal.grants[self.portal.digest(token)]["expires"] = time.monotonic() - 1
        self.assertEqual(
            (
                await self.client.post(
                    "/setup/session", json={"token": token}, headers=self.headers
                )
            ).status,
            401,
        )
        await self.login()
        next(iter(self.portal.sessions.values()))["expires"] = time.monotonic() - 1
        self.assertEqual((await self.client.get("/setup/status")).status, 401)

    async def test_new_link_revokes_old_session_and_transfer_revokes_new_session(self):
        await self.login()
        self.portal.issue(1, 100)
        self.assertEqual((await self.client.get("/setup/status")).status, 401)
        await self.login()
        self.guild.owner_id = 300
        self.assertEqual((await self.client.get("/setup/status")).status, 401)
        self.assertFalse(self.portal.sessions)

    async def test_cross_origin_and_csrf_mutations_are_rejected(self):
        token = self.portal.issue(1, 100).split("#")[1]
        response = await self.client.post(
            "/setup/session",
            json={"token": token},
            headers={"Origin": "https://evil.test"},
        )
        self.assertEqual(response.status, 403)
        await self.login()
        for headers in (
            {},
            {"Origin": self.portal.origin},
            {
                "Origin": "https://evil.test",
                "X-CSRF-Token": self.headers["X-CSRF-Token"],
            },
        ):
            response = await self.client.put(
                "/setup/sources/youtube", json={"cookies": COOKIES}, headers=headers
            )
            self.assertEqual(response.status, 403)
        self.assertIsNone(self.store.source(1, "youtube"))

    async def test_save_test_remove_are_guild_scoped_and_never_return_credentials(self):
        await self.login()
        response = await self.client.put(
            "/setup/sources/youtube",
            json={"cookies": COOKIES, "guild_id": 2},
            headers=self.headers,
        )
        self.assertEqual(response.status, 200)
        self.assertNotIn("guild-one-secret", await response.text())
        self.assertIsNone(self.store.source(2, "youtube"))
        self.assertEqual(self.store.read(1)["owner_id"], 100)
        self.cog.revoke_source_playback.assert_awaited_once_with(1)
        self.cog.media.test = AsyncMock(return_value={"success": True, "message": "ok"})
        response = await self.client.post(
            "/setup/sources/youtube/test", json={}, headers=self.headers
        )
        self.assertEqual(response.status, 200)
        self.cog.media.test.assert_awaited_once_with(1, "youtube")
        self.assertEqual(
            (
                await self.client.post(
                    "/setup/sources/youtube/test", json={}, headers=self.headers
                )
            ).status,
            429,
        )
        response = await self.client.delete(
            "/setup/sources/youtube", headers=self.headers
        )
        self.assertEqual(response.status, 200)
        self.assertIsNone(self.store.source(1, "youtube"))

    async def test_bad_requests_are_safe_and_bounded(self):
        await self.login()
        for body in (
            [],
            None,
            {},
            {"cookies": "bad"},
            {"cookies": "x" * (128 * 1024 + 1)},
        ):
            response = await self.client.put(
                "/setup/sources/youtube", json=body, headers=self.headers
            )
            self.assertEqual(response.status, 400)
        response = await self.client.put(
            "/setup/sources/youtube",
            data='{"cookies":"' + "x" * (300 * 1024) + '"}',
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status, 413)
        self.assertEqual(
            (
                await self.client.put(
                    "/setup/sources/unknown", json={}, headers=self.headers
                )
            ).status,
            404,
        )

    async def test_discord_command_rejects_admin_who_is_not_owner(self):
        from cogs.music_cog import MusicCog
        from unittest.mock import Mock

        cog = MusicCog.__new__(MusicCog)
        cog.setup_portal = Mock()
        interaction = SimpleNamespace(
            guild=self.guild,
            user=SimpleNamespace(id=200),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        await MusicCog.setup_sources.callback(cog, interaction)
        cog.setup_portal.issue.assert_not_called()
        self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])

    async def test_logout_revokes_access(self):
        await self.login()
        self.assertEqual(
            (await self.client.delete("/setup/session", headers=self.headers)).status,
            200,
        )
        self.assertEqual((await self.client.get("/setup/status")).status, 401)

    def test_public_url_requires_tls_except_loopback_and_storage_requires_key(self):
        for url in (
            "http://example.com",
            "https://user:pass@example.com",
            "https://example.com/setup",
            "https://example.com?key=x",
        ):
            with self.assertRaises(ValueError):
                SetupPortal(self.bot, self.cog, url)
        self.assertTrue(SetupPortal(self.bot, self.cog, "https://example.com").secure)
        self.cog.credentials = CredentialStore(Path(self.tmp.name), "")
        with self.assertRaises(ValueError):
            SetupPortal(self.bot, self.cog, "https://example.com")
