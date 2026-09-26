from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
import time
from dataclasses import replace
from typing import Optional
from urllib.parse import parse_qs, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from music.audio_source import (
    EQ_PRESETS,
    CrossfadeSource,
    TrackInfo,
    YTDLSource,
)
from music.metrics import (
    active_players as metric_active_players,
    playback_errors_total,
    queue_size as metric_queue_size,
    tracks_played_total,
    voice_connections as metric_voice_connections,
)
from music.queue_manager import (
    FavoritesManager,
    GuildQueue,
    HistoryManager,
    LoopMode,
    PlaylistManager,
    QueueManager,
    RatingsManager,
)
from music.credentials import CredentialError, CredentialStore
from music.providers import MediaService, SourceError, requires_youtube
from music.url_parser import InputType, classify

from music.presentation import (
    card,
    collection_pages,
    duration,
    lyrics_pages,
    notice,
    plain,
    player_card,
    progress,
    request_card,
    search_card,
    send_pages,
    shorten,
    stats_card,
    track_duration,
    track_link,
    track_row,
    vote_card,
)

log = logging.getLogger(__name__)


_YT_JUNK_RE = re.compile(
    r"\s*[\(\[]"
    r"(?:official\s*(?:music\s*)?(?:video|audio|lyric\s*video|visualizer)?|"
    r"lyrics?|hd|hq|4k|8k|remaster(?:ed)?|explicit|clean|radio\s*edit|"
    r"full\s*(?:song|album)|feat\.?[^)\]]*|ft\.?[^)\]]*|prod\.?[^)\]]*|"
    r"extended\s*(?:version|mix)?|original\s*(?:version|mix)?|topic)"
    r"[\)\]]",
    re.IGNORECASE,
)
_YT_TOPIC_RE = re.compile(r"\s*-\s*topic\s*$", re.IGNORECASE)


def _clean_title(title: str) -> str:
    """Strip common YouTube noise from track titles for lyrics search."""
    title = _YT_TOPIC_RE.sub("", title)
    title = _YT_JUNK_RE.sub("", title)
    return title.strip(" -–—|")


def format_duration(seconds: int) -> str:
    return duration(seconds)


def progress_bar(elapsed: int, total: int, length: int = 16) -> str:
    return progress(elapsed, total, length)


def parse_time(value: str) -> int | None:
    """Parse '90', '1:30', or '1:30:00' into seconds. Returns None on failure."""
    parts = value.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def _check_dj(interaction: discord.Interaction, gq: GuildQueue) -> str | None:
    """Return None if the user is authorized, or an error message string."""
    if gq.dj_role_id is None:
        return None
    member = interaction.user
    if member.guild_permissions.administrator:  # type: ignore[union-attr]
        return None
    if any(r.id == gq.dj_role_id for r in member.roles):  # type: ignore[union-attr]
        return None
    # Allow if user is alone with bot in VC
    if member.voice and member.voice.channel:  # type: ignore[union-attr]
        non_bot = [m for m in member.voice.channel.members if not m.bot]  # type: ignore[union-attr]
        vc = interaction.guild.voice_client
        if vc and member.voice.channel == vc.channel and len(non_bot) <= 1:
            return None
    role = interaction.guild.get_role(gq.dj_role_id)  # type: ignore[union-attr]
    role_name = role.name if role else "DJ"
    return f"You need the **{role_name}** role to use this command."


