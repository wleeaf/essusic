"""Generate an offline gallery from the bot's real embed and component builders.

Run from the repository root: python scripts/preview_ui.py
No credentials, network calls, or optional packages are required.
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cogs.music_cog import (
    HelpView,
    PlayerView,
    QueueView,
    SearchView,
    _HELP_CATEGORIES,
    _build_help_embed,
)  # noqa: E402
from music.audio_source import TrackInfo  # noqa: E402
from music.presentation import (  # noqa: E402
    PagesView,
    card,
    collection_pages,
    lyrics_pages,
    notice,
    player_card,
    request_card,
    search_card,
    stats_card,
    track_row,
    vote_card,
)
from music.queue_manager import GuildQueue  # noqa: E402


async def samples():
    queue = GuildQueue()
    queue.current = TrackInfo(
        "Midnight Drive",
        "https://example.com/midnight",
        248,
        artist="North Arcade",
        requester="Maya",
    )
    titles = [
        "Slow Motion",
        "After the Rain",
        "Velvet Skies",
        "The Long Way Home",
        "Golden Hour",
        "Soft Landing",
        "City Lights",
        "Still Here",
    ]
    for i, title in enumerate(titles):
        queue.add(
            TrackInfo(
                title,
                f"https://example.com/track/{i}",
                198 + i * 13,
                requester=["Maya", "Alex", "Deniz"][i % 3],
                artist="North Arcade",
            )
        )
    queue.normalize = True
    queue.crossfade_seconds = 4
    cog = SimpleNamespace(
        queues=SimpleNamespace(get=lambda _: queue), _get_elapsed=lambda _: 93
    )
    guild = SimpleNamespace(id=1, voice_client=SimpleNamespace(is_paused=lambda: False))
    player = PlayerView(cog, guild)
    player._sync_pause_button()
    queue_view = QueueView(queue)
    tracks = list(queue.queue)[:5]
    search = SearchView(tracks, cog, None)
    result = []

    def add(name, description, embeds, view=None):
        if not isinstance(embeds, list):
            embeds = [embeds]
        result.append(
            {
                "name": name,
                "description": description,
                "pages": [embed.to_dict() for embed in embeds],
                "components": view.to_components() if view else [],
            }
        )

    add(
        "Player",
        "The main listening surface: clear transport controls, seeking, and up next.",
        player._build_embed(),
        player,
    )
    add(
        "Queue",
        "Six tracks per page, original track positions, and a current-track summary.",
        [QueueView(queue, i).build_embed() for i in range(queue_view.total_pages)],
        queue_view,
    )
    add(
        "Search",
        "Scannable results with a compact selection menu.",
        search_card(tracks, "late-night drive", "YouTube"),
        search,
    )
    help_view = HelpView()
    add(
        "Command guide",
        "A three-step welcome and a section for every command group.",
        [_build_help_embed("overview")]
        + [_build_help_embed(cid) for _, cid, _ in _HELP_CATEGORIES],
        help_view,
    )
    favorites = collection_pages(
        "Maya · Favorites",
        [track_row(t, i + 1) for i, t in enumerate(queue.queue)],
        section="Your library",
        footer="8/50 saved · /playfavs to listen · /unfav to remove",
    )
    add(
        "Favorites",
        "A personal library with consistent numbering across pages.",
        favorites,
        PagesView(favorites),
    )
    playlists = collection_pages(
        "The Listening Room · Playlists",
        [
            "**Late-night drive**\n24 tracks · By Maya · 2 collaborators",
            "**Sunday, slowly**\n18 tracks · By Deniz · 1 collaborator",
            "**On repeat**\n12 tracks · By Alex · 0 collaborators",
        ],
        section="Server library",
        footer="/playlist load to listen · /playlist save to keep a queue",
    )
    add("Playlists", "Ownership and collaboration details without clutter.", playlists)
    lyrics = lyrics_pages(
        "Midnight Drive",
        "North Arcade",
        "[Original preview text]\n\nThe street is quiet, the windows glow\nWe take the road we’ve come to know\n\nA little farther, a little slow\nA song to follow as we go",
    )
    add(
        "Lyrics",
        "Line breaks stay intact; long lyrics are paged in the same message.",
        lyrics,
    )
    data = {
        "total_plays": 428,
        "unique_tracks": 86,
        "total_time_seconds": 100080,
        "top_tracks": [
            ("Midnight Drive", 32),
            ("Slow Motion", 24),
            ("Velvet Skies", 19),
        ],
        "top_users": [(1, 164), (2, 143), (3, 121)],
    }
    add(
        "Server stats",
        "Key figures first, followed by tracks and listeners.",
        stats_card(
            data, "The Listening Room", listeners={1: "Maya", 2: "Alex", 3: "Deniz"}
        ),
    )
    add(
        "Your stats",
        "The same layout for a personal listening history.",
        stats_card(data, "Maya"),
    )
    add(
        "Charts & ratings",
        "A consistent reading order for rankings and community votes.",
        collection_pages(
            "The Listening Room · Crowd favorites",
            [
                "`01` **Midnight Drive**\n18 likes · 2 dislikes · **+16** score",
                "`02` **Velvet Skies**\n14 likes · 1 dislike · **+13** score",
            ],
            section="Top rated",
            footer="Ranked by likes minus dislikes · /rate to cast your vote",
        ),
    )
    add(
        "DJ requests",
        "Pending and resolved requests have distinct, explicit states.",
        [
            request_card(queue.current),
            request_card(
                queue.current, state="Approved", detail="Added at position 3."
            ),
            request_card(
                queue.current,
                state="Request expired",
                detail="Use /play to request it again.",
            ),
        ],
    )
    add(
        "Listener votes",
        "Visible progress keeps the decision easy to follow.",
        [
            vote_card(queue.current, 2, 4),
            vote_card(queue.current, 4, 4, state="Vote passed · Skipping"),
        ],
    )
    add(
        "Settings & feedback",
        "A common style for confirmations, limits, and actionable errors.",
        [
            notice("Volume set to **70%**.", section="Sound settings"),
            notice(
                "Per-user queue limit set to **5 tracks**.", section="Server settings"
            ),
            notice(
                "❌ You need to be in a voice channel. Join one, then try `/play` again.",
                section="Playback",
            ),
            notice(
                "No favorites yet. Use `/fav` to save the current track.",
                section="Your library",
            ),
        ],
    )
    empty = GuildQueue()
    add(
        "Empty, paused & live",
        "Distinct states, including a real 0:00 timestamp.",
        [player_card(empty), player_card(queue, elapsed=93, paused=True)],
    )
    queue.current = TrackInfo(
        "Night radio", "https://example.com/live", is_live=True, requester="Maya"
    )
    result[-1]["pages"].append(player_card(queue).to_dict())
    add(
        "Saved track",
        "Track links and metadata stay readable in DMs.",
        card(
            title="Midnight Drive",
            section="Saved for later",
            description="North Arcade",
            footer="Shared from Essusic · Paste the track link into /play to listen again",
        ),
    )
    for view in (player, queue_view, search, help_view):
        view.stop()
    return result


def main():
    data = asyncio.run(samples())
    template = Path(__file__).with_name("ui_preview.html").read_text()
    html = template.replace(
        "/* SAMPLE_DATA */ []",
        json.dumps(data, ensure_ascii=False).replace("</", "<\\/"),
    )
    target = Path("build/ui-preview.html")
    target.parent.mkdir(exist_ok=True)
    target.write_text(html)
    print(target.resolve())


if __name__ == "__main__":
    main()
