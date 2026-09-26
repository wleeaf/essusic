"""Shared presentation for Discord messages and controls."""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

import discord

ACCENT = 0xA99BFF
SUCCESS = 0x74D3B0
WARNING = 0xE8BE78
ERROR = 0xED929C
MUTED = 0x9298A8


def shorten(value: str, limit: int = 180) -> str:
    value = str(value)
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def plain(value: str, limit: int = 180) -> str:
    """Keep user/media metadata on one line without interpreting its Markdown."""
    return discord.utils.escape_markdown(shorten(" ".join(str(value).split()), limit))


def duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return (
        f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    )


def track_duration(track) -> str:
    return (
        "LIVE"
        if track.is_live
        else duration(track.duration)
        if track.duration > 0
        else "Unknown length"
    )


def progress(elapsed: int, total: int, length: int = 16) -> str:
    elapsed = max(0, elapsed)
    if total <= 0:
        return f"`{duration(elapsed)}` · Duration unavailable"
    elapsed = min(elapsed, total)
    marker = round((length - 1) * elapsed / total)
    bar = "━" * marker + "●" + "─" * (length - marker - 1)
    return f"`{duration(elapsed)}` {bar} `{duration(total)}`"


def media_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return quote(value, safe=":/?&=#%+-_.~@")
    except ValueError:
        pass
    return None


def track_link(track, limit: int = 100) -> str:
    title = plain(track.title, limit)
    url = media_url(track.url)
    # Very long signed URLs should not consume the embed's text budget.
    return f"[{title}]({url})" if url and len(url) <= 400 else title


def card(
    *,
    title: str,
    description: str = "",
    color=ACCENT,
    url=None,
    section: str = "Music",
    footer: str = "",
) -> discord.Embed:
    # Remove the older decorative emoji prefixes; hierarchy carries the label.
    title = re.sub(r"^[^\w`]+", "", title).strip()
    embed = discord.Embed(
        title=shorten(title, 256),
        description=shorten(description, 4000) or None,
        color=color,
        url=media_url(url) if url else None,
    )
    embed.set_author(name=f"ESSUSIC  /  {section.upper()}")
    embed.set_footer(text=shorten(footer or "Your server. Your soundtrack.", 300))
    return embed


def notice(message: str, *, section: str = "Music") -> discord.Embed:
    message = str(message)
    error = message.startswith(
        ("❌", "Invalid", "Cannot", "Could not", "Failed", "Only admins", "You need")
    )
    warning = message.startswith(
        ("No ", "Nothing", "Not ", "Already", "Queue is full", "Requires", "Too many")
    )
    clean = re.sub(r"^[^\w`*]+", "", message).strip()
    return card(
        title="Check this first" if error or warning else section,
        description=clean,
        section=section,
        color=ERROR if error else WARNING if warning else ACCENT,
    )


def track_row(track, position: int, *, detail: str = "") -> str:
    meta = detail or (
        f"{track_duration(track)} · Added by {plain(track.requester or 'Radio', 45)}"
    )
    return f"`{position:02d}` **{track_link(track)}**\n{meta}"


def player_card(gq, *, elapsed: int = 0, paused: bool = False) -> discord.Embed:
    track = gq.current
    if track is None:
        return card(
            title="Ready when you are",
            section="Player",
            color=MUTED,
            description="Join a voice channel, then use `/play` to start your soundtrack.",
            footer="Search a song, paste a link, or load a saved playlist.",
        )
    status = "Paused" if paused else "Live now" if track.is_live else "Now playing"
    timeline = (
        "● LIVE · Streaming" if track.is_live else progress(elapsed, track.duration)
    )
    embed = card(
        title=track.title,
        url=track.url,
        section=status,
        description=(f"{plain(track.artist, 100)}\n\n" if track.artist else "")
        + timeline,
        color=WARNING if paused else ACCENT,
        footer=(
            "Live stream · /queue to explore what’s next"
            if track.is_live
            else "Use the time buttons to seek · /queue to explore what’s next"
            if track.duration > 0
            else "Use /queue to explore what’s next"
        ),
    )
    if media_url(track.thumbnail):
        embed.set_thumbnail(url=track.thumbnail)
    embed.add_field(name="Added by", value=plain(track.requester or "Radio", 80))
    embed.add_field(name="Volume", value=f"{round(gq.volume * 100)}%")
    embed.add_field(
        name="Repeat",
        value={"OFF": "Off", "SINGLE": "This track", "QUEUE": "Queue"}[
            gq.loop_mode.name
        ],
    )
    modes = []
    if gq.filter_name:
        modes.append(gq.filter_name.replace("_", " ").title())
    if gq.speed != 1:
        modes.append(f"{gq.speed:g}× speed")
    if gq.normalize:
        modes.append("Normalized")
    if any(gq.eq_bands):
        modes.append("Custom EQ")
    if gq.crossfade_seconds:
        modes.append(f"{gq.crossfade_seconds}s crossfade")
    if gq.autoplay:
        modes.append("Autoplay")
    if gq.radio_mode:
        modes.append("Radio")
    if modes:
        embed.add_field(name="Sound & session", value=" · ".join(modes), inline=False)
    if gq.queue:
        embed.add_field(
            name=f"Up next · {len(gq.queue)} queued",
            value=f"**{track_link(gq.queue[0])}**\n{track_duration(gq.queue[0])}",
            inline=False,
        )
    else:
        embed.add_field(
            name="Up next",
            value="The queue is open. Add a song with `/play`.",
            inline=False,
        )
    return embed


