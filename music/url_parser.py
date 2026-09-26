import re
from enum import Enum, auto
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit


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
    r"|(?:https?://)\S+(?:/stream|/live|/radio)\S*",
    re.IGNORECASE,
)


def classify(query: str) -> tuple[InputType, str]:
    """Return (InputType, cleaned_value) for a user query.

    YouTube video URLs have incidental playlist/radio context removed.
    Explicit playlist URLs retain their original value.
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
        match = re.fullmatch(
            r"/(?:intl-[^/]+/)?(track|playlist|album)/([A-Za-z0-9]+)/?", parsed.path
        )
        if match:
            kind, spotify_id = match.groups()
            return {
                "track": InputType.SPOTIFY_TRACK,
                "playlist": InputType.SPOTIFY_PLAYLIST,
                "album": InputType.SPOTIFY_ALBUM,
            }[kind], spotify_id

    if host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }:
        params = parse_qs(parsed.query)
        video_selected = (
            (parsed.path.rstrip("/") == "/watch" and bool(params.get("v", [""])[0]))
            or (
                host in {"youtu.be", "www.youtu.be"}
                and bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", parsed.path.strip("/")))
            )
            or bool(
                re.fullmatch(r"/(?:shorts|live|embed)/[A-Za-z0-9_-]{11}/?", parsed.path)
            )
        )
        if video_selected:
            # Shared song links carry the sender's playlist (e.g. list=LM).
            # A selected video takes priority; /playlist and /browse still
            # explicitly request the collection.
            video_query = urlencode(
                [
                    (key, value)
                    for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                    if key not in {"list", "index", "start_radio"}
                ]
            )
            return InputType.YOUTUBE_URL, urlunsplit(
                (
                    parsed.scheme,
                    "www.youtube.com" if host == "music.youtube.com" else parsed.netloc,
                    parsed.path,
                    video_query,
                    parsed.fragment,
                )
            )
        if "list" in params or parsed.path.startswith("/browse/"):
            return InputType.YOUTUBE_PLAYLIST, query
        return InputType.YOUTUBE_URL, query

    if host in {"soundcloud.com", "www.soundcloud.com"}:
        if "/sets/" in parsed.path:
            return InputType.SOUNDCLOUD_PLAYLIST, query
        return InputType.SOUNDCLOUD_URL, query

    if _STREAM_RE.fullmatch(query):
        return InputType.RADIO_STREAM, query

    return InputType.SEARCH_QUERY, query
