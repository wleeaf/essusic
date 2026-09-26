import re
from enum import Enum, auto
from urllib.parse import parse_qs, urlsplit


class InputType(Enum):
    YOUTUBE_URL = auto()
    YOUTUBE_PLAYLIST = auto()
    SPOTIFY_TRACK = auto()
    SPOTIFY_PLAYLIST = auto()
    SPOTIFY_ALBUM = auto()
    SOUNDCLOUD_URL = auto()
    SOUNDCLOUD_PLAYLIST = auto()
    RADIO_STREAM = auto()
    SEARCH_QUERY = auto()


_STREAM_RE = re.compile(
    r"(?:https?://)\S+\.(?:m3u8?|pls|aac|mp3|ogg|opus)(?:\?\S*)?"
    r"|(?:https?://)\S+(?:/stream|/live|/radio)\S*", re.IGNORECASE)


def classify(query: str) -> tuple[InputType, str]:
    """Return (InputType, cleaned_value) for a user query.

    For YouTube URLs the cleaned value is the original URL.
    For Spotify URLs it's the Spotify ID.
    For search queries it's the original string.
    """
    query = query.strip()

    try:
        parsed = urlsplit(query if "://" in query else "https://" + query)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return InputType.SEARCH_QUERY, query
    if parsed.scheme not in ("http", "https"):
        return InputType.SEARCH_QUERY, query

    if host == "open.spotify.com":
        match = re.fullmatch(r"/(?:intl-[^/]+/)?(track|playlist|album)/([A-Za-z0-9]+)/?", parsed.path)
        if match:
            kind, spotify_id = match.groups()
            return {
                "track": InputType.SPOTIFY_TRACK,
                "playlist": InputType.SPOTIFY_PLAYLIST,
                "album": InputType.SPOTIFY_ALBUM,
            }[kind], spotify_id

    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}:
        if "list" in parse_qs(parsed.query) or parsed.path.startswith("/browse/"):
            return InputType.YOUTUBE_PLAYLIST, query
        return InputType.YOUTUBE_URL, query

    if host in {"soundcloud.com", "www.soundcloud.com"}:
        if "/sets/" in parsed.path:
            return InputType.SOUNDCLOUD_PLAYLIST, query
        return InputType.SOUNDCLOUD_URL, query

    if _STREAM_RE.fullmatch(query):
        return InputType.RADIO_STREAM, query

    return InputType.SEARCH_QUERY, query
