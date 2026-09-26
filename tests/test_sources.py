import asyncio
import base64
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from music.credentials import CredentialError, CredentialStore, validate_cookies
from music.providers import MediaService, SourceError
from music.spotify_resolver import SpotifyResolver

KEY = base64.b64encode(b"k" * 32).decode()
COOKIES = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tguild-one-secret\n"


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = CredentialStore(Path(self.tmp.name) / "credentials", KEY)

    def test_encrypted_private_storage_is_bound_to_guild(self):
        self.store.save(1, "youtube", {"cookies": COOKIES}, owner_id=100)
        path = self.store.directory / "1.enc"
        self.assertNotIn(b"guild-one-secret", path.read_bytes())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.read(1)["owner_id"], 100)
        self.assertEqual(self.store.source(1, "youtube")["cookies"], COOKIES)
        self.assertIsNone(self.store.source(2, "youtube"))
        (path.parent / "2.enc").write_bytes(path.read_bytes())
        with self.assertRaises(CredentialError):
            self.store.read(2)
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        with self.assertRaises(CredentialError):
            self.store.read(1)

    def test_key_required_and_bad_key_rejected(self):
        with self.assertRaises(CredentialError):
            CredentialStore(key="bad-key")
        with self.assertRaises(CredentialError):
            CredentialStore(self.store.directory, "").save(
                1, "youtube", {"cookies": COOKIES}
            )
        self.store.save(1, "youtube", {"cookies": COOKIES})
        with self.assertRaises(CredentialError):
            CredentialStore(
                self.store.directory, base64.b64encode(b"x" * 32).decode()
            ).read(1)

    def test_cookie_validation_rejects_unrelated_expired_and_malformed_files(self):
        for text in [
            "",
            "not cookies",
            COOKIES.replace("youtube.com", "google.com"),
            COOKIES.replace("youtube.com", "youtube.com.evil.test"),
            COOKIES.replace("\t0\t", "\t1\t"),
            COOKIES.replace("SAPISID", "PREF"),
            COOKIES.replace("\tTRUE\t/", "\tFALSE\t/"),
            COOKIES + "x" * (128 * 1024),
        ]:
            with self.subTest(text=text[:30]), self.assertRaises(CredentialError):
                validate_cookies(text)
        self.assertIn(
            "#HttpOnly_",
            validate_cookies(COOKIES.replace(".youtube.com", "#HttpOnly_.youtube.com")),
        )

    def test_fractional_expiry_is_normalized_for_ytdlp_without_changing_cookies(self):
        from yt_dlp.cookies import YoutubeDLCookieJar

        expiry = int(time.time()) + 3600
        exported = COOKIES.replace("\t0\t", f"\t{expiry}.123456\t").replace(
            ".youtube.com", "#HttpOnly_.youtube.com"
        )
        normalized = validate_cookies(exported)
        self.assertEqual(normalized, exported.replace(f"{expiry}.123456", str(expiry)))
        jar = YoutubeDLCookieJar(io.StringIO(normalized))
        jar.load(ignore_discard=True, ignore_expires=True)
        cookie = next(iter(jar))
        self.assertEqual(cookie.expires, expiry)
        self.assertEqual(cookie.value, "guild-one-secret")
        self.assertEqual(cookie.domain, ".youtube.com")
        self.assertTrue(cookie.secure)

    def test_fractional_expired_values_cannot_become_session_cookies(self):
        for expiry in ("0.5", "1.999999", str(int(time.time()) - 10) + ".9"):
            with (
                self.subTest(expiry=expiry),
                self.assertRaisesRegex(CredentialError, "unexpired"),
            ):
                validate_cookies(COOKIES.replace("\t0\t", f"\t{expiry}\t"))
        self.assertEqual(validate_cookies(COOKIES.replace("\t0\t", "\t0.0\t")), COOKIES)

    def test_malformed_or_unbounded_expiry_is_rejected(self):
        for expiry in (
            "NaN",
            "Infinity",
            "-1",
            "1e12",
            "1.2.3",
            "9999999999999",
            "0." + "1" * 21,
        ):
            with (
                self.subTest(expiry=expiry),
                self.assertRaisesRegex(CredentialError, "invalid entry"),
            ):
                validate_cookies(COOKIES.replace("\t0\t", f"\t{expiry}\t"))

    def test_status_never_exposes_secrets_and_stale_test_cannot_mark_new_source_ready(
        self,
    ):
        self.store.save(1, "youtube", {"cookies": COOKIES})
        revision = self.store.revision(1, "youtube")
        self.store.save(1, "youtube", {"cookies": COOKIES.replace("one", "new")})
        self.store.checked(1, "youtube", revision, True)
        status = self.store.status(1)
        self.assertEqual(status["youtube"]["state"], "configured")
        self.assertNotIn("secret", str(status))
        self.assertNotIn("cookies", str(status))
        self.store.delete(1, "youtube")
        self.store.checked(1, "youtube", revision, True)
        self.assertEqual(self.store.status(1)["youtube"]["state"], "missing")
        self.assertFalse((self.store.directory / "1.enc").exists())

    def test_removing_one_provider_preserves_other_guild_and_source(self):
        for guild in (1, 2):
            self.store.save(guild, "youtube", {"cookies": COOKIES})
            self.store.save(
                guild, "spotify", {"client_id": "a" * 32, "client_secret": "b" * 32}
            )
        self.store.delete(1, "youtube")
        self.assertIsNotNone(self.store.source(1, "spotify"))
        self.assertIsNotNone(self.store.source(2, "youtube"))
        self.store.delete(1)
        self.assertIsNone(self.store.source(1, "spotify"))


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = CredentialStore(Path(self.tmp.name), KEY)
        self.media = MediaService(self.store)

    async def test_missing_guild_credentials_never_fall_back_to_another_guild(self):
        self.store.save(1, "youtube", {"cookies": COOKIES})
        with patch.object(self.media, "_extract") as extract:
            for query in (
                "hello",
                "ytsearch:hello",
                "https://www.youtube.com/watch?v=test",
                "https://www.youtube.com/playlist?list=test",
            ):
                with self.assertRaises(SourceError):
                    await self.media.extract(2, query)
            extract.assert_not_called()

    async def test_concurrent_guild_requests_receive_only_their_own_cookies(self):
        self.store.save(1, "youtube", {"cookies": COOKIES})
        self.store.save(2, "youtube", {"cookies": COOKIES.replace("one", "two")})
        with patch.object(
            self.media, "_extract", return_value={"url": "audio"}
        ) as extract:
            await asyncio.gather(
                self.media.extract(1, "first"), self.media.extract(2, "second")
            )
        calls = {call.args[0]: call.args[1] for call in extract.call_args_list}
        self.assertIn("one-secret", calls["first"])
        self.assertNotIn("two-secret", calls["first"])
        self.assertIn("two-secret", calls["second"])

    async def test_plaintext_stays_in_memory_and_global_cache_is_disabled(self):
        extractor = Mock()
        extractor.extract_info.return_value = {"url": "audio"}
        with patch("music.providers.yt_dlp.YoutubeDL") as factory:
            factory.return_value.__enter__.return_value = extractor
            self.media._extract("hello", COOKIES, False, False)
        options = factory.call_args.args[0]
        self.assertEqual(options["cookiefile"].getvalue(), COOKIES)
        self.assertIsNone(options["cookiesfrombrowser"])
        self.assertFalse(options["cachedir"])

    async def test_deleted_credentials_waiting_for_global_slot_never_start_request(
        self,
    ):
        self.store.save(1, "youtube", {"cookies": COOKIES})
        self.media._global_slots = asyncio.Semaphore(0)
        with patch.object(self.media, "_extract") as extract:
            task = asyncio.create_task(self.media.extract(1, "hello"))
            await asyncio.sleep(0)
            self.store.delete(1, "youtube")
            self.media._global_slots.release()
            with self.assertRaises(SourceError):
                await task
            extract.assert_not_called()

    async def test_owner_change_is_enforced_before_gateway_listener_cleanup(self):
        self.store.save(1, "youtube", {"cookies": COOKIES}, owner_id=100)
        self.media.guild_lookup = lambda gid: SimpleNamespace(owner_id=200)
        with patch.object(self.media, "_extract") as extract:
            with self.assertRaisesRegex(SourceError, "ownership changed"):
                await self.media.extract(1, "hello")
            extract.assert_not_called()

    async def test_cookie_replacement_during_fetch_discards_old_result(self):
        self.store.save(1, "youtube", {"cookies": COOKIES})
        entered, finish = asyncio.Event(), asyncio.Event()

        async def fetch(*args):
            entered.set()
            await finish.wait()
            return {"url": "old-session-audio"}

        with patch("music.providers.asyncio.to_thread", side_effect=fetch):
            task = asyncio.create_task(self.media.extract(1, "hello"))
            await entered.wait()
            self.store.delete(1, "youtube")
            finish.set()
            with self.assertRaises(SourceError):
                await task

    async def test_failures_are_redacted_and_back_off(self):
        self.store.save(1, "youtube", {"cookies": COOKIES})
        with patch.object(
            self.media, "_extract", side_effect=RuntimeError("sensitive-provider-body")
        ) as extract:
            with self.assertRaises(SourceError) as error:
                await self.media.extract(1, "hello")
            self.assertNotIn("sensitive", str(error.exception))
            with self.assertRaisesRegex(SourceError, "cooling down"):
                await self.media.extract(1, "hello")
            self.assertEqual(extract.call_count, 1)
        self.assertEqual(self.store.status(1)["youtube"]["state"], "attention")

    async def test_soundcloud_does_not_receive_youtube_cookies(self):
        self.store.save(1, "youtube", {"cookies": COOKIES})
        with patch.object(
            self.media, "_extract", return_value={"url": "audio"}
        ) as extract:
            await self.media.extract(1, "https://soundcloud.com/artist/track")
        self.assertIsNone(extract.call_args.args[1])

    async def test_expired_cookies_are_rejected_before_network(self):
        self.store.save(
            1,
            "youtube",
            {"cookies": COOKIES.replace("\t0\t", f"\t{int(time.time()) + 100}\t")},
        )
        with (
            patch("music.credentials.time.time", return_value=time.time() + 200),
            patch.object(self.media, "_extract") as extract,
        ):
            with self.assertRaisesRegex(SourceError, "attention"):
                await self.media.extract(1, "hello")
            extract.assert_not_called()

    def test_spotify_never_reads_global_credentials_and_uses_separate_memory_caches(
        self,
    ):
        with patch.dict(
            os.environ,
            {
                "SPOTIFY_CLIENT_ID": "operator",
                "SPOTIFY_CLIENT_SECRET": "operator",
                "SPOTIPY_CLIENT_ID": "operator",
                "SPOTIPY_CLIENT_SECRET": "operator",
            },
        ):
            self.assertFalse(SpotifyResolver().available)
            for guild in (1, 2):
                self.store.save(
                    guild,
                    "spotify",
                    {"client_id": str(guild) * 32, "client_secret": "s" * 32},
                )
            first, second = self.media.spotify(1), self.media.spotify(2)
            self.assertIsNot(
                first._sp.auth_manager.cache_handler,
                second._sp.auth_manager.cache_handler,
            )
            first._sp.auth_manager.cache_handler.save_token_to_cache(
                {"access_token": "private"}
            )
            self.assertIsNone(second._sp.auth_manager.cache_handler.get_cached_token())
            self.assertFalse(self.media.spotify(3).available)
            self.store.delete(1, "spotify")
            self.assertFalse(self.media.spotify(1).available)

    async def test_owner_transfer_revokes_credentials_sessions_and_playback(self):
        from cogs.music_cog import MusicCog

        cog = MusicCog.__new__(MusicCog)
        cog.credentials, cog.media = self.store, self.media
        cog.setup_portal = Mock()
        cog.revoke_source_playback = AsyncMock()
        self.store.save(1, "youtube", {"cookies": COOKIES}, owner_id=100)
        await cog.on_guild_available(SimpleNamespace(id=1, owner_id=200))
        self.assertIsNone(self.store.source(1, "youtube"))
        cog.setup_portal.revoke.assert_called_once_with(1)
        cog.revoke_source_playback.assert_awaited_once_with(1)