class SearchView(discord.ui.View):
    """Compact track selection menu for search results."""

    def __init__(
        self, results: list[TrackInfo], cog: MusicCog, interaction: discord.Interaction
    ) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.original_interaction = interaction

        self.results = results[:5]
        self.select = discord.ui.Select(
            placeholder="Choose a track to add to the queue…",
            options=[discord.SelectOption(
                label=shorten(f"{i + 1}. {track.title}", 100), value=str(i),
                description=shorten(f"{track_duration(track)} · {track.artist or 'Add to your server queue'}", 100),
            ) for i, track in enumerate(self.results)],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        track = self.results[int(self.select.values[0])]
        await self._make_callback(track)(interaction)

    def _make_callback(self, track: TrackInfo):
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            requested = replace(track, requester=interaction.user.display_name,
                                requester_id=interaction.user.id)
            await self.cog._enqueue_and_play(interaction, requested)

        return callback

    async def on_timeout(self) -> None:
        self.select.placeholder = "Search expired · run /search again"
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        try:
            await self.original_interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass


class MixConfirmView(discord.ui.View):
    """Asks the user whether to play a YouTube Mix or just the single video."""

    def __init__(self, cog: MusicCog, interaction: discord.Interaction, url: str) -> None:
        super().__init__(timeout=30)
        self.cog = cog
        self.original_interaction = interaction
        self.url = url

    @discord.ui.button(label="Play this track", emoji="▶️", style=discord.ButtonStyle.primary)
    async def play_video(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        self._disable_all()
        await self.original_interaction.edit_original_response(view=self)
        # Strip list= params to get just the video URL
        parsed = urlparse(self.url)
        params = parse_qs(parsed.query)
        video_id = params.get("v", [None])[0]
        if video_id:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
        else:
            video_url = self.url
        # Re-invoke play logic as a YouTube URL
        await self.cog._play_single_url(interaction, video_url)

    @discord.ui.button(label="Queue the mix", emoji="➕", style=discord.ButtonStyle.secondary)
    async def play_mix(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        self._disable_all()
        await self.original_interaction.edit_original_response(view=self)
        await self.cog._play_youtube_playlist(interaction, self.url)

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]

    async def on_timeout(self) -> None:
        self._disable_all()
        try:
            await self.original_interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass


class VoteSkipView(discord.ui.View):
    """Vote-skip button that tracks unique voters."""

    def __init__(self, cog: MusicCog, guild: discord.Guild, required: int) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.guild = guild
        self.required = required
        self.track = self.cog.queues.get(guild.id).current
        self.voters: set[int] = set()
        self.message: discord.Message | None = None

    @discord.ui.button(label="Skip (0/0)", style=discord.ButtonStyle.danger)
    async def vote(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.voters.add(interaction.user.id)
        # Sync a copy into the guild queue (don't share the same set object)
        gq = self.cog.queues.get(self.guild.id)
        gq.skip_votes = set(self.voters)

        count = len(self.voters)
        button.label = f"Skip ({count}/{self.required})"

        if count >= self.required:
            button.disabled = True
            await interaction.response.edit_message(
                content=None, embed=vote_card(self.track, count, self.required, state="Vote passed · Skipping"),
                view=self,
            )
            self.stop()
            vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
            if vc and (vc.is_playing() or vc.is_paused()):
                self.cog._skip(self.guild)
        else:
            await interaction.response.edit_message(embed=vote_card(self.track, count, self.required), view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        if self.message:
            try:
                await self.message.edit(content=None, embed=vote_card(self.track, len(self.voters), self.required, state="Vote closed"), view=self)
            except discord.HTTPException:
                pass


class RateView(discord.ui.View):
    """Thumbs up/down rating buttons for the current track."""

    def __init__(self, cog: MusicCog, guild_id: int, track_url: str, track_title: str) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.track_url = track_url
        self.track_title = track_title
        self.message = None
        up, down = self.cog.ratings.get_rating(guild_id, track_url)
        self.up_btn.label = f"Like · {up}"
        self.down_btn.label = f"Dislike · {down}"

    @discord.ui.button(label="Like · 0", emoji="👍", style=discord.ButtonStyle.secondary)
    async def up_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        up, down = self.cog.ratings.vote(
            self.guild_id, self.track_url, self.track_title,
            interaction.user.id, "up",
        )
        self.up_btn.label = f"Like · {up}"
        self.down_btn.label = f"Dislike · {down}"
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Dislike · 0", emoji="👎", style=discord.ButtonStyle.secondary)
    async def down_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        up, down = self.cog.ratings.vote(
            self.guild_id, self.track_url, self.track_title,
            interaction.user.id, "down",
        )
        self.up_btn.label = f"Like · {up}"
        self.down_btn.label = f"Dislike · {down}"
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class DJApprovalView(discord.ui.View):
    """Approve/reject a track request in DJ queue mode."""

    def __init__(self, cog: MusicCog, guild: discord.Guild, track: TrackInfo) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.track = track
        self.message: discord.Message | None = None

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gq = self.cog.queues.get(self.guild.id)
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Requests'), ephemeral=True)
            return
        # Guard against double-click or approve-after-reject race
        if self.track not in gq.pending_requests:
            await interaction.response.send_message(embed=notice("This request was already handled.", section='Requests'), ephemeral=True)
            return
        gq.pending_requests.remove(self.track)
        pos = gq.add(self.track)
        if pos is None:
            await interaction.response.send_message(embed=notice("Queue is full.", section='Requests'), ephemeral=True)
            return
        self._disable_all()
        self.stop()
        await interaction.response.edit_message(
            content=None, embed=request_card(self.track, state="Approved", detail=f"Added to the queue at position {pos}."),
            view=self,
        )
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        if vc and not vc.is_playing() and not vc.is_paused():
            await self.cog._play_next(self.guild)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gq = self.cog.queues.get(self.guild.id)
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Requests'), ephemeral=True)
            return
        # Guard against double-click or reject-after-approve race
        if self.track not in gq.pending_requests:
            await interaction.response.send_message(embed=notice("This request was already handled.", section='Requests'), ephemeral=True)
            return
        gq.pending_requests.remove(self.track)
        self._disable_all()
        self.stop()
        await interaction.response.edit_message(
            content=None, embed=request_card(self.track, state="Declined", detail="This track was not added to the queue."),
            view=self,
        )

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]

    async def on_timeout(self) -> None:
        self._disable_all()
        gq = self.cog.queues.get(self.guild.id)
        if self.track in gq.pending_requests:
            gq.pending_requests.remove(self.track)
        if self.message:
            try:
                await self.message.edit(embed=request_card(self.track, state="Request expired",
                                        detail="No decision was made. Use /play to request it again."), view=self)
            except discord.HTTPException:
                pass


class PlayerView(discord.ui.View):
    """Interactive music player with controls, progress bar, and seek."""

    SEEK_SEGMENTS = 5

    def __init__(self, cog: MusicCog, guild: discord.Guild) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.guild = guild
        self.message: discord.Message | None = None
        self._update_task: asyncio.Task | None = None
        self._build_seek_bar()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if err := _check_dj(interaction, self.cog.queues.get(self.guild.id)):
            await interaction.response.send_message(embed=notice(err, section='Playback'), ephemeral=True)
            return False
        return True

    # ── Clickable seek bar (row 1) ────────────────────────────────────────

    def _build_seek_bar(self) -> None:
        """Add a row of buttons that act as a clickable progress bar."""
        gq = self.cog.queues.get(self.guild.id)
        if not gq.current or gq.current.is_live or gq.current.duration <= 0:
            return
        dur = gq.current.duration
        elapsed = self.cog._get_elapsed(gq)

        for i in range(self.SEEK_SEGMENTS):
            seg_start = int(dur * i / self.SEEK_SEGMENTS)
            seg_end = int(dur * (i + 1) / self.SEEK_SEGMENTS)
            is_current = seg_start <= elapsed < seg_end or (i == self.SEEK_SEGMENTS - 1 and elapsed >= seg_start)

            if is_current:
                label = format_duration(seg_start)
                style = discord.ButtonStyle.primary
            else:
                label = format_duration(seg_start)
                style = discord.ButtonStyle.secondary

            btn = discord.ui.Button(label=label, style=style, row=1)
            btn.callback = self._make_seek_cb(seg_start)
            self.add_item(btn)

    def _rebuild_seek_bar(self) -> None:
        """Remove old seek buttons and rebuild with updated position."""
        # Row 1 buttons are the seek bar — remove them
        to_remove = [c for c in self.children
                     if isinstance(c, discord.ui.Button) and c.row == 1]
        for c in to_remove:
            self.remove_item(c)
        self._build_seek_bar()

    def _make_seek_cb(self, secs: int):
        async def callback(interaction: discord.Interaction) -> None:
            gq = self.cog.queues.get(self.guild.id)
            if err := _check_dj(interaction, gq):
                await interaction.response.send_message(embed=notice(err, section='Playback'), ephemeral=True)
                return
            if gq.current is None:
                await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Playback'), ephemeral=True)
                return
            target = min(secs, max(0, gq.current.duration - 1)) if gq.current.duration else secs
            await interaction.response.defer()
            await self.cog._restart_playback(self.guild, seek_seconds=target)
            await asyncio.sleep(0.5)
            self._rebuild_seek_bar()
            await self._update_player()
        return callback

    def _build_embed(self) -> discord.Embed:
        gq = self.cog.queues.get(self.guild.id)
        vc = self.guild.voice_client
        return player_card(gq, elapsed=self.cog._get_elapsed(gq), paused=bool(vc and vc.is_paused()))

    def _sync_pause_button(self) -> None:
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        paused = vc is not None and vc.is_paused()
        self.pause_resume_btn.emoji = "▶️" if paused else "⏸️"
        self.pause_resume_btn.label = "Resume" if paused else "Pause"
        self.pause_resume_btn.style = discord.ButtonStyle.primary
        gq = self.cog.queues.get(self.guild.id)
        active = gq.current is not None and vc is not None
        seekable = active and not gq.current.is_live and gq.current.duration > 0
        self.rewind_btn.disabled = not seekable
        self.forward_btn.disabled = not seekable
        self.prev_btn.disabled = not active or gq.previous is None
        self.next_btn.disabled = not active
        self.pause_resume_btn.disabled = not active

    async def _auto_update(self) -> None:
        try:
            while not self.is_finished():
                await asyncio.sleep(10)
                if self.message is None:
                    break
                try:
                    await self._update_player()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("PlayerView auto-update failed: %s", exc)
        except asyncio.CancelledError:
            pass

    async def _update_player(self) -> None:
        embed = self._build_embed()
        self._sync_pause_button()
        self._rebuild_seek_bar()
        if self.message:
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

    async def on_timeout(self) -> None:
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        self.cog._active_players.pop(self.guild.id, None)

    # Row 0: transport controls

    @discord.ui.button(label="Back", emoji="\u23ee", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gq = self.cog.queues.get(self.guild.id)
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        if gq.previous is None:
            await interaction.response.send_message(embed=notice("No previous track.", section='Playback'), ephemeral=True)
            return
        track = gq.previous
        gq.previous = None
        gq.queue.appendleft(track)
        gq.current = None
        if vc:
            self.cog._skip(self.guild)
        await interaction.response.defer()

    @discord.ui.button(label="−10s", emoji="\u23ea", style=discord.ButtonStyle.secondary, row=0)
    async def rewind_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gq = self.cog.queues.get(self.guild.id)
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Playback'), ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        if vc is None or gq.current is None:
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Playback'), ephemeral=True)
            return
        elapsed = self.cog._get_elapsed(gq)
        seek_to = max(0, elapsed - 10)
        await interaction.response.defer()
        await self.cog._restart_playback(self.guild, seek_seconds=seek_to)
        await asyncio.sleep(0.5)
        await self._update_player()

    @discord.ui.button(label="Pause", emoji="\u23f8", style=discord.ButtonStyle.secondary, row=0)
    async def pause_resume_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        if vc is None:
            await interaction.response.send_message(embed=notice("Not connected.", section='Playback'), ephemeral=True)
            return
        if vc.is_paused():
            self.cog._resume(self.guild)
        elif vc.is_playing():
            self.cog._pause(self.guild)
        else:
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Playback'), ephemeral=True)
            return
        await interaction.response.defer()
        await self._update_player()

    @discord.ui.button(label="+10s", emoji="\u23e9", style=discord.ButtonStyle.secondary, row=0)
    async def forward_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gq = self.cog.queues.get(self.guild.id)
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Playback'), ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        if vc is None or gq.current is None:
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Playback'), ephemeral=True)
            return
        elapsed = self.cog._get_elapsed(gq)
        seek_to = elapsed + 10
        if gq.current.duration and seek_to >= gq.current.duration:
            await interaction.response.send_message(embed=notice("Already near the end.", section='Playback'), ephemeral=True)
            return
        await interaction.response.defer()
        await self.cog._restart_playback(self.guild, seek_seconds=seek_to)
        await asyncio.sleep(0.5)
        await self._update_player()

    @discord.ui.button(label="Skip", emoji="\u23ed", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gq = self.cog.queues.get(self.guild.id)
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Playback'), ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        if vc is None or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Playback'), ephemeral=True)
            return
        self.cog._skip(self.guild)
        await interaction.response.defer()

    # Row 2: volume controls

    @discord.ui.button(label="Softer", emoji="\U0001f509", style=discord.ButtonStyle.secondary, row=2)
    async def vol_down_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gq = self.cog.queues.get(self.guild.id)
        gq.volume = max(0.0, round(gq.volume - 0.1, 2))
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = gq.volume
        self.cog.queues.save_settings()
        await interaction.response.defer()
        await self._update_player()

    @discord.ui.button(label="Louder", emoji="\U0001f50a", style=discord.ButtonStyle.secondary, row=2)
    async def vol_up_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gq = self.cog.queues.get(self.guild.id)
        gq.volume = min(1.0, round(gq.volume + 0.1, 2))
        vc: Optional[discord.VoiceClient] = self.guild.voice_client  # type: ignore[assignment]
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = gq.volume
        self.cog.queues.save_settings()
        await interaction.response.defer()
        await self._update_player()


class QueueView(discord.ui.View):
    """Paginated queue display with navigation buttons."""

    PER_PAGE = 6

    def __init__(self, gq: GuildQueue, page: int = 0) -> None:
        super().__init__(timeout=120)
        self.gq = gq
        self.page = page
        self.message = None
        self._sync_buttons()

    @property
    def total_pages(self) -> int:
        return max(1, math.ceil(len(self.gq.queue) / self.PER_PAGE))

    def _sync_buttons(self) -> None:
        total = self.total_pages
        self.page = max(0, min(self.page, total - 1))  # clamp in case queue shrank
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= total - 1
        self.page_indicator.label = f"{self.page + 1} / {total}"

    def build_embed(self) -> discord.Embed:
        self._sync_buttons()
        gq = self.gq
        tracks = list(gq.queue)
        start = self.page * self.PER_PAGE
        rows = [track_row(track, i + 1) for i, track in enumerate(tracks[start:start + self.PER_PAGE], start)]
        known_duration = sum(track.duration for track in tracks if not track.is_live)
        embed = card(title="Your listening queue", section="Queue",
                     description="\n\n".join(rows) or "No upcoming tracks. Use `/play` to add something you love.",
                     footer=f"{len(tracks)} queued · {duration(known_duration)} known duration · Page {self.page + 1}/{self.total_pages}")
        if gq.current:
            embed.add_field(name="Playing now", value=f"**{track_link(gq.current)}** · {track_duration(gq.current)}", inline=False)
        return embed

    @discord.ui.button(label="Previous", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="1 / 1", disabled=True, style=discord.ButtonStyle.secondary)
    async def page_indicator(self, interaction, button):
        pass

    @discord.ui.button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


_HELP_CATEGORIES: list[tuple[str, str, list[tuple[str, str]]]] = [
    (
        "🎵 Playback",
        "playback",
        [
            ("/play", "Play from a YouTube/Spotify URL or search keywords"),
            ("/playnext", "Insert a single track to play immediately after the current one"),
            ("/skip", "Skip the current track and play the next one"),
            ("/stop", "Stop playback, clear queue, and disconnect"),
            ("/pause", "Pause playback"),
            ("/resume", "Resume playback"),
            ("/replay", "Restart the current track from the beginning"),
            ("/back", "Play the previous track"),
            ("/voteskip", "Start a skip vote — requires half the listeners to agree"),
        ],
    ),
    (
        "📋 Queue",
        "queue",
        [
            ("/queue", "Show the current queue"),
            ("/myqueue", "Show only the tracks you have in the queue"),
            ("/remove `<pos>`", "Remove a track by position"),
            ("/move `<from>` `<to>`", "Move a track to a different position"),
            ("/skipto `<pos>`", "Jump to a specific position in the queue"),
            ("/clear", "Clear the queue (keeps current track playing)"),
            ("/shuffle", "Shuffle the queue, spreading same-artist tracks evenly"),
            ("/undo", "Revert the last queue change"),
            ("/queue-export", "Export the queue as a shareable code"),
            ("/queue-import `<code>`", "Import a queue from an exported code"),
        ],
    ),
    (
        "🔊 Audio",
        "audio",
        [
            ("/volume `<1-100>`", "Adjust playback volume"),
            ("/filter", "Apply an audio filter (Bass Boost, Nightcore, Vaporwave, 8D, Karaoke)"),
            ("/seek `<time>`", "Seek to a position — absolute (`1:30`) or relative (`+30`, `-15`)"),
            ("/speed `<0.5-2.0>`", "Set playback speed"),
            ("/normalize", "Toggle loudness normalization to balance volume differences"),
            ("/eq", "Apply an EQ preset (Flat, Bass Heavy, Treble Heavy, Vocal, Electronic)"),
            ("/eqcustom `<band>` `<gain>`", "Boost or cut a specific frequency band (-12 to +12 dB)"),
            ("/crossfade `<0-10>`", "Set crossfade duration between tracks"),
        ],
    ),
    (
        "🎛️ Player",
        "player",
        [
            ("/nowplaying", "Show the currently playing track with progress bar"),
            ("/player", "Open an interactive player with playback controls"),
            ("/loop", "Cycle loop mode: off → single → queue → off"),
            ("/autoplay", "Auto-queue similar tracks when the queue runs out"),
            ("/lyrics", "Show lyrics for the current or a specified track"),
            ("/grab", "Send the current track info to your DMs"),
        ],
    ),
    (
        "📻 Radio",
        "radio",
        [
            ("/radio `<seed>`", "Start endless radio seeded by an artist or genre"),
            ("/radio-off", "Stop radio mode"),
            ("/similar", "Show Spotify recommendations based on the current track"),
        ],
    ),
    (
        "❤️ Favorites",
        "favorites",
        [
            ("/fav", "Save the current track to your favorites"),
            ("/favs", "List your favorite tracks"),
            ("/unfav `<pos>`", "Remove a track from your favorites by position"),
            ("/playfavs", "Queue all your favorite tracks"),
        ],
    ),
    (
        "📁 Playlists",
        "playlists",
        [
            ("/playlist save `<name>`", "Save the current queue as a named playlist"),
            ("/playlist load `<name>`", "Queue tracks from a saved playlist"),
            ("/playlist list", "List all saved playlists"),
            ("/playlist delete `<name>`", "Delete a saved playlist"),
            ("/playlist addtrack `<name>`", "Add the current track to a playlist"),
            ("/playlist removetrack `<name>` `<pos>`", "Remove a track from a playlist"),
            ("/playlist adduser `<name>` `<user>`", "Add a collaborator to a playlist"),
            ("/playlist removeuser `<name>` `<user>`", "Remove a collaborator from a playlist"),
        ],
    ),
    (
        "🏆 Stats & Social",
        "stats",
        [
            ("/top", "Show the most played tracks in this server"),
            ("/toprated", "Show the highest-rated tracks in this server"),
            ("/rate", "Rate the current track with thumbs up / down"),
            ("/stats", "Show server-wide listening stats"),
            ("/mystats", "Show your personal listening history and top tracks"),
        ],
    ),
    (
        "🔎 Search", "search", [
            ("/search", "Search and pick from results (uses server default)"),
            ("/youtube-search", "Search YouTube and pick from results"),
            ("/spotify-search", "Search Spotify and pick from results"),
            ("/searchmode", "Toggle default search between YouTube and Spotify"),
        ],
    ),
    (
        "⚙️ Settings",
        "settings",
        [
            ("/setup", "Owner-only private setup for this server’s music sources"),
            ("/invite", "Install Essusic in another server"),
            ("/maxqueue `<size>`", "Set the maximum queue size (default 50)"),
            ("/maxperuser `<limit>`", "Limit how many tracks each user can have in the queue"),
            ("/setnpchannel", "Set this channel as the now-playing display channel"),
            ("/clearnpchannel", "Disable the dedicated now-playing channel"),
            ("/dj `<role>`", "Restrict bot controls to a DJ role (admin only)"),
            ("/djclear", "Remove the DJ role restriction (admin only)"),
            ("/djmode", "Require DJ approval before non-DJs can add tracks (admin only)"),
            ("/24-7", "Keep the bot connected even when idle or alone"),
            ("/language `<lang>`", "Set the bot language for this server"),
        ],
    ),
]


def _build_help_embed(category_id: str) -> discord.Embed:
    for label, cid, commands_list in _HELP_CATEGORIES:
        if cid == category_id:
            lines = [f"**{cmd.replace('`', '')}**\n{desc}" for cmd, desc in commands_list]
            return card(title=label, section="Command guide", description="\n\n".join(lines),
                        footer=f"{len(commands_list)} commands · Choose another section below")
    embed = card(title="Make yourself at home", section="Welcome",
                 description="Your music, together. Start a track, build a queue, and settle in.",
                 footer="Choose a section below for commands and examples.")
    embed.add_field(name="01 · Start listening", value="Owner: connect sources with `/setup`. Then join voice and use `/play`.", inline=False)
    embed.add_field(name="02 · Find your rhythm", value="Open `/player` for controls, or `/search` to pick your next track.", inline=False)
    embed.add_field(name="03 · Keep the good ones", value="Save a track with `/fav` or keep the queue with `/playlist save`.", inline=False)
    return embed


class HelpView(discord.ui.View):
    """Category select menu for /help."""

    def __init__(self) -> None:
        super().__init__(timeout=120)
        self.message = None
        options = [discord.SelectOption(label="Start here", value="overview", description="A quick guide to your first session")]
        options += [
            discord.SelectOption(label=label, value=cid, description=f"{len(cmds)} commands")
            for label, cid, cmds in _HELP_CATEGORIES
        ]
        select = discord.ui.Select(
            placeholder="Browse a category…",
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)
        self._select = select

    async def _on_select(self, interaction: discord.Interaction) -> None:
        chosen = self._select.values[0]
        for option in self._select.options:
            option.default = option.value == chosen
        await interaction.response.edit_message(embed=_build_help_embed(chosen), view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.queues = QueueManager()
        self.credentials = CredentialStore()
        self.media = MediaService(self.credentials, guild_lookup=bot.get_guild)
        self.setup_portal = None
        self.history = HistoryManager()
        self.favorites = FavoritesManager()
        self.playlists = PlaylistManager()
        self.ratings = RatingsManager()
        self._active_players: dict[int, PlayerView] = {}
        self._crossfade_timers: dict[int, asyncio.TimerHandle] = {}
        self._play_locks: dict[int, asyncio.Lock] = {}
        self._playing_guilds: set[int] = set()  # guilds currently playing audio

    @app_commands.command(name="setup", description="Server owner: connect or manage this server's music sources")
    @app_commands.default_permissions(manage_guild=True)
    async def setup_sources(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message(
                embed=notice("Only the server owner can manage source credentials.", section="Server setup"), ephemeral=True)
            return
        if self.setup_portal is None:
            await interaction.response.send_message(embed=notice(
                "The bot operator must configure CREDENTIALS_KEY, WEB_BASE_URL and WEB_PORT, then restart the bot. "
                "See the README for hosted and self-hosted setup.", section="Server setup"), ephemeral=True)
            return
        url = self.setup_portal.issue(interaction.guild.id, interaction.user.id)
        view = discord.ui.View(timeout=300)
        view.add_item(discord.ui.Button(label="Open private setup", url=url, emoji="🔐"))
        await interaction.response.send_message(embed=notice(
            "Connect YouTube and optional Spotify for **this server**. Your private link can be opened once, "
            "within 5 minutes; the setup session lasts 15 minutes. Keep the link private. "
            "Run /setup again to replace or remove credentials.", section="Server setup"), view=view, ephemeral=True)

    @app_commands.command(name="invite", description="Install Essusic in another server")
    async def invite(self, interaction: discord.Interaction) -> None:
        permissions = discord.Permissions(view_channel=True, send_messages=True, embed_links=True,
                                          attach_files=True, connect=True, speak=True)
        url = discord.utils.oauth_url(self.bot.user.id, permissions=permissions,
                                     scopes=("bot", "applications.commands"))
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Add to a server", url=url))
        await interaction.response.send_message(embed=notice(
            "Invite Essusic, then have the server owner run /setup to connect their music sources.",
            section="Install Essusic"), view=view, ephemeral=True)

    async def revoke_source_playback(self, guild_id: int) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        gq = self.queues.get(guild_id)
        mixing = guild.voice_client and isinstance(guild.voice_client.source, CrossfadeSource)
        if not mixing and (not gq.current or not requires_youtube(gq.current.url)):
            return
        # Invalidate pending extraction/crossfade identity checks before yielding.
        gq.clear()
        self._cancel_crossfade_timer(guild_id)
        self._cleanup_player(guild_id)
        self.queues.save_queue_state(guild_id)
        if guild_id in self._playing_guilds:
            self._playing_guilds.discard(guild_id)
            metric_active_players.dec()
        if guild.voice_client:
            await guild.voice_client.disconnect(force=True)
        await self._update_presence(None)

    async def _revoke_guild_sources(self, guild_id: int) -> None:
        self.credentials.delete(guild_id)
        self.media.invalidate(guild_id)
        if self.setup_portal:
            self.setup_portal.revoke(guild_id)
        await self.revoke_source_playback(guild_id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self._revoke_guild_sources(guild.id)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        if before.owner_id != after.owner_id:
            await self._revoke_guild_sources(after.id)

    @commands.Cog.listener()
    async def on_guild_available(self, guild: discord.Guild) -> None:
        # Also catches ownership transfers while the bot was offline.
        try:
            owner_id = self.credentials.read(guild.id).get('owner_id')
            if owner_id is not None and owner_id != guild.owner_id:
                await self._revoke_guild_sources(guild.id)
        except CredentialError:
            log.warning("Source storage could not be opened for guild %s", guild.id)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.on_guild_available(guild)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Remove credentials for servers the bot left while offline.
        for path in self.credentials.directory.glob('*.enc'):
            if path.stem.isdigit() and self.bot.get_guild(int(path.stem)) is None:
                await self._revoke_guild_sources(int(path.stem))

    # ── helpers ──────────────────────────────────────────────────────────

    def _cleanup_player(self, guild_id: int) -> None:
        """Stop and remove the active PlayerView for a guild."""
        old = self._active_players.pop(guild_id, None)
        if old:
            if old._update_task and not old._update_task.done():
                old._update_task.cancel()
            old.stop()

    def _get_elapsed(self, gq: GuildQueue) -> int:
        """Get elapsed playback time in seconds, accounting for speed."""
        return gq.elapsed()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=notice("Use music commands in a server.", section='Playback'), ephemeral=True
            )
            return False
        return True

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        message = str(original) if isinstance(original, (SourceError, CredentialError)) else "The request could not be completed. Try again or ask the owner to check /setup."
        if interaction.response.is_done():
            await interaction.followup.send(embed=notice(message, section="Music sources"), ephemeral=True)
        else:
            await interaction.response.send_message(embed=notice(message, section="Music sources"), ephemeral=True)

    def _pause(self, guild: discord.Guild) -> None:
        guild.voice_client.pause()
        self.queues.get(guild.id).pause_clock()
        self._cancel_crossfade_timer(guild.id)

    def _resume(self, guild: discord.Guild) -> None:
        guild.voice_client.resume()
        self.queues.get(guild.id).resume_clock()
        self._schedule_crossfade(guild)

    def _skip(self, guild: discord.Guild) -> None:
        gq = self.queues.get(guild.id)
        # A manual skip must advance even when single-track looping is enabled.
        if gq.loop_mode == LoopMode.SINGLE:
            gq.previous = gq.current
            gq.current = None
        self._cancel_crossfade_timer(guild.id)
        guild.voice_client.stop()

    async def _ensure_voice(
        self, interaction: discord.Interaction
    ) -> Optional[discord.VoiceClient]:
        """Join the user's voice channel if needed. Returns the VoiceClient or None on failure."""
        if not interaction.user.voice or not interaction.user.voice.channel:
            msg = "You need to be in a voice channel."
            if interaction.response.is_done():
                await interaction.followup.send(embed=notice(msg, section='Playback'), ephemeral=True)
            else:
                await interaction.response.send_message(embed=notice(msg, section='Playback'), ephemeral=True)
            return None

        channel = interaction.user.voice.channel
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[assignment]

        if vc is None:
            vc = await channel.connect(self_deaf=True)
            metric_voice_connections.inc()
        elif vc.channel != channel:
            await vc.move_to(channel)

        return vc

    def _check_idle(self, guild: discord.Guild) -> None:
        gq = self.queues.get(guild.id)
        if gq.stay_connected:
            return
        vc: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
        if vc and not vc.is_playing() and not vc.is_paused():
            self._cancel_crossfade_timer(guild.id)
            self._cleanup_player(guild.id)
            asyncio.run_coroutine_threadsafe(vc.disconnect(), self.bot.loop)
            self.queues.clear_queue_state(guild.id)
            self.queues.remove(guild.id)
            asyncio.run_coroutine_threadsafe(
                self._update_presence(None), self.bot.loop
            )

    def _after_play(self, guild: discord.Guild, error: Exception | None) -> None:
        if error:
            log.error("Playback error in guild %s", guild.id)
        asyncio.run_coroutine_threadsafe(self._play_next(guild), self.bot.loop)

    async def _notify_text_channel(self, guild: discord.Guild, msg: str) -> None:
        """Send a message to the guild's tracked text channel, if set."""
        gq = self.queues.get(guild.id)
        if gq.text_channel_id:
            channel = guild.get_channel(gq.text_channel_id)
            if channel and hasattr(channel, "send"):
                try:
                    await channel.send(embed=notice(msg, section='Playback'))  # type: ignore[union-attr]
                except discord.HTTPException:
                    pass

    async def _play_next(self, guild: discord.Guild) -> None:
        lock = self._play_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            await self._play_next_unlocked(guild)

    async def _play_next_unlocked(self, guild: discord.Guild, _fail_count: int = 0) -> None:
        gq = self.queues.get(guild.id)
        vc: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
        if vc is None:
            gq.clear()
            return

        # Ignore duplicate callbacks while a track is already active.
        if vc.is_playing() or vc.is_paused():
            return

        self._cancel_crossfade_timer(guild.id)
        track = gq.next_track()
        if track is None:
            spotify = self.media.spotify(guild.id)
            # Radio mode: continuously queue similar tracks
            if gq.radio_mode and spotify.available and gq.radio_seed:
                try:
                    results = await self.bot.loop.run_in_executor(
                        None,
                        lambda: spotify.recommend_by_seed(
                            gq.radio_seed, gq.radio_history, 1  # type: ignore[arg-type]
                        ),
                    )
                    if results:
                        tid, rec = results[0]
                        gq.radio_history.add(tid)
                        # Keep history bounded; if it grows too large, drop the oldest
                        # half so Spotify has more tracks to recommend from
                        if len(gq.radio_history) > 200:
                            items = list(gq.radio_history)
                            gq.radio_history = set(items[100:])
                        gq.add(rec)
                        track = gq.next_track()
                        await self._notify_text_channel(
                            guild, f"Radio: queued **{rec.title}**"
                        )
                except Exception:
                    log.warning("Radio recommendation failed in guild %s", guild.id)

            # Autoplay: recommend a track based on what just played
            # Use gq.previous — next_track() already moved current → previous
            if track is None and gq.autoplay and spotify.available and gq.previous is not None:
                try:
                    rec = await self.bot.loop.run_in_executor(
                        None, spotify.recommend, gq.previous.title
                    )
                    if rec:
                        gq.add(rec)
                        track = gq.next_track()
                        await self._notify_text_channel(
                            guild, f"Autoplay: queued **{rec.title}**"
                        )
                except Exception:
                    log.warning("Autoplay recommendation failed in guild %s", guild.id)

            if track is None:
                if guild.id in self._playing_guilds:
                    self._playing_guilds.discard(guild.id)
                    metric_active_players.dec()
                self.queues.save_queue_state(guild.id)
                await self._update_presence(None)
                if not gq.stay_connected:
                    self.bot.loop.call_later(300, self._check_idle, guild)
                return

        try:
            source = await YTDLSource.from_query(
                track.url, loop=self.bot.loop, media=self.media, guild_id=guild.id, volume=gq.volume,
                filter_name=gq.filter_name,
                speed=gq.speed, normalize=gq.normalize,
                eq_bands=gq.eq_bands if any(g != 0 for g in gq.eq_bands) else None,
                is_live=track.is_live,
            )
        except Exception as exc:
            if guild.voice_client is not vc or gq.current is not track or not vc.is_connected():
                return
            log.warning("Source resolution failed in guild %s (%s)", guild.id, type(exc).__name__)
            if isinstance(exc, (SourceError, CredentialError)):
                gq.queue.appendleft(track)
                gq.current = None
                if guild.id in self._playing_guilds:
                    self._playing_guilds.discard(guild.id)
                    metric_active_players.dec()
                self._cleanup_player(guild.id)
                await self._update_presence(None)
                self.queues.save_queue_state(guild.id)
                await self._notify_text_channel(
                    guild, str(exc) + " Playback is waiting; the track remains first in /queue. "
                    "Use /remove 1 to discard it before requesting another track."
                )
                return
            playback_errors_total.inc()
            await self._notify_text_channel(
                guild, f"Failed to play **{track.title}**, skipping..."
            )
            if _fail_count >= 10:
                await self._notify_text_channel(
                    guild, "Too many consecutive playback failures. Stopping."
                )
                return
            # Do not retry a failed track forever in a loop mode.
            gq.current = None
            await self._play_next_unlocked(guild, _fail_count + 1)
            return

        if guild.voice_client is not vc or gq.current is not track or not vc.is_connected():
            source.cleanup()
            return

        # Spotify/search entries may not have duration or artwork until resolved.
        track.duration = source.duration or track.duration
        track.thumbnail = source.thumbnail or track.thumbnail
        track.is_live = track.is_live or bool(source._data.get("is_live"))
        tracks_played_total.inc()
        if guild.id not in self._playing_guilds:
            self._playing_guilds.add(guild.id)
            metric_active_players.inc()
        metric_queue_size.labels(guild_id=str(guild.id)).set(len(gq.queue))
        gq.play_start_time = time.time()
        gq.paused_at = None
        self.history.record(
            guild.id, track,
            requester_id=track.requester_id,
            duration=track.duration,
        )
        self.queues.save_queue_state(guild.id)
        vc.play(source, after=lambda e: self._after_play(guild, e))
        await self._update_presence(track)

        self._schedule_crossfade(guild)

        # Auto-send/refresh the player view in the text channel
        await self._send_player(guild, gq)

        # Update dedicated now-playing channel if set
        await self._update_np_channel(guild, gq)

    async def _send_player(self, guild: discord.Guild, gq: GuildQueue) -> None:
        """Send or refresh the interactive PlayerView in the text channel."""
        # Clean up the old player
        old = self._active_players.pop(guild.id, None)
        if old:
            if old._update_task and not old._update_task.done():
                old._update_task.cancel()
            old.stop()

        if gq.current is None or gq.text_channel_id is None:
            if old and old.message:
                try:
                    await old.message.delete()
                except discord.HTTPException:
                    pass
            return

        channel = guild.get_channel(gq.text_channel_id)
        if channel is None or not hasattr(channel, "send"):
            if old and old.message:
                try:
                    await old.message.delete()
                except discord.HTTPException:
                    pass
            return

        view = PlayerView(self, guild)
        self._active_players[guild.id] = view
        embed = view._build_embed()
        view._sync_pause_button()

        # Always delete old message and send a new one at the bottom
        if old and old.message:
            try:
                await old.message.delete()
            except discord.HTTPException:
                pass

        try:
            msg = await channel.send(embed=embed, view=view, silent=True)  # type: ignore[union-attr]
            view.message = msg
            view._update_task = asyncio.create_task(view._auto_update())
        except discord.HTTPException:
            self._active_players.pop(guild.id, None)

    async def _update_np_channel(self, guild: discord.Guild, gq: GuildQueue) -> None:
        """Post or edit a now-playing embed in the dedicated NP channel."""
        if not gq.np_channel_id or gq.current is None:
            return
        channel = guild.get_channel(gq.np_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        vc = guild.voice_client
        embed = player_card(gq, elapsed=self._get_elapsed(gq), paused=bool(vc and vc.is_paused()))

        # Try to edit existing message first
        if gq.np_message_id:
            try:
                msg = await channel.fetch_message(gq.np_message_id)  # type: ignore[union-attr]
                await msg.edit(embed=embed)
                return
            except discord.HTTPException:
                gq.np_message_id = None

        # Post a new message
        try:
            msg = await channel.send(embed=embed)  # type: ignore[union-attr]
            gq.np_message_id = msg.id
        except discord.HTTPException:
            pass

    async def _update_presence(self, track: TrackInfo | None) -> None:
        if track:
            activity = discord.Activity(
                type=discord.ActivityType.listening, name=track.title
            )
        else:
            activity = None
        await self.bot.change_presence(activity=activity)

    def _cancel_crossfade_timer(self, guild_id: int) -> None:
        handle = self._crossfade_timers.pop(guild_id, None)
        if handle:
            handle.cancel()

    def _schedule_crossfade(self, guild: discord.Guild) -> None:
        self._cancel_crossfade_timer(guild.id)
        gq = self.queues.get(guild.id)
        vc = guild.voice_client
        if (vc is None or not vc.is_playing() or not gq.current
                or gq.current.is_live or gq.current.duration <= 0
                or not gq.queue or gq.crossfade_seconds <= 0
                or gq.loop_mode == LoopMode.SINGLE):
            return
        delay = max(0, (gq.current.duration - gq.elapsed()) / gq.speed - gq.crossfade_seconds)
        self._crossfade_timers[guild.id] = self.bot.loop.call_later(
            delay, lambda: asyncio.ensure_future(self._start_crossfade(guild))
        )

    async def _start_crossfade(self, guild: discord.Guild) -> None:
        """Begin crossfade from current track to next."""
        gq = self.queues.get(guild.id)
        vc: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
        if vc is None or not vc.is_playing() or not gq.queue:
            return

        if gq.crossfade_seconds <= 0 or gq.loop_mode == LoopMode.SINGLE:
            return
        current_track = gq.current
        current_source = vc.source
        next_track = gq.queue[0]
        try:
            incoming = await YTDLSource.from_query(
                next_track.url, loop=self.bot.loop, media=self.media, guild_id=guild.id, volume=gq.volume,
                filter_name=gq.filter_name,
                speed=gq.speed, normalize=gq.normalize,
                eq_bands=gq.eq_bands if any(g != 0 for g in gq.eq_bands) else None,
                is_live=next_track.is_live,
            )
        except Exception:
            log.warning("Crossfade source resolution failed in guild %s", guild.id)
            return

        # Re-validate state after the async fetch — vc may have stopped or
        # the queue may have changed (user skipped, /stop, etc.)
        if (
            vc is None
            or not vc.is_playing()
            or not gq.queue
            or gq.queue[0] is not next_track
            or gq.current is not current_track
            or vc.source is not current_source
            or gq.crossfade_seconds <= 0
            or gq.loop_mode == LoopMode.SINGLE
        ):
            incoming.cleanup()
            return

        outgoing = vc.source
        if outgoing is None:
            incoming.cleanup()
            return

        # Transfer source ownership to the mixer without stopping the player:
        # stopping would clean up the outgoing FFmpeg process in the audio thread.
        if isinstance(outgoing, discord.PCMVolumeTransformer):
            outgoing.volume = 1.0
        incoming.volume = 1.0
        xfade = CrossfadeSource(outgoing, incoming, gq.crossfade_seconds)
        vc.source = discord.PCMVolumeTransformer(xfade, volume=gq.volume)

        # Advance queue
        gq.previous = gq.current
        gq.current = gq.queue.popleft()
        if gq.loop_mode == LoopMode.QUEUE and gq.previous:
            gq.queue.append(gq.previous)
        gq.current.duration = incoming.duration or gq.current.duration
        gq.current.thumbnail = incoming.thumbnail or gq.current.thumbnail
        gq.skip_votes.clear()
        gq.play_start_time = time.time()
        gq.paused_at = None
        self.history.record(
            guild.id, gq.current,
            requester_id=gq.current.requester_id,
            duration=gq.current.duration,
        )
        self.queues.save_queue_state(guild.id)

        tracks_played_total.inc()
        await self._update_presence(gq.current)
        self._schedule_crossfade(guild)
        await self._send_player(guild, gq)
        await self._update_np_channel(guild, gq)

    async def _restart_playback(
        self, guild: discord.Guild, seek_seconds: int = 0
    ) -> None:
        """Restart current track with the active filter and/or seek position."""
        gq = self.queues.get(guild.id)
        vc: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
        if vc is None or gq.current is None:
            return

        self.media.ensure_source(guild.id, gq.current.url)
        eq = gq.eq_bands if any(g != 0 for g in gq.eq_bands) else None
        is_live = gq.current.is_live

        # Get the stream URL from the current source if available
        current_source = vc.source
        stream_url = None
        if isinstance(current_source, YTDLSource):
            stream_url = current_source.stream_url
            data = current_source._data

        track = gq.current
        if stream_url:
            source = YTDLSource.from_stream_url(
                stream_url,
                data=data,
                volume=gq.volume,
                filter_name=gq.filter_name,
                seek_seconds=seek_seconds,
                speed=gq.speed,
                normalize=gq.normalize,
                eq_bands=eq,
                is_live=is_live,
            )
        else:
            source = await YTDLSource.from_query(
                gq.current.url,
                loop=self.bot.loop, media=self.media, guild_id=guild.id,
                volume=gq.volume,
                filter_name=gq.filter_name,
                seek_seconds=seek_seconds,
                speed=gq.speed,
                normalize=gq.normalize,
                eq_bands=eq,
                is_live=is_live,
            )

        if (guild.voice_client is not vc or gq.current is not track
                or vc.source is not current_source
                or not (vc.is_playing() or vc.is_paused())):
            source.cleanup()
            return
        was_paused = vc.is_paused()
        vc.source = source
        if was_paused:
            vc.pause()
        if current_source:
            current_source.cleanup()
        now = time.time()
        gq.play_start_time = now - (seek_seconds / gq.speed)
        gq.paused_at = now if was_paused else None
        self._schedule_crossfade(guild)

    async def _enqueue_and_play(
        self, interaction: discord.Interaction, track: TrackInfo, *, play_next: bool = False
    ) -> None:
        self.media.ensure_source(interaction.guild.id, track.url)
        vc = await self._ensure_voice(interaction)
        if vc is None:
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.text_channel_id = interaction.channel_id

        # DJ queue mode: non-DJs submit requests for approval
        if gq.dj_queue_mode and _check_dj(interaction, gq) is not None:
            if len(gq.pending_requests) >= 50:
                msg = "Too many pending requests. Please wait for the DJ to approve some first."
                if interaction.response.is_done():
                    await interaction.followup.send(embed=notice(msg, section='Playback'), ephemeral=True)
                else:
                    await interaction.response.send_message(embed=notice(msg, section='Playback'), ephemeral=True)
                return
            gq.pending_requests.append(track)
            msg = f"**{track.title}** submitted for DJ approval."
            if interaction.response.is_done():
                await interaction.followup.send(embed=notice(msg, section='Playback'))
            else:
                await interaction.response.send_message(embed=notice(msg, section='Playback'))
            # Send approval view to text channel
            if gq.text_channel_id:
                channel = interaction.guild.get_channel(gq.text_channel_id)  # type: ignore[union-attr]
                if channel and hasattr(channel, "send"):
                    view = DJApprovalView(self, interaction.guild, track)  # type: ignore[arg-type]
                    view.message = await channel.send(  # type: ignore[union-attr]
                        embed=request_card(track),
                        view=view,
                    )
            return

        # Per-user queue limit
        if gq.max_per_user > 0:
            user_count = sum(
                1 for t in gq.queue
                if t.requester_id == interaction.user.id
                or (t.requester_id == 0 and t.requester == interaction.user.display_name)
            )
            if user_count >= gq.max_per_user:
                s = "s" if user_count != 1 else ""
                msg = (
                    f"You already have **{user_count}** track{s} in the queue "
                    f"(max **{gq.max_per_user}** per user)."
                )
                if interaction.response.is_done():
                    await interaction.followup.send(embed=notice(msg, section='Playback'), ephemeral=True)
                else:
                    await interaction.response.send_message(embed=notice(msg, section='Playback'), ephemeral=True)
                return

        # play_next: insert at front of queue when something is already playing
        if play_next and (vc.is_playing() or vc.is_paused()):
            if len(gq.queue) >= gq.max_queue:
                msg = f"Queue is full ({gq.max_queue} tracks max)."
                if interaction.response.is_done():
                    await interaction.followup.send(embed=notice(msg, section='Playback'), ephemeral=True)
                else:
                    await interaction.response.send_message(embed=notice(msg, section='Playback'), ephemeral=True)
                return
            gq.queue.appendleft(track)
            self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
            msg = f"⏭️ **{track.title}** will play next."
            if interaction.response.is_done():
                await interaction.followup.send(embed=notice(msg, section='Playback'))
            else:
                await interaction.response.send_message(embed=notice(msg, section='Playback'))
            return

        # Duplicate detection
        is_dup = gq.has_duplicate(track)
        pos = gq.add(track)
        self._schedule_crossfade(interaction.guild)
        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]

        if pos is None:
            msg = f"Queue is full ({gq.max_queue} tracks max)."
            if interaction.response.is_done():
                await interaction.followup.send(embed=notice(msg, section='Playback'), ephemeral=True)
            else:
                await interaction.response.send_message(embed=notice(msg, section='Playback'), ephemeral=True)
            return

        if not vc.is_playing() and not vc.is_paused():
            await self._play_next(interaction.guild)  # type: ignore[arg-type]
            msg = (f"Now playing: **{gq.current.title}**" if vc.is_playing() and gq.current
                   else f"Added **{track.title}**. Playback has not started; check the source message above.")
        else:
            msg = f"Queued **{track.title}** at position #{pos}"
            if is_dup:
                msg += "\n**{title}** is already in the queue. Adding anyway.".format(
                    title=track.title
                )

        if interaction.response.is_done():
            await interaction.followup.send(embed=notice(msg, section='Playback'))
        else:
            await interaction.response.send_message(embed=notice(msg, section='Playback'))

    async def _play_youtube_playlist(
        self, interaction: discord.Interaction, url: str
    ) -> None:
        """Fetch a YouTube playlist and queue all its tracks."""
        try:
            data = await self.media.extract(interaction.guild.id, url, playlist=True, flat=True)
        except Exception as exc:
            await interaction.followup.send(embed=notice(str(exc) if isinstance(exc, (SourceError, CredentialError)) else "Could not load this playlist. Try another link.", section='Playback'))
            return

        entries = (data or {}).get("entries") or []
        if not entries:
            await interaction.followup.send(embed=notice("❌ No tracks found in that playlist.", section='Playback'))
            return

        vc = await self._ensure_voice(interaction)
        if vc is None:
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.text_channel_id = interaction.channel_id
        total_entries = sum(1 for e in entries if e is not None)
        progress_msg = None
        if total_entries > 5:
            progress_msg = await interaction.followup.send(
                embed=notice(f"Loading... (0/{total_entries} queued)", section='Playback'), wait=True
            )

        count = 0
        skipped = 0
        skip_reason = "queue full"
        user_id = interaction.user.id
        user_name = interaction.user.display_name
        user_queued = sum(
            1 for t in gq.queue
            if t.requester_id == user_id or (t.requester_id == 0 and t.requester == user_name)
        )
        for entry in entries:
            if entry is None:
                continue
            if gq.max_per_user > 0 and user_queued >= gq.max_per_user:
                skipped = total_entries - count
                skip_reason = f"per-user limit of {gq.max_per_user}"
                break
            entry_url = entry.get("webpage_url") or entry.get("url", "")
            if not entry_url:
                video_id = entry.get("id", "")
                if video_id:
                    entry_url = f"https://www.youtube.com/watch?v={video_id}"
            track = TrackInfo(
                title=entry.get("title", "Unknown"),
                url=entry_url,
                duration=int(entry.get("duration", 0) or 0),
                thumbnail=entry.get("thumbnail", ""),
                requester=user_name,
                requester_id=user_id,
            )
            if gq.add(track) is None:
                skipped = total_entries - count
                break
            count += 1
            user_queued += 1
            if progress_msg and count % 5 == 0:
                try:
                    await progress_msg.edit(content=None, embed=notice(f"Loading... ({count}/{total_entries} queued)", section='Playback'))
                except discord.HTTPException:
                    pass

        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]

        if not vc.is_playing() and not vc.is_paused():
            await self._play_next(interaction.guild)  # type: ignore[arg-type]

        playlist_title = data.get("title", "YouTube playlist")
        msg = f"Queued **{count} tracks** from **{playlist_title}**."
        if skipped:
            msg += f" ({skipped} skipped — {skip_reason})"
        if progress_msg:
            try:
                await progress_msg.edit(content=None, embed=notice(msg, section='Playback'))
            except discord.HTTPException:
                await interaction.followup.send(embed=notice(msg, section='Playback'))
        else:
            await interaction.followup.send(embed=notice(msg, section='Playback'))

    async def _play_single_url(
        self, interaction: discord.Interaction, url: str, *, play_next: bool = False
    ) -> None:
        """Resolve a single YouTube URL or search query and queue it."""
        try:
            data = await self.media.extract(interaction.guild.id, url)
            if not data:
                raise SourceError("No tracks found. Try another link or search.")
            if "entries" in data:
                data = data["entries"][0]

            track = TrackInfo(
                title=data.get("title", "Unknown"),
                url=data.get("webpage_url", url),
                duration=int(data.get("duration", 0) or 0),
                thumbnail=data.get("thumbnail", ""),
                requester=interaction.user.display_name,
                artist=data.get("artist", "") or data.get("uploader", "") or "",
                requester_id=interaction.user.id,
            )
        except Exception as exc:
            await interaction.followup.send(embed=notice(str(exc) if isinstance(exc, (SourceError, CredentialError)) else "Could not resolve this track. Try another link.", section='Playback'))
            return

        await self._enqueue_and_play(interaction, track, play_next=play_next)

    # ── commands ─────────────────────────────────────────────────────────

    @app_commands.command(name="play", description="Play from a YouTube/Spotify URL or search keywords")
    @app_commands.describe(query="YouTube URL, Spotify URL, or search keywords")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        self.media.ensure_source(interaction.guild.id, query)
        input_type, value = classify(query)

        # Spotify resolution
        if input_type in (
            InputType.SPOTIFY_TRACK,
            InputType.SPOTIFY_PLAYLIST,
            InputType.SPOTIFY_ALBUM,
        ):
            spotify = self.media.spotify(interaction.guild.id)
            if not spotify.available:
                await interaction.response.send_message(
                    embed=notice("Spotify is not configured for this server. Ask the owner to run /setup.", section='Playback'), ephemeral=True
                )
                return

            await interaction.response.defer()

            resolver_map = {
                InputType.SPOTIFY_TRACK: spotify.resolve_track,
                InputType.SPOTIFY_PLAYLIST: spotify.resolve_playlist,
                InputType.SPOTIFY_ALBUM: spotify.resolve_album,
            }
            try:
                search_strings = await self.bot.loop.run_in_executor(
                    None, resolver_map[input_type], value
                )
            except Exception:
                await interaction.followup.send(embed=notice("Spotify could not complete the request. The owner can test the connection in /setup.", section='Playback'))
                return

            if not search_strings:
                await interaction.followup.send(embed=notice("❌ No tracks found from that Spotify link.", section='Playback'))
                return

            vc = await self._ensure_voice(interaction)
            if vc is None:
                return

            gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
            gq.text_channel_id = interaction.channel_id
            total = len(search_strings)
            progress_msg = None
            if total > 5:
                progress_msg = await interaction.followup.send(
                    embed=notice(f"Loading... (0/{total} queued)", section='Playback'), wait=True
                )

            count = 0
            sp_skip_reason = "queue full"
            sp_user_id = interaction.user.id
            sp_user_name = interaction.user.display_name
            sp_user_queued = sum(
                1 for t in gq.queue
                if t.requester_id == sp_user_id or (t.requester_id == 0 and t.requester == sp_user_name)
            )
            for s in search_strings:
                if gq.max_per_user > 0 and sp_user_queued >= gq.max_per_user:
                    sp_skip_reason = f"per-user limit of {gq.max_per_user}"
                    break
                track = TrackInfo(
                    title=s,
                    url=f"ytsearch:{s}",
                    requester=sp_user_name,
                    requester_id=sp_user_id,
                )
                if gq.add(track) is None:
                    break
                count += 1
                sp_user_queued += 1
                if progress_msg and count % 5 == 0:
                    try:
                        await progress_msg.edit(content=None, embed=notice(f"Loading... ({count}/{total} queued)", section='Playback'))
                    except discord.HTTPException:
                        pass

            self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]

            if not vc.is_playing() and not vc.is_paused():
                await self._play_next(interaction.guild)  # type: ignore[arg-type]

            msg = f"Queued **{count} track{'s' if count != 1 else ''}** from Spotify."
            if count < len(search_strings):
                msg += f" ({len(search_strings) - count} skipped — {sp_skip_reason})"
            if progress_msg:
                try:
                    await progress_msg.edit(content=None, embed=notice(msg, section='Playback'))
                except discord.HTTPException:
                    await interaction.followup.send(embed=notice(msg, section='Playback'))
            else:
                await interaction.followup.send(embed=notice(msg, section='Playback'))
            return

        # YouTube playlist
        if input_type == InputType.YOUTUBE_PLAYLIST:
            # Detect YouTube Mix (list=RD...) — these are personalized
            params = parse_qs(urlparse(value).query)
            list_id = params.get("list", [""])[0]
            if list_id.startswith("RD"):
                await interaction.response.send_message(
                    embed=notice("This is a **YouTube Mix** — its contents are personalized and "
                    "may differ from what you see in your browser.\n"
                    "What would you like to do?", section='Playback'),
                    view=MixConfirmView(self, interaction, value),
                )
                return

            await interaction.response.defer()
            await self._play_youtube_playlist(interaction, value)
            return

        # Radio/live stream
        if input_type == InputType.RADIO_STREAM:
            await interaction.response.defer()
            track = TrackInfo(
                title=value.split("/")[-1] or "Live Stream",
                url=value,
                duration=0,
                requester=interaction.user.display_name,
                requester_id=interaction.user.id,
                is_live=True,
            )
            await self._enqueue_and_play(interaction, track)
            return

        # SoundCloud
        if input_type == InputType.SOUNDCLOUD_URL:
            await interaction.response.defer()
            await self._play_single_url(interaction, value)
            return

        if input_type == InputType.SOUNDCLOUD_PLAYLIST:
            await interaction.response.defer()
            await self._play_youtube_playlist(interaction, value)
            return

        # YouTube URL or search
        await interaction.response.defer()
        if input_type == InputType.SEARCH_QUERY:
            url = f"ytsearch:{value}"
        else:
            url = value
        await self._play_single_url(interaction, url)

    @app_commands.command(name="playnext", description="Insert a track to play immediately after the current one")
    @app_commands.describe(query="YouTube URL, Spotify track URL, or search keywords")
    async def playnext(self, interaction: discord.Interaction, query: str) -> None:
        self.media.ensure_source(interaction.guild.id, query)
        input_type, value = classify(query)

        # Reject playlists / streams — playnext is single-track only
        if input_type in (
            InputType.YOUTUBE_PLAYLIST,
            InputType.SPOTIFY_PLAYLIST,
            InputType.SPOTIFY_ALBUM,
            InputType.SOUNDCLOUD_PLAYLIST,
            InputType.RADIO_STREAM,
        ):
            await interaction.response.send_message(
                embed=notice("❌ `/playnext` only supports single tracks. Use `/play` for playlists.", section='Playback'),
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        # Resolve Spotify track to a search string
        if input_type == InputType.SPOTIFY_TRACK:
            spotify = self.media.spotify(interaction.guild.id)
            if not spotify.available:
                await interaction.followup.send(
                    embed=notice("Spotify is not configured for this server. Ask the owner to run /setup.", section='Playback'), ephemeral=True
                )
                return
            try:
                search_strings = await self.bot.loop.run_in_executor(
                    None, spotify.resolve_track, value
                )
            except Exception:
                await interaction.followup.send(embed=notice("Spotify could not complete the request. The owner can test the connection in /setup.", section='Playback'))
                return
            if not search_strings:
                await interaction.followup.send(embed=notice("❌ No tracks found from that Spotify link.", section='Playback'))
                return
            value = f"ytsearch:{search_strings[0]}"
        elif input_type == InputType.SEARCH_QUERY:
            value = f"ytsearch:{value}"

        await self._play_single_url(interaction, value, play_next=True)

    @app_commands.command(name="stop", description="Stop playback, clear queue, and disconnect")
    async def stop(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Playback'), ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None:
            await interaction.response.send_message(embed=notice("Not connected.", section='Playback'), ephemeral=True)
            return

        gq.clear()
        self.queues.clear_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        self._cancel_crossfade_timer(interaction.guild.id)  # type: ignore[union-attr]
        self._cleanup_player(interaction.guild.id)  # type: ignore[union-attr]
        vc.stop()
        await vc.disconnect()
        metric_voice_connections.dec()
        if interaction.guild.id in self._playing_guilds:  # type: ignore[union-attr]
            self._playing_guilds.discard(interaction.guild.id)  # type: ignore[union-attr]
            metric_active_players.dec()
        self.queues.remove(interaction.guild.id)  # type: ignore[union-attr]
        await self._update_presence(None)
        await interaction.response.send_message(embed=notice("⏹️ Stopped and disconnected.", section='Playback'))

    @app_commands.command(name="skip", description="Skip the current track and play the next one in the queue")
    async def skip(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Playback'), ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Playback'), ephemeral=True)
            return

        title = gq.current.title if gq.current else "current track"
        next_up = gq.queue[0].title if gq.queue else None
        self._skip(interaction.guild)
        msg = f"Skipped **{title}**."
        if next_up:
            msg += f" Now playing **{next_up}**."
        await interaction.response.send_message(embed=notice(msg, section='Playback'))

    @app_commands.command(name="queue", description="Show the current queue")
    async def queue(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]

        view = QueueView(gq)
        await interaction.response.send_message(embed=view.build_embed(), view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="myqueue", description="Show only the tracks you have in the queue")
    async def myqueue(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        my_tracks = [
            (i + 1, t) for i, t in enumerate(gq.queue)
            if t.requester_id == interaction.user.id
            or (t.requester_id == 0 and t.requester == interaction.user.display_name)
        ]
        if not my_tracks:
            await interaction.response.send_message(
                embed=notice("You have no tracks in the queue.", section='Queue'), ephemeral=True
            )
            return
        rows = [track_row(track, position) for position, track in my_tracks]
        await send_pages(interaction, collection_pages(
            f"{interaction.user.display_name} · Your queue", rows, section="Queue",
            footer=f"{len(my_tracks)} tracks · Numbers match the server queue"), ephemeral=True)

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction) -> None:
        if err := _check_dj(interaction, self.queues.get(interaction.guild.id)):
            await interaction.response.send_message(embed=notice(err, section='Playback'), ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or not vc.is_playing():
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Playback'), ephemeral=True)
            return
        self._pause(interaction.guild)
        await interaction.response.send_message(embed=notice("⏸️ Paused.", section='Playback'))

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction) -> None:
        if err := _check_dj(interaction, self.queues.get(interaction.guild.id)):
            await interaction.response.send_message(embed=notice(err, section='Playback'), ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or not vc.is_paused():
            await interaction.response.send_message(embed=notice("Nothing is paused.", section='Playback'), ephemeral=True)
            return
        self._resume(interaction.guild)
        await interaction.response.send_message(embed=notice("▶️ Resumed.", section='Playback'))

    @app_commands.command(name="nowplaying", description="Show the currently playing track")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.current is None:
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Playback'), ephemeral=True)
            return

        vc = interaction.guild.voice_client
        embed = player_card(gq, elapsed=self._get_elapsed(gq), paused=bool(vc and vc.is_paused()))
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="player", description="Show an interactive music player with controls")
    async def player(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.current is None:
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Playback'), ephemeral=True)
            return
        gq.text_channel_id = interaction.channel_id
        await interaction.response.defer()
        await self._send_player(interaction.guild, gq)  # type: ignore[arg-type]
        await interaction.followup.send(embed=notice("🎛️ Player opened.", section='Playback'), ephemeral=True)

    @app_commands.command(name="volume", description="Adjust volume (1-100)")
    @app_commands.describe(level="Volume level from 1 to 100")
    async def volume(self, interaction: discord.Interaction, level: int) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Sound settings'), ephemeral=True)
            return
        if not 1 <= level <= 100:
            await interaction.response.send_message(
                embed=notice("Volume must be between 1 and 100.", section='Sound settings'), ephemeral=True
            )
            return

        gq.volume = level / 100

        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = gq.volume

        self.queues.save_settings()
        await interaction.response.send_message(embed=notice(f"🔊 Volume set to **{level}%**.", section='Sound settings'))

    async def _do_youtube_search(self, interaction: discord.Interaction, query: str) -> None:
        results = await YTDLSource.search(query, loop=self.bot.loop, media=self.media, guild_id=interaction.guild.id, limit=5)
        if not results:
            await interaction.followup.send(embed=notice("No results found.", section='Discover'))
            return

        embed = search_card(results, query, "YouTube")
        view = SearchView(results, self, interaction)
        await interaction.followup.send(embed=embed, view=view)

    async def _do_spotify_search(self, interaction: discord.Interaction, query: str) -> None:
        spotify = self.media.spotify(interaction.guild.id)
        if not spotify.available:
            await interaction.followup.send(
                embed=notice("Spotify is not configured for this server. Ask the owner to run /setup.", section='Discover'), ephemeral=True
            )
            return

        results = await self.bot.loop.run_in_executor(
            None, lambda: spotify.search(query, limit=5)
        )
        if not results:
            await interaction.followup.send(embed=notice("No results found.", section='Discover'))
            return

        embed = search_card(results, query, "Spotify")
        view = SearchView(results, self, interaction)
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="search", description="Search and pick from results (uses server default)")
    @app_commands.describe(query="Search keywords")
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.search_mode == "spotify":
            await self._do_spotify_search(interaction, query)
        else:
            await self._do_youtube_search(interaction, query)

    @app_commands.command(name="youtube-search", description="Search YouTube and pick from results")
    @app_commands.describe(query="Search keywords")
    async def youtube_search(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        await self._do_youtube_search(interaction, query)

    @app_commands.command(name="spotify-search", description="Search Spotify and pick from results")
    @app_commands.describe(query="Search keywords")
    async def spotify_search(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        await self._do_spotify_search(interaction, query)

    @app_commands.command(name="searchmode", description="Toggle default search between YouTube and Spotify")
    async def searchmode(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.search_mode == "youtube":
            gq.search_mode = "spotify"
        else:
            gq.search_mode = "youtube"
        self.queues.save_settings()
        await interaction.response.send_message(
            embed=notice(f"Default search mode set to **{gq.search_mode}**.", section='Server settings')
        )

    @app_commands.command(name="maxqueue", description="Set the maximum queue size")
    @app_commands.describe(size="Maximum number of tracks in the queue (1-500)")
    async def maxqueue(self, interaction: discord.Interaction, size: int) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Server settings'), ephemeral=True)
            return
        if not 1 <= size <= 500:
            await interaction.response.send_message(
                embed=notice("Max queue size must be between 1 and 500.", section='Server settings'), ephemeral=True
            )
            return
        gq.max_queue = size
        self.queues.save_settings()
        await interaction.response.send_message(embed=notice(f"📋 Max queue size set to **{size}** tracks.", section='Server settings'))

    @app_commands.command(name="maxperuser", description="Set the max tracks a single user can have in the queue (0 = unlimited)")
    @app_commands.describe(limit="Max tracks per user, 0 to remove the limit")
    async def maxperuser(self, interaction: discord.Interaction, limit: int) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Server settings'), ephemeral=True)
            return
        if limit < 0:
            await interaction.response.send_message(
                embed=notice("Limit must be 0 or higher.", section='Server settings'), ephemeral=True
            )
            return
        gq.max_per_user = limit
        self.queues.save_settings()
        if limit == 0:
            await interaction.response.send_message(embed=notice("📋 Per-user queue limit removed.", section='Server settings'))
        else:
            s = "s" if limit != 1 else ""
            await interaction.response.send_message(
                embed=notice(f"📋 Each user can now queue up to **{limit}** track{s}.", section='Server settings')
            )

    @app_commands.command(name="setnpchannel", description="Set this channel as the dedicated now-playing display channel")
    async def setnpchannel(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.manage_channels:  # type: ignore[union-attr]
            await interaction.response.send_message(
                embed=notice("You need the **Manage Channels** permission to use this.", section='Server settings'), ephemeral=True
            )
            return
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                embed=notice("❌ This must be used in a text channel.", section='Server settings'), ephemeral=True
            )
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.np_channel_id = interaction.channel_id
        gq.np_message_id = None
        self.queues.save_settings()
        await interaction.response.send_message(
            embed=notice("📺 Now-playing updates will be posted in this channel when tracks change.\n"
            "Use `/clearnpchannel` to disable.", section='Server settings')
        )

    @app_commands.command(name="clearnpchannel", description="Disable the dedicated now-playing channel")
    async def clearnpchannel(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.manage_channels:  # type: ignore[union-attr]
            await interaction.response.send_message(
                embed=notice("You need the **Manage Channels** permission to use this.", section='Server settings'), ephemeral=True
            )
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.np_channel_id = None
        gq.np_message_id = None
        self.queues.save_settings()
        await interaction.response.send_message(embed=notice("📺 Now-playing channel cleared.", section='Server settings'))

    @app_commands.command(name="remove", description="Remove a track from the queue")
    @app_commands.describe(position="Position in the queue (1-indexed)")
    async def remove(self, interaction: discord.Interaction, position: int) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Queue'), ephemeral=True)
            return
        if position < 1 or position > len(gq.queue):
            await interaction.response.send_message(
                embed=notice(f"❌ Invalid position. The queue has {len(gq.queue)} tracks.", section='Queue'), ephemeral=True
            )
            return
        gq.snapshot(f"Removed #{position}")
        removed = gq.remove_at(position - 1)
        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        await interaction.response.send_message(embed=notice(f"Removed **{removed.title}** from the queue.", section='Queue'))

    @app_commands.command(name="move", description="Move a track to a different position in the queue")
    @app_commands.describe(
        from_pos="Current position of the track (1-indexed)",
        to_pos="New position for the track (1-indexed)",
    )
    async def move(self, interaction: discord.Interaction, from_pos: int, to_pos: int) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Queue'), ephemeral=True)
            return
        n = len(gq.queue)
        if from_pos < 1 or from_pos > n or to_pos < 1 or to_pos > n:
            await interaction.response.send_message(
                embed=notice(f"❌ Invalid position. The queue has {n} tracks.", section='Queue'), ephemeral=True
            )
            return
        if from_pos == to_pos:
            await interaction.response.send_message(embed=notice("Track is already at that position.", section='Queue'), ephemeral=True)
            return
        gq.snapshot(f"Move #{from_pos} to #{to_pos}")
        moved = gq.move(from_pos - 1, to_pos - 1)
        if moved is None:
            await interaction.response.send_message(
                embed=notice(f"❌ Invalid position. The queue has {len(gq.queue)} tracks.", section='Queue'), ephemeral=True
            )
            return
        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        await interaction.response.send_message(
            embed=notice(f"Moved **{moved.title}** to position #{to_pos}.", section='Queue')
        )

    @app_commands.command(name="skipto", description="Skip to a specific position in the queue")
    @app_commands.describe(position="Position in the queue to skip to (1-indexed)")
    async def skipto(self, interaction: discord.Interaction, position: int) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Queue'), ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None:
            await interaction.response.send_message(embed=notice("Not connected.", section='Queue'), ephemeral=True)
            return

        if position < 1 or position > len(gq.queue):
            await interaction.response.send_message(
                embed=notice(f"❌ Invalid position. The queue has {len(gq.queue)} tracks.", section='Queue'), ephemeral=True
            )
            return
        gq.snapshot(f"Skip to #{position}")
        target = gq.skip_to(position - 1)

        gq.current = None
        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        vc.stop()  # triggers _play_next → pops target from front
        await interaction.response.send_message(embed=notice(f"Skipping to **{target.title}**.", section='Queue'))

    @app_commands.command(name="clear", description="Clear the queue (keeps current track playing)")
    async def clear(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Queue'), ephemeral=True)
            return
        count = len(gq.queue)
        if count == 0:
            await interaction.response.send_message(embed=notice("❌ Queue is already empty.", section='Queue'), ephemeral=True)
            return
        gq.snapshot("Clear queue")
        gq.queue.clear()
        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        s = "s" if count != 1 else ""
        await interaction.response.send_message(embed=notice(f"🗑️ Cleared **{count}** track{s} from the queue.", section='Queue'))

    @app_commands.command(name="shuffle", description="Shuffle the queue, spreading tracks from the same artist evenly")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Queue'), ephemeral=True)
            return
        if len(gq.queue) < 2:
            await interaction.response.send_message(
                embed=notice("Not enough tracks to shuffle.", section='Queue'), ephemeral=True
            )
            return
        gq.snapshot("Shuffle")
        gq.smart_shuffle()
        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        await interaction.response.send_message(embed=notice(f"🔀 Shuffled **{len(gq.queue)}** tracks.", section='Queue'))

    @app_commands.command(name="loop", description="Cycle loop mode: off → single → queue → off")
    async def loop(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Playback'), ephemeral=True)
            return
        gq.loop_mode = gq.loop_mode.next()
        self.queues.save_settings()
        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        await interaction.response.send_message(embed=notice(f"🔁 Loop: **{gq.loop_mode.label()}**.", section='Playback'))

    @app_commands.command(name="autoplay", description="Toggle autoplay — auto-queue similar tracks when the queue runs out")
    async def autoplay(self, interaction: discord.Interaction) -> None:
        spotify = self.media.spotify(interaction.guild.id)
        if not spotify.available:
            await interaction.response.send_message(
                embed=notice("Autoplay requires this server’s Spotify credentials. Ask the owner to run /setup.", section='Playback'), ephemeral=True
            )
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.autoplay = not gq.autoplay
        self.queues.save_settings()
        state = "on" if gq.autoplay else "off"
        await interaction.response.send_message(embed=notice(f"✨ Autoplay: **{state}**.", section='Playback'))

    # ── dj permissions ────────────────────────────────────────────────────

    @app_commands.command(name="dj", description="Restrict bot controls to a DJ role (admin only)")
    @app_commands.describe(role="Role to set as the DJ role")
    async def dj(
        self, interaction: discord.Interaction, role: discord.Role | None = None
    ) -> None:
        if not interaction.user.guild_permissions.administrator:  # type: ignore[union-attr]
            await interaction.response.send_message(
                embed=notice("Only admins can set the DJ role.", section='Server settings'), ephemeral=True
            )
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if role is None:
            if gq.dj_role_id:
                r = interaction.guild.get_role(gq.dj_role_id)  # type: ignore[union-attr]
                name = r.name if r else str(gq.dj_role_id)
                await interaction.response.send_message(embed=notice(f"Current DJ role: **{name}**.", section='Server settings'))
            else:
                await interaction.response.send_message(embed=notice("No DJ role is set.", section='Server settings'))
            return
        gq.dj_role_id = role.id
        self.queues.save_settings()
        await interaction.response.send_message(
            embed=notice(f"🎧 DJ role set to **{role.name}**. Only users with this role (or admins) can use music commands.", section='Server settings')
        )

    @app_commands.command(name="djclear", description="Remove the DJ role restriction so anyone can use bot controls (admin only)")
    async def djclear(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:  # type: ignore[union-attr]
            await interaction.response.send_message(
                embed=notice("Only admins can clear the DJ role.", section='Server settings'), ephemeral=True
            )
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.dj_role_id = None
        self.queues.save_settings()
        await interaction.response.send_message(embed=notice("🔓 DJ role restriction cleared.", section='Server settings'))

    # ── replay / back ───────────────────────────────────────────────────

    @app_commands.command(name="replay", description="Restart the current track from the beginning")
    async def replay(self, interaction: discord.Interaction) -> None:
        if err := _check_dj(interaction, self.queues.get(interaction.guild.id)):
            await interaction.response.send_message(embed=notice(err, section='Playback'), ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Playback'), ephemeral=True)
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        await interaction.response.defer()
        await self._restart_playback(interaction.guild, seek_seconds=0)  # type: ignore[arg-type]
        title = gq.current.title if gq.current else "current track"
        await interaction.followup.send(embed=notice(f"Replaying **{title}**.", section='Playback'))

    @app_commands.command(name="back", description="Play the previous track")
    async def back(self, interaction: discord.Interaction) -> None:
        if err := _check_dj(interaction, self.queues.get(interaction.guild.id)):
            await interaction.response.send_message(embed=notice(err, section='Playback'), ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None:
            await interaction.response.send_message(embed=notice("Not connected.", section='Playback'), ephemeral=True)
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.previous is None:
            await interaction.response.send_message(embed=notice("No previous track.", section='Playback'), ephemeral=True)
            return
        track = gq.previous
        gq.previous = None
        gq.queue.appendleft(track)
        gq.current = None
        vc.stop()  # triggers _play_next → pops track from front
        await interaction.response.send_message(embed=notice(f"Playing previous: **{track.title}**.", section='Playback'))

    # ── 24/7 mode ───────────────────────────────────────────────────────

    @app_commands.command(name="24-7", description="Toggle 24/7 mode — bot stays connected even when idle or alone")
    async def stay(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Server settings'), ephemeral=True)
            return
        gq.stay_connected = not gq.stay_connected
        self.queues.save_settings()
        state = "on" if gq.stay_connected else "off"
        await interaction.response.send_message(embed=notice(f"🕐 24/7 mode: **{state}**.", section='Server settings'))

    # ── filter / seek ────────────────────────────────────────────────────

    @app_commands.command(name="filter", description="Apply an audio filter to playback")
    @app_commands.describe(name="Audio filter to apply")
    @app_commands.choices(name=[
        app_commands.Choice(name="Bass Boost", value="bassboost"),
        app_commands.Choice(name="Nightcore", value="nightcore"),
        app_commands.Choice(name="Vaporwave", value="vaporwave"),
        app_commands.Choice(name="8D", value="8d"),
        app_commands.Choice(name="Karaoke", value="karaoke"),
        app_commands.Choice(name="None", value="none"),
    ])
    async def filter_cmd(self, interaction: discord.Interaction, name: str) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Sound settings'), ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Sound settings'), ephemeral=True)
            return
        if gq.current and gq.current.is_live:
            await interaction.response.send_message(embed=notice("Cannot apply filters to a live stream.", section='Sound settings'), ephemeral=True)
            return

        gq.filter_name = name if name != "none" else None
        self.queues.save_settings()

        await interaction.response.defer()
        elapsed = self._get_elapsed(gq)
        await self._restart_playback(interaction.guild, seek_seconds=elapsed)

        label = name if name != "none" else "off"
        await interaction.followup.send(embed=notice(f"🎛️ Filter: **{label}**.", section='Sound settings'))

    @app_commands.command(name="seek", description="Seek to a position in the current track")
    @app_commands.describe(position="Absolute (1:30, 90) or relative (+30 or -15 seconds)")
    async def seek(self, interaction: discord.Interaction, position: str) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Playback'), ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Playback'), ephemeral=True)
            return

        if gq.current and gq.current.is_live:
            await interaction.response.send_message(embed=notice("Cannot seek in a live stream.", section='Playback'), ephemeral=True)
            return

        # Handle relative seek: +30 or -15
        position = position.strip()
        relative_dir = 0
        if position.startswith("+"):
            relative_dir = 1
            position = position[1:]
        elif position.startswith("-"):
            relative_dir = -1
            position = position[1:]

        secs = parse_time(position)
        if secs is None or secs < 0:
            await interaction.response.send_message(
                embed=notice("Invalid time format. Use `90`, `1:30`, `+30`, or `-15`.", section='Playback'), ephemeral=True
            )
            return

        if relative_dir != 0:
            secs = max(0, self._get_elapsed(gq) + relative_dir * secs)

        if gq.current and gq.current.duration and secs > gq.current.duration:
            await interaction.response.send_message(embed=notice("Seek position is past the end of the track.", section='Playback'), ephemeral=True)
            return

        await interaction.response.defer()
        await self._restart_playback(interaction.guild, seek_seconds=secs)
        await interaction.followup.send(embed=notice(f"⏩ Seeked to **{format_duration(secs)}**.", section='Playback'))

    @app_commands.command(name="speed", description="Set playback speed (0.5x - 2.0x)")
    @app_commands.describe(rate="Speed multiplier (0.5 to 2.0)")
    async def speed(self, interaction: discord.Interaction, rate: float) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Sound settings'), ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Sound settings'), ephemeral=True)
            return
        if not 0.5 <= rate <= 2.0:
            await interaction.response.send_message(
                embed=notice("Speed must be between 0.5 and 2.0.", section='Sound settings'), ephemeral=True
            )
            return
        if gq.current and gq.current.is_live:
            await interaction.response.send_message(embed=notice("Cannot change speed on a live stream.", section='Sound settings'), ephemeral=True)
            return

        elapsed = self._get_elapsed(gq)
        gq.speed = rate
        self.queues.save_settings()

        await interaction.response.defer()
        await self._restart_playback(interaction.guild, seek_seconds=elapsed)
        await interaction.followup.send(embed=notice(f"⚡ Speed: **{rate}x**.", section='Sound settings'))

    @app_commands.command(name="normalize", description="Toggle loudness normalization to balance volume differences between tracks")
    async def normalize(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Sound settings'), ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Sound settings'), ephemeral=True)
            return
        if gq.current and gq.current.is_live:
            await interaction.response.send_message(embed=notice("Cannot normalize a live stream.", section='Sound settings'), ephemeral=True)
            return

        elapsed = self._get_elapsed(gq)
        gq.normalize = not gq.normalize
        self.queues.save_settings()

        await interaction.response.defer()
        await self._restart_playback(interaction.guild, seek_seconds=elapsed)
        state = "on" if gq.normalize else "off"
        await interaction.followup.send(embed=notice(f"📊 Loudness normalization: **{state}**.", section='Sound settings'))

    # ── lyrics ───────────────────────────────────────────────────────────

    @app_commands.command(name="lyrics", description="Show lyrics for the current or specified track")
    @app_commands.describe(query="Search query (defaults to current track)")
    async def lyrics(self, interaction: discord.Interaction, query: str | None = None) -> None:
        # Resolve artist + clean title from current track, or parse the user query
        artist = ""
        search_title = query or ""
        if query is None:
            gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
            if gq.current is None:
                await interaction.response.send_message(
                    embed=notice("❌ Nothing is playing. Provide a track name or URL to search for lyrics.", section='Lyrics'), ephemeral=True
                )
                return
            track = gq.current
            artist = track.artist or ""
            search_title = _clean_title(track.title)
            # If title contains "Artist - Title" pattern, split it
            if not artist and " - " in search_title:
                parts = search_title.split(" - ", 1)
                artist, search_title = parts[0].strip(), parts[1].strip()

        await interaction.response.defer()

        try:
            async with aiohttp.ClientSession() as session:
                hit = None

                # Try exact-match endpoint first (much more accurate)
                if artist and search_title:
                    async with session.get(
                        "https://lrclib.net/api/get",
                        params={"track_name": search_title, "artist_name": artist},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data and (data.get("plainLyrics") or data.get("syncedLyrics")):
                                hit = data

                # Fall back to fuzzy search
                if hit is None:
                    q = f"{artist} {search_title}".strip() if artist else search_title
                    async with session.get(
                        "https://lrclib.net/api/search",
                        params={"q": q},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status != 200:
                            await interaction.followup.send(embed=notice("Could not fetch lyrics.", section='Lyrics'))
                            return
                        results = await resp.json()
                        if results:
                            hit = results[0]
        except Exception:
            await interaction.followup.send(embed=notice("Could not fetch lyrics.", section='Lyrics'))
            return

        if hit is None:
            await interaction.followup.send(embed=notice(f"No lyrics found for **{search_title or query}**.", section='Lyrics'))
            return

        text = hit.get("plainLyrics") or hit.get("syncedLyrics") or ""
        if not text:
            await interaction.followup.send(embed=notice(f"No lyrics found for **{search_title or query}**.", section='Lyrics'))
            return

        title = hit.get("trackName", query)
        artist = hit.get("artistName", "")
        await send_pages(interaction, lyrics_pages(title, artist, text))

    # ── vote skip ────────────────────────────────────────────────────────

    @app_commands.command(name="voteskip", description="Start a skip vote — requires half the listeners to agree")
    async def voteskip(self, interaction: discord.Interaction) -> None:
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Listening together'), ephemeral=True)
            return

        # Count listeners (non-bot members in the voice channel)
        listeners = [m for m in vc.channel.members if not m.bot]
        if len(listeners) <= 1:
            # Solo — just skip
            gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
            title = gq.current.title if gq.current else "current track"
            self._skip(interaction.guild)
            await interaction.response.send_message(embed=notice(f"Skipped **{title}**.", section='Listening together'))
            return

        required = math.ceil(len(listeners) / 2)
        view = VoteSkipView(self, interaction.guild, required)  # type: ignore[arg-type]
        view.voters.add(interaction.user.id)
        view.children[0].label = f"Skip (1/{required})"  # type: ignore[union-attr]

        if 1 >= required:
            self._skip(interaction.guild)
            await interaction.response.send_message(embed=notice("Vote skip passed! Skipping...", section='Listening together'))
            return

        await interaction.response.send_message(
            embed=vote_card(self.queues.get(interaction.guild.id).current, 1, required),
            view=view,
        )
        view.message = await interaction.original_response()

    # ── history / top ────────────────────────────────────────────────────

    @app_commands.command(name="top", description="Show the most played tracks in this server")
    async def top(self, interaction: discord.Interaction) -> None:
        top_tracks = self.history.top(interaction.guild.id)  # type: ignore[union-attr]
        if not top_tracks:
            await interaction.response.send_message(embed=notice("No play history yet.", section='Listening together'), ephemeral=True)
            return

        rows = [f"`{i + 1:02d}` **{plain(title, 120)}**\n{count} plays"
                for i, (title, _url, count) in enumerate(top_tracks)]
        await send_pages(interaction, collection_pages(
            f"{interaction.guild.name} · On repeat", rows, section="Most played",
            footer="Ranked by playback starts · Latest 500 plays"))

    # ── favorites ────────────────────────────────────────────────────────

    @app_commands.command(name="fav", description="Save the current track to your favorites")
    async def fav(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.current is None:
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Your library'), ephemeral=True)
            return

        ok = self.favorites.add(
            interaction.user.id, gq.current, guild_id=interaction.guild.id  # type: ignore[union-attr]
        )
        if ok:
            await interaction.response.send_message(
                embed=notice(f"Saved **{gq.current.title}** to your favorites.", section='Your library')
            )
        else:
            await interaction.response.send_message(
                embed=notice("Already in your favorites or favorites list is full (50 max).", section='Your library'),
                ephemeral=True,
            )

    @app_commands.command(name="favs", description="List your favorite tracks")
    async def favs(self, interaction: discord.Interaction) -> None:
        favs = self.favorites.list(interaction.user.id)
        if not favs:
            await interaction.response.send_message(
                embed=notice("You have no favorites yet. Use `/fav` to save the current track.", section='Your library'),
                ephemeral=True,
            )
            return

        rows = []
        current_guild_id = interaction.guild.id
        for i, favorite in enumerate(favs):
            saved_guild = favorite.get("guild_id", 0)
            origin = "This server"
            if saved_guild and saved_guild != current_guild_id:
                guild = self.bot.get_guild(saved_guild)
                origin = plain(guild.name if guild else "Another server", 50)
            elif not saved_guild:
                origin = "All servers"
            rows.append(f"`{i + 1:02d}` **{plain(favorite['title'], 120)}**\n"
                        f"{duration(favorite.get('duration', 0))} · {origin}")
        await send_pages(interaction, collection_pages(
            f"{interaction.user.display_name} · Favorites", rows, section="Your library",
            footer=f"{len(favs)}/50 saved · /playfavs to listen · /unfav to remove"))

    @app_commands.command(name="unfav", description="Remove a track from your favorites")
    @app_commands.describe(position="Position in your favorites list (1-indexed)")
    async def unfav(self, interaction: discord.Interaction, position: int) -> None:
        removed = self.favorites.remove(interaction.user.id, position - 1)
        if removed is None:
            await interaction.response.send_message(
                embed=notice("❌ Invalid position.", section='Your library'), ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=notice(f"💔 Removed **{removed['title']}** from your favorites.", section='Your library')
        )

    @app_commands.command(name="playfavs", description="Queue your favorite tracks saved in this server")
    async def playfavs(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        tracks = self.favorites.as_tracks_for_guild(
            interaction.user.id, guild_id, requester=interaction.user.display_name
        )
        if not tracks:
            # Fallback: check if they have any favs at all (from other servers)
            all_favs = self.favorites.list(interaction.user.id)
            if all_favs:
                await interaction.response.send_message(
                    embed=notice("You have no favorites saved in this server. "
                    "Use `/fav` while music is playing, or check `/favs` to see all your favorites.", section='Your library'),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    embed=notice("You have no favorites. Use `/fav` to save tracks.", section='Your library'), ephemeral=True
                )
            return

        vc = await self._ensure_voice(interaction)
        if vc is None:
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.text_channel_id = interaction.channel_id
        count = 0
        fav_skip_reason = "queue full"
        fav_user_id = interaction.user.id
        fav_user_queued = sum(
            1 for t in gq.queue
            if t.requester_id == fav_user_id or (t.requester_id == 0 and t.requester == interaction.user.display_name)
        )
        for track in tracks:
            if gq.max_per_user > 0 and fav_user_queued >= gq.max_per_user:
                fav_skip_reason = f"per-user limit of {gq.max_per_user}"
                break
            if gq.add(track) is None:
                break
            count += 1
            fav_user_queued += 1

        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]

        if not vc.is_playing() and not vc.is_paused():
            await self._play_next(interaction.guild)  # type: ignore[arg-type]

        msg = f"Queued **{count}** favorite{'s' if count != 1 else ''}."
        if count < len(tracks):
            msg += f" ({len(tracks) - count} skipped — {fav_skip_reason})"
        if interaction.response.is_done():
            await interaction.followup.send(embed=notice(msg, section='Your library'))
        else:
            await interaction.response.send_message(embed=notice(msg, section='Your library'))

    # ── grab ────────────────────────────────────────────────────────────

    @app_commands.command(name="grab", description="Send the current track title, URL, and thumbnail to your DMs")
    async def grab(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.current is None:
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Your library'), ephemeral=True)
            return

        track = gq.current
        embed = card(title=track.title, url=track.url, section="Saved for later",
                     description=f"{plain(track.artist, 100)}" if track.artist else "A track worth coming back to.",
                     footer="Shared from Essusic · Paste the track link into /play to listen again")
        embed.add_field(name="Length", value=track_duration(track))
        embed.add_field(name="Added by", value=plain(track.requester or "Radio", 80))
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)

        try:
            await interaction.user.send(embed=embed)
            await interaction.response.send_message(embed=notice("📬 Track info sent to your DMs!", section='Your library'), ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=notice("❌ I can't DM you. Please enable DMs from server members in your Privacy Settings.", section='Your library'), ephemeral=True
            )

    # ── saved playlists ──────────────────────────────────────────────────

    playlist_group = app_commands.Group(name="playlist", description="Save and load playlists")

    async def _playlist_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        names = self.playlists.names(interaction.guild.id)  # type: ignore[union-attr]
        filtered = [n for n in names if current.lower() in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]

    @playlist_group.command(name="save", description="Save the current queue as a named playlist")
    @app_commands.describe(name="Playlist name (max 64 characters)")
    async def playlist_save(self, interaction: discord.Interaction, name: str) -> None:
        if len(name) > 64:
            await interaction.response.send_message(embed=notice("Name must be 64 characters or less.", section='Your library'), ephemeral=True)
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        tracks: list[TrackInfo] = []
        if gq.current:
            tracks.append(gq.current)
        tracks.extend(gq.queue)
        if not tracks:
            await interaction.response.send_message(embed=notice("Nothing to save — queue is empty.", section='Your library'), ephemeral=True)
            return
        err = self.playlists.save(
            interaction.guild.id, name, tracks,  # type: ignore[union-attr]
            created_by=str(interaction.user.id),
        )
        if err:
            await interaction.response.send_message(embed=notice(err, section='Your library'), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=notice(f"Saved playlist **{name}** with **{len(tracks)}** track{'s' if len(tracks) != 1 else ''}.", section='Your library')
        )

    @playlist_group.command(name="load", description="Queue tracks from a saved playlist")
    @app_commands.describe(name="Playlist name")
    @app_commands.autocomplete(name=_playlist_name_autocomplete)
    async def playlist_load(self, interaction: discord.Interaction, name: str) -> None:
        tracks = self.playlists.load(interaction.guild.id, name)  # type: ignore[union-attr]
        if tracks is None:
            await interaction.response.send_message(embed=notice(f"Playlist **{name}** not found.", section='Your library'), ephemeral=True)
            return

        vc = await self._ensure_voice(interaction)
        if vc is None:
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.text_channel_id = interaction.channel_id
        count = 0
        pl_skip_reason = "queue full"
        pl_user_id = interaction.user.id
        pl_user_queued = sum(
            1 for t in gq.queue
            if t.requester_id == pl_user_id or (t.requester_id == 0 and t.requester == interaction.user.display_name)
        )
        for track in tracks:
            if gq.max_per_user > 0 and pl_user_queued >= gq.max_per_user:
                pl_skip_reason = f"per-user limit of {gq.max_per_user}"
                break
            track.requester = interaction.user.display_name
            track.requester_id = pl_user_id
            if gq.add(track) is None:
                break
            count += 1
            pl_user_queued += 1

        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]

        if not vc.is_playing() and not vc.is_paused():
            await self._play_next(interaction.guild)  # type: ignore[arg-type]

        msg = f"Queued **{count}** track{'s' if count != 1 else ''} from **{name}**."
        if count < len(tracks):
            msg += f" ({len(tracks) - count} skipped — {pl_skip_reason})"
        if interaction.response.is_done():
            await interaction.followup.send(embed=notice(msg, section='Your library'))
        else:
            await interaction.response.send_message(embed=notice(msg, section='Your library'))

    @playlist_group.command(name="list", description="List all saved playlists")
    async def playlist_list(self, interaction: discord.Interaction) -> None:
        playlists = self.playlists.list_all(interaction.guild.id)  # type: ignore[union-attr]
        if not playlists:
            await interaction.response.send_message(embed=notice("No saved playlists.", section='Your library'), ephemeral=True)
            return
        rows = []
        for playlist in playlists:
            creator = str(playlist.get("created_by", "Unknown"))
            member = interaction.guild.get_member(int(creator)) if creator.isdigit() else None
            owner = member.display_name if member else "A server member" if creator.isdigit() else creator
            count = len(playlist.get("tracks", []))
            collaborators = len(playlist.get("collaborators", []))
            rows.append(f"**{plain(playlist['name'], 100)}**\n"
                        f"{count} tracks · By {plain(owner, 50)} · {collaborators} collaborators")
        await send_pages(interaction, collection_pages(
            f"{interaction.guild.name} · Playlists", rows, section="Server library",
            footer="/playlist load to listen · /playlist save to keep a queue"))

    @playlist_group.command(name="delete", description="Delete a saved playlist")
    @app_commands.describe(name="Playlist name")
    @app_commands.autocomplete(name=_playlist_name_autocomplete)
    async def playlist_delete(self, interaction: discord.Interaction, name: str) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        creator = self.playlists.get_creator(guild_id, name)
        if creator is None:
            await interaction.response.send_message(embed=notice(f"Playlist **{name}** not found.", section='Your library'), ephemeral=True)
            return
        is_admin = interaction.user.guild_permissions.administrator  # type: ignore[union-attr]
        is_creator = creator == str(interaction.user.id)
        gq = self.queues.get(guild_id)
        is_dj = _check_dj(interaction, gq) is None
        if not is_creator and not is_admin and not is_dj:
            await interaction.response.send_message(
                embed=notice("Only the playlist creator, a DJ, or an admin can delete playlists.", section='Your library'), ephemeral=True
            )
            return
        self.playlists.delete(guild_id, name)
        await interaction.response.send_message(embed=notice(f"Deleted playlist **{name}**.", section='Your library'))

    # ── collaborative playlists ──────────────────────────────────────────

    @playlist_group.command(name="adduser", description="Add a collaborator to a playlist")
    @app_commands.describe(name="Playlist name", user="User to add as collaborator")
    @app_commands.autocomplete(name=_playlist_name_autocomplete)
    async def playlist_adduser(
        self, interaction: discord.Interaction, name: str, user: discord.Member
    ) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        creator = self.playlists.get_creator(guild_id, name)
        if creator is None:
            await interaction.response.send_message(embed=notice(f"Playlist **{name}** not found.", section='Your library'), ephemeral=True)
            return
        # Only creator or DJ can add collaborators
        gq = self.queues.get(guild_id)
        if creator != str(interaction.user.id) and _check_dj(interaction, gq) is not None:
            await interaction.response.send_message(embed=notice("Only the playlist creator or a DJ can add collaborators.", section='Your library'), ephemeral=True)
            return
        if self.playlists.add_collaborator(guild_id, name, user.id):
            await interaction.response.send_message(embed=notice(f"Added **{user.display_name}** as collaborator on **{name}**.", section='Your library'))
        else:
            await interaction.response.send_message(embed=notice("❌ User is already a collaborator.", section='Your library'), ephemeral=True)

    @playlist_group.command(name="removeuser", description="Remove a collaborator from a playlist")
    @app_commands.describe(name="Playlist name", user="User to remove")
    @app_commands.autocomplete(name=_playlist_name_autocomplete)
    async def playlist_removeuser(
        self, interaction: discord.Interaction, name: str, user: discord.Member
    ) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        creator = self.playlists.get_creator(guild_id, name)
        if creator is None:
            await interaction.response.send_message(embed=notice(f"Playlist **{name}** not found.", section='Your library'), ephemeral=True)
            return
        gq = self.queues.get(guild_id)
        if creator != str(interaction.user.id) and _check_dj(interaction, gq) is not None:
            await interaction.response.send_message(embed=notice("Only the playlist creator or a DJ can remove collaborators.", section='Your library'), ephemeral=True)
            return
        if self.playlists.remove_collaborator(guild_id, name, user.id):
            await interaction.response.send_message(embed=notice(f"Removed **{user.display_name}** from **{name}**.", section='Your library'))
        else:
            await interaction.response.send_message(embed=notice("User is not a collaborator.", section='Your library'), ephemeral=True)

    @playlist_group.command(name="addtrack", description="Add the current track to a playlist")
    @app_commands.describe(name="Playlist name")
    @app_commands.autocomplete(name=_playlist_name_autocomplete)
    async def playlist_addtrack(self, interaction: discord.Interaction, name: str) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        gq = self.queues.get(guild_id)
        if gq.current is None:
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Your library'), ephemeral=True)
            return
        creator = self.playlists.get_creator(guild_id, name)
        if creator is None:
            await interaction.response.send_message(embed=notice(f"Playlist **{name}** not found.", section='Your library'), ephemeral=True)
            return
        is_creator = creator == str(interaction.user.id)
        is_collab = self.playlists.is_collaborator(guild_id, name, interaction.user.id)
        if not is_creator and not is_collab:
            await interaction.response.send_message(embed=notice("You must be the creator or a collaborator.", section='Your library'), ephemeral=True)
            return
        err = self.playlists.add_track_to_playlist(guild_id, name, gq.current)
        if err:
            await interaction.response.send_message(embed=notice(err, section='Your library'), ephemeral=True)
        else:
            await interaction.response.send_message(embed=notice(f"Added **{gq.current.title}** to **{name}**.", section='Your library'))

    @playlist_group.command(name="removetrack", description="Remove a track from a playlist by position")
    @app_commands.describe(name="Playlist name", position="Track position (1-indexed)")
    @app_commands.autocomplete(name=_playlist_name_autocomplete)
    async def playlist_removetrack(
        self, interaction: discord.Interaction, name: str, position: int
    ) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        creator = self.playlists.get_creator(guild_id, name)
        if creator is None:
            await interaction.response.send_message(embed=notice(f"Playlist **{name}** not found.", section='Your library'), ephemeral=True)
            return
        is_creator = creator == str(interaction.user.id)
        is_collab = self.playlists.is_collaborator(guild_id, name, interaction.user.id)
        if not is_creator and not is_collab:
            await interaction.response.send_message(embed=notice("You must be the creator or a collaborator.", section='Your library'), ephemeral=True)
            return
        removed = self.playlists.remove_track_from_playlist(guild_id, name, position - 1)
        if removed is None:
            await interaction.response.send_message(embed=notice("❌ Invalid position.", section='Your library'), ephemeral=True)
        else:
            await interaction.response.send_message(embed=notice(f"Removed **{removed['title']}** from **{name}**.", section='Your library'))

    # ── equalizer ────────────────────────────────────────────────────────

    @app_commands.command(name="eq", description="Apply an EQ preset (Flat, Bass Heavy, Treble Heavy, Vocal, Electronic)")
    @app_commands.describe(preset="Equalizer preset to apply")
    @app_commands.choices(preset=[
        app_commands.Choice(name="Flat", value="flat"),
        app_commands.Choice(name="Bass Heavy", value="bass_heavy"),
        app_commands.Choice(name="Treble Heavy", value="treble_heavy"),
        app_commands.Choice(name="Vocal", value="vocal"),
        app_commands.Choice(name="Electronic", value="electronic"),
    ])
    async def eq(self, interaction: discord.Interaction, preset: str) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Sound settings'), ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Sound settings'), ephemeral=True)
            return
        gq.eq_bands = list(EQ_PRESETS[preset])
        self.queues.save_settings()
        await interaction.response.defer()
        elapsed = self._get_elapsed(gq)
        await self._restart_playback(interaction.guild, seek_seconds=elapsed)  # type: ignore[arg-type]
        await interaction.followup.send(embed=notice(f"🎚️ EQ preset: **{preset.replace('_', ' ').title()}**.", section='Sound settings'))

    @app_commands.command(name="eqcustom", description="Boost or cut a specific EQ frequency band (-12 to +12 dB)")
    @app_commands.describe(band="Band number (1-10)", gain="Gain in dB (-12 to +12)")
    async def eqcustom(self, interaction: discord.Interaction, band: int, gain: float) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Sound settings'), ephemeral=True)
            return
        if not 1 <= band <= 10:
            await interaction.response.send_message(embed=notice("Band must be 1-10.", section='Sound settings'), ephemeral=True)
            return
        if not -12.0 <= gain <= 12.0:
            await interaction.response.send_message(embed=notice("Gain must be -12 to +12 dB.", section='Sound settings'), ephemeral=True)
            return
        vc: Optional[discord.VoiceClient] = interaction.guild.voice_client  # type: ignore[union-attr, assignment]
        if vc is None or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Sound settings'), ephemeral=True)
            return
        gq.eq_bands[band - 1] = gain
        self.queues.save_settings()
        await interaction.response.defer()
        elapsed = self._get_elapsed(gq)
        await self._restart_playback(interaction.guild, seek_seconds=elapsed)  # type: ignore[arg-type]
        from music.audio_source import EQ_BANDS
        band_name = EQ_BANDS[band - 1][0]
        await interaction.followup.send(embed=notice(f"EQ band {band} ({band_name}): **{gain:+.1f} dB**.", section='Sound settings'))

    # ── similar / radio ──────────────────────────────────────────────────

    @app_commands.command(name="similar", description="Show Spotify recommendations based on the current track")
    async def similar(self, interaction: discord.Interaction) -> None:
        spotify = self.media.spotify(interaction.guild.id)
        if not spotify.available:
            await interaction.response.send_message(embed=notice("Requires this server’s Spotify credentials. Ask the owner to run /setup.", section='Discover'), ephemeral=True)
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.current is None:
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Discover'), ephemeral=True)
            return
        # Capture title now — gq.current can become None during the async Spotify fetch
        current_title = gq.current.title
        await interaction.response.defer()
        try:
            results = await self.bot.loop.run_in_executor(
                None, lambda: spotify.recommend_multiple(current_title, 5)
            )
        except Exception:
            log.warning("Similar tracks lookup failed in guild %s", interaction.guild.id)
            results = []
        if not results:
            await interaction.followup.send(embed=notice("No similar tracks found.", section='Discover'))
            return
        embed = search_card(results, current_title, "Discover")
        view = SearchView(results, self, interaction)
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="radio", description="Start endless radio — auto-queues similar tracks by artist or genre")
    @app_commands.describe(seed="Artist name or genre to seed the radio")
    async def radio(self, interaction: discord.Interaction, seed: str) -> None:
        self.media.ensure_source(interaction.guild.id, "ytsearch:" + seed)
        spotify = self.media.spotify(interaction.guild.id)
        if not spotify.available:
            await interaction.response.send_message(embed=notice("Requires this server’s Spotify credentials. Ask the owner to run /setup.", section='Discover'), ephemeral=True)
            return
        await interaction.response.defer()
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.text_channel_id = interaction.channel_id

        try:
            results = await self.bot.loop.run_in_executor(
                None, lambda: spotify.recommend_by_seed(seed, set(), 5)
            )
        except Exception:
            log.warning("Radio seed lookup failed in guild %s", interaction.guild.id)
            results = []
        if not results:
            await interaction.followup.send(embed=notice(f"❌ No tracks found for **{seed}**.", section='Discover'))
            return

        vc = await self._ensure_voice(interaction)
        if vc is None:
            return

        gq.radio_mode = True
        gq.radio_seed = seed
        gq.radio_history = {tid for tid, _ in results}

        count = 0
        for _, track in results:
            track.requester = "Radio"
            track.requester_id = interaction.user.id
            if gq.add(track) is None:
                break
            count += 1

        if not vc.is_playing() and not vc.is_paused():
            await self._play_next(interaction.guild)  # type: ignore[arg-type]

        await interaction.followup.send(
            embed=notice(f"Radio started with **{seed}** — queued {count} tracks. "
            f"More will be added automatically.", section='Discover')
        )

    @app_commands.command(name="radio-off", description="Stop radio mode")
    async def radio_off(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if not gq.radio_mode:
            await interaction.response.send_message(embed=notice("Radio mode is not active.", section='Discover'), ephemeral=True)
            return
        gq.radio_mode = False
        gq.radio_seed = None
        gq.radio_history.clear()
        await interaction.response.send_message(embed=notice("📻 Radio mode stopped.", section='Discover'))

    # ── queue import/export ──────────────────────────────────────────────

    @app_commands.command(name="queue-export", description="Export the queue as a shareable code")
    async def queue_export(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        tracks: list[dict] = []
        if gq.current:
            tracks.append({"t": gq.current.title, "u": gq.current.url, "d": gq.current.duration})
        for t in gq.queue:
            tracks.append({"t": t.title, "u": t.url, "d": t.duration})
        if not tracks:
            await interaction.response.send_message("Nothing to export.", ephemeral=True)
            return
        data = base64.b64encode(json.dumps(tracks, separators=(",", ":")).encode()).decode()
        if len(data) <= 1900:
            await interaction.response.send_message(f"```\n{data}\n```")
        else:
            import io
            file = discord.File(io.BytesIO(data.encode()), filename="queue.txt")
            await interaction.response.send_message("Queue exported:", file=file)

    @app_commands.command(name="queue-import", description="Import a queue from an exported code")
    @app_commands.describe(code="The exported queue code")
    async def queue_import(self, interaction: discord.Interaction, code: str) -> None:
        try:
            raw = base64.b64decode(code.strip().strip("`")).decode()
            items = json.loads(raw)
        except Exception:
            await interaction.response.send_message(embed=notice("Invalid queue code.", section='Queue'), ephemeral=True)
            return
        if not isinstance(items, list) or not items:
            await interaction.response.send_message(embed=notice("❌ No tracks found in that code.", section='Queue'), ephemeral=True)
            return

        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("u"), str) or not item["u"].strip()
            or not isinstance(item.get("t", "Unknown"), str)
            or type(item.get("d", 0)) is not int or item.get("d", 0) < 0
            for item in items
        ):
            await interaction.response.send_message(embed=notice("Invalid queue code.", section='Queue'), ephemeral=True)
            return
        await interaction.response.defer()
        vc = await self._ensure_voice(interaction)
        if vc is None:
            return

        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.text_channel_id = interaction.channel_id
        count = 0
        imp_skip_reason = "queue full"
        imp_user_id = interaction.user.id
        imp_user_queued = sum(
            1 for t in gq.queue
            if t.requester_id == imp_user_id or (t.requester_id == 0 and t.requester == interaction.user.display_name)
        )
        for item in items:
            if gq.max_per_user > 0 and imp_user_queued >= gq.max_per_user:
                imp_skip_reason = f"per-user limit of {gq.max_per_user}"
                break
            track = TrackInfo(
                title=item.get("t", "Unknown"),
                url=item.get("u", ""),
                duration=item.get("d", 0),
                requester=interaction.user.display_name,
                requester_id=imp_user_id,
            )
            if gq.add(track) is None:
                break
            count += 1
            imp_user_queued += 1

        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        if not vc.is_playing() and not vc.is_paused():
            await self._play_next(interaction.guild)  # type: ignore[arg-type]

        msg = f"Imported **{count}** track{'s' if count != 1 else ''}."
        if count < len(items):
            msg += f" ({len(items) - count} skipped — {imp_skip_reason})"
        if interaction.response.is_done():
            await interaction.followup.send(embed=notice(msg, section='Queue'))
        else:
            await interaction.response.send_message(embed=notice(msg, section='Queue'))

    # ── ratings ──────────────────────────────────────────────────────────

    @app_commands.command(name="rate", description="Rate the current track (thumbs up/down)")
    async def rate(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if gq.current is None:
            await interaction.response.send_message(embed=notice("❌ Nothing is playing. Use `/play` to queue a track.", section='Listening together'), ephemeral=True)
            return
        view = RateView(self, interaction.guild.id, gq.current.url, gq.current.title)  # type: ignore[union-attr]
        embed = card(title="What do you think?", section="Rate this track",
                     description=f"**{track_link(gq.current)}**\n\nHelp shape your server’s favorites.",
                     footer="Tap again to remove your vote · /toprated to see the chart")
        if gq.current.thumbnail:
            embed.set_thumbnail(url=gq.current.thumbnail)
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="toprated", description="Show the top-rated tracks in this server")
    async def toprated(self, interaction: discord.Interaction) -> None:
        items = self.ratings.top_rated(interaction.guild.id)  # type: ignore[union-attr]
        if not items:
            await interaction.response.send_message(embed=notice("No ratings yet.", section='Listening together'), ephemeral=True)
            return
        rows = [f"`{i + 1:02d}` **{plain(title, 120)}**\n"
                f"{up} likes · {down} dislikes · **{up - down:+d}** score"
                for i, (title, _url, up, down) in enumerate(items)]
        await send_pages(interaction, collection_pages(
            f"{interaction.guild.name} · Crowd favorites", rows, section="Top rated",
            footer="Ranked by likes minus dislikes · /rate to cast your vote"))

    # ── listening stats ──────────────────────────────────────────────────

    @app_commands.command(name="stats", description="Show server-wide listening stats: top tracks, listeners, and hours played")
    async def stats(self, interaction: discord.Interaction) -> None:
        data = self.history.server_stats(interaction.guild.id)  # type: ignore[union-attr]
        if data["total_plays"] == 0:
            await interaction.response.send_message(embed=notice("No play history yet.", section='Listening together'), ephemeral=True)
            return
        listeners = {}
        for uid, _count in data["top_users"]:
            member = interaction.guild.get_member(uid)
            listeners[uid] = member.display_name if member else f"Listener {uid}"
        await interaction.response.send_message(embed=stats_card(data, interaction.guild.name, listeners=listeners))

    @app_commands.command(name="mystats", description="Show your personal listening history, top tracks, and time spent")
    async def mystats(self, interaction: discord.Interaction) -> None:
        data = self.history.user_stats(interaction.guild.id, interaction.user.id)  # type: ignore[union-attr]
        if data["total_plays"] == 0:
            await interaction.response.send_message(embed=notice("No listening history for you yet.", section='Listening together'), ephemeral=True)
            return
        await interaction.response.send_message(embed=stats_card(data, interaction.user.display_name))

    # ── DJ queue mode ────────────────────────────────────────────────────

    @app_commands.command(name="djmode", description="Require DJ approval before non-DJ users can add tracks (admin only)")
    async def djmode(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:  # type: ignore[union-attr]
            await interaction.response.send_message(embed=notice("Only admins can toggle DJ mode.", section='Server settings'), ephemeral=True)
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.dj_queue_mode = not gq.dj_queue_mode
        state = "on" if gq.dj_queue_mode else "off"
        await interaction.response.send_message(
            embed=notice(f"DJ queue mode is now **{state}**. "
            + ("Non-DJs must get approval to add tracks." if gq.dj_queue_mode else ""), section='Server settings')
        )

    # ── undo ─────────────────────────────────────────────────────────────

    @app_commands.command(name="undo", description="Revert the last change to the queue (remove, move, shuffle, etc.)")
    async def undo(self, interaction: discord.Interaction) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        desc = gq.undo()
        if desc is None:
            await interaction.response.send_message(embed=notice("Nothing to undo.", section='Queue'), ephemeral=True)
            return
        self.queues.save_queue_state(interaction.guild.id)  # type: ignore[union-attr]
        await interaction.response.send_message(embed=notice(f"Undone: **{desc}**. Queue restored.", section='Queue'))

    # ── language ──────────────────────────────────────────────────────────

    @app_commands.command(name="language", description="Set the bot language for this server")
    @app_commands.describe(lang="Language code (e.g. en, tr, de)")
    async def language(self, interaction: discord.Interaction, lang: str) -> None:
        from music.i18n import available_locales
        available = available_locales()
        if lang not in available:
            await interaction.response.send_message(
                embed=notice(f"Language **{lang}** is not available. Available: {', '.join(available) or 'en'}", section='Server settings'),
                ephemeral=True,
            )
            return
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        gq.locale = lang
        self.queues.save_settings()
        await interaction.response.send_message(embed=notice(f"Language set to **{lang}**.", section='Server settings'))

    # ── crossfade ────────────────────────────────────────────────────────

    @app_commands.command(name="crossfade", description="Set crossfade duration between tracks (0-10 seconds)")
    @app_commands.describe(seconds="Crossfade duration in seconds (0 to disable)")
    async def crossfade(self, interaction: discord.Interaction, seconds: int) -> None:
        gq = self.queues.get(interaction.guild.id)  # type: ignore[union-attr]
        if err := _check_dj(interaction, gq):
            await interaction.response.send_message(embed=notice(err, section='Sound settings'), ephemeral=True)
            return
        if not 0 <= seconds <= 10:
            await interaction.response.send_message(embed=notice("Must be 0-10 seconds.", section='Sound settings'), ephemeral=True)
            return
        gq.crossfade_seconds = seconds
        self.queues.save_settings()
        self._schedule_crossfade(interaction.guild)
        if seconds == 0:
            self._cancel_crossfade_timer(interaction.guild.id)  # type: ignore[union-attr]
            await interaction.response.send_message(embed=notice("🎵 Crossfade disabled.", section='Sound settings'))
        else:
            await interaction.response.send_message(embed=notice(f"🎵 Crossfade: **{seconds}s**.", section='Sound settings'))

    # ── help ─────────────────────────────────────────────────────────────

    @app_commands.command(name="help", description="Browse all bot commands by category")
    async def help(self, interaction: discord.Interaction) -> None:
        view = HelpView()
        await interaction.response.send_message(embed=_build_help_embed("overview"), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Auto-disconnect when alone + join notification."""
        if member.bot:
            return

        vc: Optional[discord.VoiceClient] = member.guild.voice_client  # type: ignore[assignment]
        if vc is None:
            return

        # Join notification: user joined the bot's VC
        joined_bot_vc = (
            after.channel is not None
            and after.channel == vc.channel
            and (before.channel is None or before.channel != after.channel)
        )
        if joined_bot_vc:
            gq = self.queues.get(member.guild.id)
            if gq.current and gq.text_channel_id and vc.is_playing():
                channel = member.guild.get_channel(gq.text_channel_id)
                if channel and hasattr(channel, "send"):
                    embed = player_card(gq, elapsed=self._get_elapsed(gq))
                    try:
                        await channel.send(embed=embed, delete_after=30)  # type: ignore[union-attr]
                    except discord.HTTPException:
                        pass

        # Auto-disconnect when bot is left alone
        if before.channel is None:
            return

        if vc.channel != before.channel:
            return

        non_bot_members = [m for m in before.channel.members if not m.bot]
        if len(non_bot_members) == 0:
            gq = self.queues.get(member.guild.id)
            if gq.stay_connected:
                return
            gq.clear()
            self.queues.clear_queue_state(member.guild.id)
            self._cancel_crossfade_timer(member.guild.id)
            self._cleanup_player(member.guild.id)
            vc.stop()
            await vc.disconnect()
            metric_voice_connections.dec()
            if member.guild.id in self._playing_guilds:
                self._playing_guilds.discard(member.guild.id)
                metric_active_players.dec()
            self.queues.remove(member.guild.id)
            await self._update_presence(None)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