def search_card(results, query: str, provider: str) -> discord.Embed:
    rows = [
        track_row(track, i + 1, detail=track_duration(track))
        for i, track in enumerate(results[:5])
    ]
    return card(
        title="Find your next track",
        section=provider,
        description=f"Results for **{plain(query, 120)}**\n\n" + "\n\n".join(rows),
        footer=f"{len(rows)} results · Choose a track below to add it to the queue",
    )


class PagesView(discord.ui.View):
    """One message for a collection, with visible page position and expiry."""

    def __init__(self, pages: list[discord.Embed]):
        super().__init__(timeout=180)
        self.pages = pages
        self.page = 0
        self.message = None
        self.sync()

    def sync(self):
        self.previous.disabled = self.page == 0
        self.next_page.disabled = self.page == len(self.pages) - 1
        self.position.label = f"{self.page + 1} / {len(self.pages)}"

    @discord.ui.button(label="Previous", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page = max(0, self.page - 1)
        self.sync()
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

    @discord.ui.button(
        label="1 / 1", disabled=True, style=discord.ButtonStyle.secondary
    )
    async def position(self, interaction, button):
        pass

    @discord.ui.button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        self.page = min(len(self.pages) - 1, self.page + 1)
        self.sync()
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def collection_pages(
    title: str, rows: list[str], *, section: str, footer: str = "", per_page: int = 6
) -> list[discord.Embed]:
    pages = []
    # Bound each row so even unusually long metadata fits into a Discord embed.
    rows = [shorten(row, 550) for row in rows]
    for start in range(0, len(rows), per_page):
        pages.append(
            card(
                title=title,
                description="\n\n".join(rows[start : start + per_page]),
                section=section,
                footer=footer,
            )
        )
    return pages or [
        card(
            title=title, section=section, description="Nothing here yet.", footer=footer
        )
    ]


async def send_pages(
    interaction, pages: list[discord.Embed], *, ephemeral: bool = False
):
    view = PagesView(pages) if len(pages) > 1 else None
    options = {"embed": pages[0], "ephemeral": ephemeral}
    if view is not None:
        options["view"] = view
    if interaction.response.is_done():
        message = await interaction.followup.send(**options, wait=True)
    else:
        await interaction.response.send_message(**options)
        message = await interaction.original_response() if view else None
    if view:
        view.message = message


def stats_card(
    data: dict, name: str, *, listeners: dict[int, str] | None = None
) -> discord.Embed:
    embed = card(
        title=f"{name} · Listening history",
        section="Your stats" if listeners is None else "Server stats",
        description="A look at what’s been on repeat.",
        footer="Based on the latest 500 playback starts · Hours use full track durations",
    )
    embed.add_field(name="Plays", value=f"**{data['total_plays']:,}**")
    embed.add_field(
        name="Track hours", value=f"**{data['total_time_seconds'] / 3600:.1f}**"
    )
    if "unique_tracks" in data:
        embed.add_field(name="Unique tracks", value=f"**{data['unique_tracks']:,}**")
    if data.get("top_tracks"):
        embed.add_field(
            name="On repeat",
            inline=False,
            value="\n".join(
                f"`{i + 1:02d}` {plain(title, 80)} · **{count}** plays"
                for i, (title, count) in enumerate(data["top_tracks"][:5])
            ),
        )
    if listeners is not None and data.get("top_users"):
        embed.add_field(
            name="Regular listeners",
            inline=False,
            value="\n".join(
                f"`{i + 1:02d}` {plain(listeners.get(uid, 'Listener'), 60)} · **{count}** plays"
                for i, (uid, count) in enumerate(data["top_users"][:5])
            ),
        )
    return embed


def lyrics_pages(title: str, artist: str, text: str) -> list[discord.Embed]:
    chunks = []
    while text:
        cut = text[:3200]
        if len(text) > 3200:
            newline = cut.rfind("\n")
            if newline > 1600:
                cut = cut[:newline]
        chunks.append(cut)
        text = text[len(cut) :].lstrip("\n")
    return [
        card(
            title=title,
            section="Lyrics",
            description=chunk,
            footer=f"{shorten(artist, 100) or 'Lyrics'} · {i + 1}/{len(chunks)} · Source: LRCLIB",
        )
        for i, chunk in enumerate(chunks)
    ]


def request_card(
    track, *, state: str = "Waiting for a DJ", detail: str = ""
) -> discord.Embed:
    color = (
        SUCCESS
        if state == "Approved"
        else WARNING
        if state == "Waiting for a DJ"
        else MUTED
    )
    embed = card(
        title=state,
        section="Track request",
        color=color,
        description=f"**{track_link(track)}**\n{track_duration(track)}",
        footer=detail
        or "A DJ can approve or decline this track below · Expires in 5 minutes",
    )
    embed.add_field(
        name="Requested by",
        value=plain(track.requester or "Listener", 80),
        inline=False,
    )
    return embed


def vote_card(
    track, count: int, required: int, *, state: str = "Skip this track?"
) -> discord.Embed:
    return card(
        title=state,
        section="Listener vote",
        description=(f"**{track_link(track)}**\n\n" if track else "")
        + f"**{count} / {required}** votes\n"
        + "●" * min(count, required, 20)
        + "○" * min(max(0, required - count), 20),
        footer="One vote per listener · Voting closes after 60 seconds",
        color=SUCCESS if count >= required else ACCENT,
    )
