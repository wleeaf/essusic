import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cogs.music_cog import (
    DJApprovalView,
    HelpView,
    PlayerView,
    QueueView,
    SearchView,
    _HELP_CATEGORIES,
    _build_help_embed,
)
from music.audio_source import TrackInfo
from music.presentation import (
    ACCENT,
    WARNING,
    PagesView,
    collection_pages,
    duration,
    lyrics_pages,
    notice,
    player_card,
    progress,
    search_card,
    send_pages,
    stats_card,
)
from music.queue_manager import GuildQueue


def assert_discord_limits(test, embed):
    test.assertLessEqual(len(embed), 6000)
    test.assertLessEqual(len(embed.title or ""), 256)
    test.assertLessEqual(len(embed.description or ""), 4096)
    test.assertLessEqual(len(embed.fields), 25)
    for field in embed.fields:
        test.assertLessEqual(len(field.name), 256)
        test.assertLessEqual(len(field.value), 1024)


class PresentationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.queue = GuildQueue()
        self.queue.current = TrackInfo(
            "Midnight Drive",
            "https://example.com/song",
            duration=240,
            artist="Sample artist",
            requester="Listener",
        )
        for i in range(50):
            self.queue.add(
                TrackInfo(
                    f"Song {i + 1}",
                    f"https://example.com/{i}",
                    duration=180,
                    requester="Listener",
                )
            )

    def test_zero_timestamp_and_live_are_distinct(self):
        self.assertEqual(duration(0), "0:00")
        self.assertIn("`0:00`", progress(0, 240))
        self.assertNotIn("LIVE", player_card(self.queue).description)
        self.queue.current.is_live = True
        self.assertIn("LIVE", player_card(self.queue).description)

    def test_long_metadata_respects_embed_limits(self):
        self.queue.current.title = "*" * 500
        self.queue.current.artist = "*" * 500
        self.queue.current.requester = "*" * 500
        self.queue.queue[0].title = "*" * 500
        self.queue.queue[0].url = "https://example.com/" + "a" * 3000
        assert_discord_limits(self, player_card(self.queue))
        assert_discord_limits(
            self, search_card(list(self.queue.queue)[:5], "*" * 500, "YouTube")
        )
        assert_discord_limits(self, notice("Queued " + "x" * 8000, section="Queue"))

    def test_player_status_and_modes(self):
        self.queue.crossfade_seconds = 5
        self.queue.speed = 1.5
        self.queue.normalize = True
        playing = player_card(self.queue, elapsed=90)
        paused = player_card(self.queue, elapsed=90, paused=True)
        self.assertEqual(playing.color.value, ACCENT)
        self.assertEqual(paused.color.value, WARNING)
        self.assertIn("PAUSED", paused.author.name)
        modes = next(
            field.value for field in playing.fields if field.name == "Sound & session"
        )
        self.assertIn("1.5× speed", modes)
        self.assertIn("5s crossfade", modes)
        self.assertIn("Normalized", modes)

    def test_queue_pages_keep_track_positions_and_clamp_after_clear(self):
        view = QueueView(self.queue, page=8)
        last = view.build_embed()
        self.assertIn("`49`", last.description)
        self.assertIn("`50`", last.description)
        self.assertTrue(view.next_btn.disabled)
        self.queue.queue.clear()
        empty = view.build_embed()
        self.assertEqual(view.page, 0)
        self.assertIn("No upcoming tracks", empty.description)
        self.assertTrue(view.prev_btn.disabled)
        assert_discord_limits(self, empty)

    async def test_paginated_collection_preserves_all_items(self):
        rows = [f"ROW-{i:02d} " + "x" * 300 for i in range(50)]
        pages = collection_pages("Favorites", rows, section="Library")
        self.assertEqual(len(pages), 9)
        joined = "".join(page.description for page in pages)
        for row in rows:
            self.assertIn(row, joined)
        for page in pages:
            assert_discord_limits(self, page)
        view = PagesView(pages)
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock())
        )
        await view.next_page.callback(interaction)
        self.assertEqual(view.page, 1)
        self.assertEqual(view.position.label, "2 / 9")
        interaction.response.edit_message.assert_awaited_once_with(
            embed=pages[1], view=view
        )
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        self.assertTrue(all(child.disabled for child in view.children))
        view.message.edit.assert_awaited_once_with(view=view)

    def test_lyrics_keep_every_line_across_pages(self):
        lines = [f"Original sample line {i:03d}" for i in range(300)]
        pages = lyrics_pages("Sample", "Artist", "\n".join(lines))
        joined = "\n".join(page.description for page in pages)
        self.assertEqual(joined, "\n".join(lines))
        for page in pages:
            assert_discord_limits(self, page)

    def test_all_help_sections_fit_and_offer_a_home_option(self):
        for category in ["overview"] + [cid for _, cid, _ in _HELP_CATEGORIES]:
            assert_discord_limits(self, _build_help_embed(category))
        view = HelpView()
        self.assertEqual(view._select.options[0].value, "overview")

    def test_player_components_fit_and_live_seeking_is_disabled(self):
        cog = SimpleNamespace(
            queues=SimpleNamespace(get=lambda _: self.queue), _get_elapsed=lambda _: 30
        )
        guild = SimpleNamespace(
            id=1, voice_client=SimpleNamespace(is_paused=lambda: False)
        )
        player = PlayerView(cog, guild)
        player._sync_pause_button()
        components = player.to_components()
        self.assertLessEqual(len(components), 5)
        self.assertTrue(all(len(row["components"]) <= 5 for row in components))
        self.assertEqual(player.pause_resume_btn.label, "Pause")
        self.queue.current.is_live = True
        player._rebuild_seek_bar()
        player._sync_pause_button()
        self.assertTrue(player.rewind_btn.disabled)
        self.assertTrue(player.forward_btn.disabled)
        self.assertFalse(any(child.row == 1 for child in player.children))

    def test_search_dropdown_limits(self):
        tracks = [TrackInfo("x" * 300, "query", artist="y" * 300) for _ in range(5)]
        view = SearchView(tracks, Mock(), Mock())
        self.assertEqual(len(view.children), 1)
        self.assertEqual(len(view.select.options), 5)
        for option in view.select.options:
            self.assertLessEqual(len(option.label), 100)
            self.assertLessEqual(len(option.description), 100)

    def test_stats_fields_fit_with_long_names(self):
        data = {
            "total_plays": 500,
            "total_time_seconds": 50000,
            "unique_tracks": 100,
            "top_tracks": [("*" * 500, 100)] * 5,
            "top_users": [(i, 100) for i in range(5)],
        }
        assert_discord_limits(
            self,
            stats_card(data, "x" * 300, listeners={i: "*" * 500 for i in range(5)}),
        )

    async def test_single_page_does_not_pass_none_as_a_discord_view(self):
        for deferred in [False, True]:
            interaction = SimpleNamespace(
                response=SimpleNamespace(
                    is_done=lambda: deferred, send_message=AsyncMock()
                ),
                followup=SimpleNamespace(send=AsyncMock()),
                original_response=AsyncMock(),
            )
            page = player_card(self.queue)
            await send_pages(interaction, [page], ephemeral=True)
            send = (
                interaction.followup.send
                if deferred
                else interaction.response.send_message
            )
            self.assertNotIn("view", send.await_args.kwargs)
            self.assertIs(send.await_args.kwargs["embed"], page)
            self.assertTrue(send.await_args.kwargs["ephemeral"])

    async def test_multi_page_message_is_available_for_timeout_edit(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock()),
            original_response=AsyncMock(return_value=Mock()),
        )
        await send_pages(
            interaction, [player_card(self.queue), player_card(GuildQueue())]
        )
        view = interaction.response.send_message.await_args.kwargs["view"]
        self.assertIs(view.message, interaction.original_response.return_value)
        view.stop()

    async def test_approved_request_stops_expiry_timer(self):
        self.queue.queue.clear()
        track = self.queue.current
        self.queue.pending_requests.append(track)
        cog = SimpleNamespace(queues=SimpleNamespace(get=lambda _: self.queue))
        guild = SimpleNamespace(id=1, voice_client=None)
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock())
        )
        view = DJApprovalView(cog, guild, track)
        await view.approve.callback(interaction)
        self.assertTrue(view.is_finished())
        self.assertTrue(all(child.disabled for child in view.children))
        self.assertEqual(
            interaction.response.edit_message.await_args.kwargs["embed"].title,
            "Approved",
        )
