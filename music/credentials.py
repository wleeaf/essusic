"""Encrypted, guild-bound source credentials. No process-wide fallback credentials."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox

from .config import data_path

MAX_COOKIE_BYTES = 128 * 1024
AUTH_COOKIES = {
    "SAPISID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "SID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "LOGIN_INFO",
}


class CredentialError(ValueError):
    """A deliberately safe, user-facing credential error."""


def validate_cookies(text: str) -> str:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_COOKIE_BYTES:
        raise CredentialError("Choose a Netscape cookie file smaller than 128 KiB.")
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() not in {
        "# Netscape HTTP Cookie File",
        "# HTTP Cookie File",
    }:
        raise CredentialError("The file must use Netscape HTTP Cookie File format.")
    cleaned = ["# Netscape HTTP Cookie File"]
    authenticated = False
    for line in lines[1:]:
        if not line.strip() or (
            line.startswith("#") and not line.startswith("#HttpOnly_")
        ):
            continue
        entry = line.removeprefix("#HttpOnly_").split("\t")
        if len(entry) != 7:
            raise CredentialError("The cookie file contains an invalid entry.")
        domain, subdomains, path, secure, expiry, name, value = entry
        host = domain.lstrip(".").lower()
        if host != "youtube.com" and not host.endswith(".youtube.com"):
            raise CredentialError(
                "Export cookies for youtube.com only, not your entire browser."
            )
        if (
            subdomains not in {"TRUE", "FALSE"}
            or secure not in {"TRUE", "FALSE"}
            or domain.startswith(".") != (subdomains == "TRUE")
            or not path.startswith("/")
            or not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,20})?", expiry)
            or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise CredentialError("The cookie file contains an invalid entry.")
        expires_at = Decimal(expiry)
        if expires_at and expires_at <= time.time():
            continue
        if name in AUTH_COOKIES and value:
            authenticated = True
        # Browser exporters can retain fractional epoch seconds. yt-dlp's
        # Netscape loader requires integer seconds; round down without ever
        # turning an expired fractional timestamp into a session cookie.
        entry[4] = str(int(expires_at))
        prefix = "#HttpOnly_" if line.startswith("#HttpOnly_") else ""
        cleaned.append(prefix + "\t".join(entry))
        if len(cleaned) > 251:
            raise CredentialError("The cookie file contains too many entries.")
    if not authenticated:
        raise CredentialError(
            "No unexpired YouTube sign-in cookies were found. Export a fresh signed-in session."
        )
    return "\n".join(cleaned) + "\n"


def validate_spotify(client_id: str, client_secret: str) -> dict:
    if not isinstance(client_id, str) or not re.fullmatch(
        r"[a-zA-Z0-9]{16,128}", client_id
    ):
        raise CredentialError("Enter a valid Spotify client ID.")
    if (
        not isinstance(client_secret, str)
        or not 16 <= len(client_secret) <= 256
        or not client_secret.isascii()
        or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in client_secret)
    ):
        raise CredentialError("Enter a valid Spotify client secret.")
    return {"client_id": client_id, "client_secret": client_secret}


class CredentialStore:
    def __init__(self, directory: Path | None = None, key: str | None = None):
        self.directory = (
            Path(directory) if directory is not None else data_path("credentials")
        )
        key = os.getenv("CREDENTIALS_KEY", "") if key is None else key
        self._box = None
        if key:
            try:
                decoded = base64.b64decode(key, altchars=b"-_", validate=True)
                self._box = SecretBox(decoded)
            except (ValueError, TypeError):
                raise CredentialError(
                    "CREDENTIALS_KEY must encode exactly 32 random bytes."
                ) from None

    @property
    def enabled(self) -> bool:
        return self._box is not None

    def _path(self, guild_id: int) -> Path:
        if type(guild_id) is not int or guild_id <= 0:
            raise CredentialError("Invalid server.")
        return self.directory / f"{guild_id}.enc"

    def read(self, guild_id: int) -> dict:
        path = self._path(guild_id)
        if not path.exists():
            return {"guild_id": guild_id, "sources": {}}
        if not self._box:
            raise CredentialError(
                "Source storage is locked. Ask the bot operator to configure its encryption key."
            )
        try:
            result = json.loads(self._box.decrypt(path.read_bytes()))
            if result["guild_id"] != guild_id or not isinstance(
                result["sources"], dict
            ):
                raise ValueError
            return result
        except (CryptoError, ValueError, KeyError, TypeError, OSError):
            raise CredentialError(
                "Source storage could not be opened. Ask the bot operator to check its encryption key."
            ) from None

    def _write(self, guild_id: int, data: dict) -> None:
        if not self._box:
            raise CredentialError(
                "The bot operator must configure CREDENTIALS_KEY before setup."
            )
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        encrypted = self._box.encrypt(json.dumps(data).encode())
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".credentials-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path(guild_id))
        finally:
            Path(tmp).unlink(missing_ok=True)

    def save(
        self, guild_id: int, provider: str, value: dict, *, owner_id: int | None = None
    ) -> None:
        if provider == "youtube":
            value = {"cookies": validate_cookies(value.get("cookies"))}
        elif provider == "spotify":
            value = validate_spotify(value.get("client_id"), value.get("client_secret"))
        else:
            raise CredentialError("Unknown source.")
        data = self.read(guild_id)
        if owner_id is not None:
            data["owner_id"] = owner_id
        data["sources"][provider] = {
            **value,
            "revision": secrets.token_hex(16),
            "updated_at": int(time.time()),
            "state": "configured",
            "checked_at": None,
        }
        self._write(guild_id, data)

    def source(self, guild_id: int, provider: str) -> dict | None:
        return self.read(guild_id)["sources"].get(provider)

    def revision(self, guild_id: int, provider: str) -> str | None:
        source = self.source(guild_id, provider)
        return source["revision"] if source else None

    def checked(
        self, guild_id: int, provider: str, revision: str, success: bool
    ) -> None:
        data = self.read(guild_id)
        source = data["sources"].get(provider)
        # A check running during replacement/deletion cannot overwrite new state.
        if source and source["revision"] == revision:
            source.update(
                state="ready" if success else "attention", checked_at=int(time.time())
            )
            self._write(guild_id, data)

    def delete(self, guild_id: int, provider: str | None = None) -> None:
        if provider is None:
            self._path(guild_id).unlink(missing_ok=True)
            return
        if provider not in {"youtube", "spotify"}:
            raise CredentialError("Unknown source.")
        data = self.read(guild_id)
        data["sources"].pop(provider, None)
        if data["sources"]:
            self._write(guild_id, data)
        else:
            self._path(guild_id).unlink(missing_ok=True)

    def status(self, guild_id: int) -> dict:
        sources = self.read(guild_id)["sources"]
        return {
            name: {
                key: sources[name].get(key)
                for key in ("state", "updated_at", "checked_at")
            }
            if name in sources
            else {"state": "missing", "updated_at": None, "checked_at": None}
            for name in ("youtube", "spotify")
        }
