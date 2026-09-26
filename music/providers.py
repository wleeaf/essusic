"""All media requests use an explicit guild and isolated credentials."""

from __future__ import annotations

import asyncio
import io
import time

import yt_dlp

from .audio_source import YTDL_OPTIONS
from .credentials import CredentialError, CredentialStore, validate_cookies
from .spotify_resolver import SpotifyResolver
from .url_parser import InputType, classify

YOUTUBE_TEST_URL = "https://www.youtube.com/watch?v=BaW_jenozKc"


class SourceError(ValueError):
    """Safe error text; never exposes provider responses or secrets."""


class QuietLogger:
    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


def requires_youtube(query: str) -> bool:
    return classify(query)[0] not in {
        InputType.SOUNDCLOUD_URL,
        InputType.SOUNDCLOUD_PLAYLIST,
        InputType.RADIO_STREAM,
    }


class MediaService:
    def __init__(self, credentials: CredentialStore, guild_lookup=None):
        self.credentials = credentials
        self.guild_lookup = guild_lookup
        self._spotify = {}
        self._locks = {}
        self._global_slots = asyncio.Semaphore(4)
        self._next_request = {}
        self._failures = {}

    def _source(self, guild_id: int, provider: str):
        data = self.credentials.read(guild_id)
        if self.guild_lookup is not None:
            guild = self.guild_lookup(guild_id)
            if guild is None or data.get("owner_id", guild.owner_id) != guild.owner_id:
                raise SourceError(
                    "Server ownership changed. The current owner must run /setup."
                )
        return data["sources"].get(provider)

    def ensure_source(self, guild_id: int, query: str) -> None:
        if requires_youtube(query) and not self._source(guild_id, "youtube"):
            raise SourceError(
                "YouTube is not configured for this server. Ask the server owner to run /setup."
            )

    def spotify(self, guild_id: int) -> SpotifyResolver:
        source = self._source(guild_id, "spotify")
        revision = source["revision"] if source else None
        cached = self._spotify.get(guild_id)
        if cached is None or cached[0] != revision:
            resolver = (
                SpotifyResolver(source["client_id"], source["client_secret"])
                if source
                else SpotifyResolver()
            )
            self._spotify[guild_id] = (revision, resolver)
        return self._spotify[guild_id][1]

    def invalidate(self, guild_id: int) -> None:
        self._spotify.pop(guild_id, None)
        self._next_request.pop(guild_id, None)
        self._failures.pop(guild_id, None)

    async def extract(self, guild_id: int, query: str, *, playlist=False, flat=False):
        lock = self._locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            self.ensure_source(guild_id, query)
            source = (
                self._source(guild_id, "youtube") if requires_youtube(query) else None
            )
            revision = source["revision"] if source else None
            if source:
                try:
                    cookies = validate_cookies(source["cookies"])
                except CredentialError:
                    self.credentials.checked(guild_id, "youtube", revision, False)
                    raise SourceError(
                        "YouTube cookies need attention. Ask the server owner to replace them in /setup."
                    ) from None
            else:
                cookies = None
            remaining = self._next_request.get(guild_id, 0) - time.monotonic()
            if remaining > 1:
                raise SourceError(
                    "This server’s music source is cooling down after a failed request. Try again shortly."
                )
            if remaining > 0:
                await asyncio.sleep(remaining)
            async with self._global_slots:
                # A queued request must not start with credentials revoked while it waited.
                if source:
                    self.ensure_source(guild_id, query)
                    if self.credentials.revision(guild_id, "youtube") != revision:
                        raise SourceError(
                            "YouTube configuration changed during this request. Please try again."
                        )
                try:
                    data = await asyncio.to_thread(
                        self._extract, query, cookies, playlist, flat
                    )
                except Exception:
                    failures = self._failures.get(guild_id, 0) + 1
                    self._failures[guild_id] = failures
                    self._next_request[guild_id] = time.monotonic() + min(
                        60, 5 * 2 ** min(failures - 1, 4)
                    )
                    if source:
                        self.credentials.checked(guild_id, "youtube", revision, False)
                    raise SourceError(
                        "The source could not complete this request. Try another track; the owner can check the connection in /setup."
                    ) from None
            self._next_request[guild_id] = time.monotonic() + 1
            self._failures.pop(guild_id, None)
            if source:
                self.ensure_source(guild_id, query)
            if source and self.credentials.revision(guild_id, "youtube") != revision:
                raise SourceError(
                    "YouTube configuration changed during this request. Please try again."
                )
            return data

    @staticmethod
    def _extract(query: str, cookies: str | None, playlist: bool, flat: bool):
        # File-like cookie input stays in memory and is unique to this request.
        options = {
            **YTDL_OPTIONS,
            "cookiefile": io.StringIO(cookies) if cookies else None,
            "cookiesfrombrowser": None,
            "cachedir": False,
            "logger": QuietLogger(),
            "noplaylist": not playlist,
            "socket_timeout": 20,
            "retries": 1,
            "extractor_retries": 1,
            "playlistend": 200,
        }
        if flat:
            options["extract_flat"] = "in_playlist"
        with yt_dlp.YoutubeDL(options) as extractor:
            data = extractor.extract_info(query, download=False)
            if data and "entries" in data:
                data["entries"] = list(data["entries"] or [])
            return data

    async def test(self, guild_id: int, provider: str) -> dict:
        source = self._source(guild_id, provider)
        if not source:
            raise SourceError(f"{provider.title()} is not configured for this server.")
        revision = source["revision"]
        try:
            if provider == "youtube":
                data = await self.extract(guild_id, YOUTUBE_TEST_URL)
                success = bool(data and data.get("url"))
            elif provider == "spotify":
                resolver = self.spotify(guild_id)
                success = await asyncio.to_thread(resolver.test_connection)
            else:
                raise SourceError("Unknown source.")
        except Exception:
            success = False
        if self.credentials.revision(guild_id, provider) != revision:
            raise SourceError(
                "Configuration changed during the check. Test the new connection again."
            )
        self._source(guild_id, provider)
        self.credentials.checked(guild_id, provider, revision, success)
        return {
            "success": success,
            "message": (
                "Sample playback resolved. This does not guarantee future availability."
                if provider == "youtube"
                else "Spotify accepted this server’s application credentials."
            )
            if success
            else "The connection check failed. Check the credentials and try again; source restrictions may also apply.",
        }
