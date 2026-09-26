import asyncio
import base64
import json
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.music_cog import MusicCog, SearchView
from music.audio_source import CrossfadeSource, TrackInfo, YTDLSource
from music.queue_manager import LoopMode, QueueManager


class PCMSource(discord.AudioSource):
    def __init__(self):
        self.cleaned = False

    def read(self):
        if self.cleaned:
            return b''
        return struct.pack('<1920h', *([1000] * 1920))

    def cleanup(self):
        self.cleaned = True


def source():
    return YTDLSource(PCMSource(), data={'duration': 120, 'thumbnail': 'cover'},
                      stream_url='https://example.com/audio', volume=0.5)


class VoiceClient:
    def __init__(self):
        self.source = source()
        self.playing = True
        self.paused = False
        self.stop_calls = 0
        self.play_calls = 0

    def is_connected(self):
        return True

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return self.paused

    def pause(self):
        self.playing = False
        self.paused = True

    def resume(self):
        self.playing = True
        self.paused = False

    def stop(self):
        self.stop_calls += 1
        self.playing = self.paused = False
        self.source.cleanup()

    def play(self, new_source, **kwargs):
        self.source = new_source
        self.playing = True
        self.paused = False
        self.play_calls += 1


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cog = MusicCog.__new__(MusicCog)
        self.cog.bot = SimpleNamespace(loop=asyncio.get_running_loop())
        self.cog.queues = QueueManager(self.directory.name + '/settings.json')
        self.cog.history = Mock()
        self.cog._playing_guilds = set()
        self.cog._play_locks = {}
        self.cog._crossfade_timers = {}
        self.cog._update_presence = AsyncMock()
        self.cog._send_player = AsyncMock()
        self.cog._update_np_channel = AsyncMock()
        self.cog._notify_text_channel = AsyncMock()
        self.cog.spotify = SimpleNamespace(available=False)
        self.vc = VoiceClient()
        self.guild = SimpleNamespace(id=1, voice_client=self.vc)
        self.queue = self.cog.queues.get(1)
        self.queue.current = TrackInfo('First', 'first', duration=120)
        self.queue.add(TrackInfo('Second', 'second', duration=120))
        self.addCleanup(self.cog._cancel_crossfade_timer, 1)

    async def test_crossfade_keeps_outgoing_alive_and_applies_volume_once(self):
        outgoing = self.vc.source
        incoming = source()
        self.queue.crossfade_seconds = 1
        self.queue.loop_mode = LoopMode.QUEUE
        with patch.object(YTDLSource, 'from_query', AsyncMock(return_value=incoming)):
            await self.cog._start_crossfade(self.guild)
        self.assertEqual(self.vc.stop_calls, 0)
        self.assertFalse(outgoing.original.cleaned)
        self.assertEqual(self.queue.current.title, 'Second')
        self.assertEqual(self.queue.queue[-1].title, 'First')
        samples = struct.unpack('<1920h', self.vc.source.read())
        self.assertEqual(samples[0], 500)
        self.vc.source.cleanup()
        self.assertTrue(outgoing.original.cleaned)
        self.assertTrue(incoming.original.cleaned)

    async def test_completed_crossfade_releases_outgoing_source(self):
        outgoing = PCMSource()
        incoming = PCMSource()
        mixer = CrossfadeSource(outgoing, incoming, crossfade_seconds=1)
        for _ in range(50):
            self.assertTrue(mixer.read())
        self.assertTrue(outgoing.cleaned)
        self.assertIsNone(mixer.outgoing)
        self.assertFalse(incoming.cleaned)
        self.assertTrue(mixer.read())
        mixer.cleanup()
        self.assertTrue(incoming.cleaned)

    async def test_empty_youtube_search_returns_no_results(self):
        with patch('music.audio_source.yt_dlp.YoutubeDL') as extractor:
            extractor.return_value.extract_info.return_value = None
            results = await YTDLSource.search('missing', loop=asyncio.get_running_loop())
        self.assertEqual(results, [])

    async def test_crossfade_does_not_override_single_loop(self):
        self.queue.crossfade_seconds = 5
        self.queue.loop_mode = LoopMode.SINGLE
        with patch.object(YTDLSource, 'from_query', AsyncMock()) as fetch:
            await self.cog._start_crossfade(self.guild)
        fetch.assert_not_awaited()
        self.assertEqual(self.queue.current.title, 'First')

    async def test_restart_failure_preserves_current_audio(self):
        original = self.vc.source
        with patch.object(YTDLSource, 'from_stream_url', side_effect=ValueError('fetch failed')):
            with self.assertRaises(ValueError):
                await self.cog._restart_playback(self.guild, 30)
        self.assertIs(self.vc.source, original)
        self.assertTrue(self.vc.is_playing())
        self.assertFalse(original.original.cleaned)
        self.assertEqual(self.vc.stop_calls, 0)

    async def test_seeking_while_paused_preserves_pause_and_position(self):
        self.vc.pause()
        old = self.vc.source
        with patch.object(YTDLSource, 'from_stream_url', return_value=source()):
            await self.cog._restart_playback(self.guild, 30)
        self.assertTrue(self.vc.is_paused())
        self.assertEqual(self.queue.elapsed(), 30)
        self.assertTrue(old.original.cleaned)
        self.assertEqual(self.vc.stop_calls, 0)

    async def test_manual_skip_advances_single_loop(self):
        self.queue.loop_mode = LoopMode.SINGLE
        self.cog._skip(self.guild)
        self.assertEqual(self.queue.next_track().title, 'Second')
        self.assertEqual(self.queue.previous.title, 'First')
        self.assertEqual(self.queue.loop_mode, LoopMode.SINGLE)

    async def test_concurrent_play_requests_do_not_drop_tracks(self):
        self.vc.stop()
        self.queue.current = None
        self.queue.add(TrackInfo('Third', 'third'))
        started = asyncio.Event()
        release = asyncio.Event()

        async def fetch(*args, **kwargs):
            started.set()
            await release.wait()
            return source()

        with patch.object(YTDLSource, 'from_query', side_effect=fetch) as resolver:
            first = asyncio.create_task(self.cog._play_next(self.guild))
            await started.wait()
            second = asyncio.create_task(self.cog._play_next(self.guild))
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual(resolver.await_count, 1)
        self.assertEqual(self.vc.play_calls, 1)
        self.assertEqual(self.queue.current.title, 'Second')
        self.assertEqual(self.queue.queue[0].title, 'Third')

    async def test_stop_during_resolution_discards_source(self):
        self.vc.stop()
        incoming = source()

        async def fetch(*args, **kwargs):
            self.queue.clear()
            return incoming

        with patch.object(YTDLSource, 'from_query', side_effect=fetch):
            await self.cog._play_next(self.guild)
        self.assertTrue(incoming.original.cleaned)
        self.assertEqual(self.vc.play_calls, 0)

    async def test_failed_track_is_not_retried_in_single_loop(self):
        self.vc.stop()
        self.queue.loop_mode = LoopMode.SINGLE
        with patch.object(YTDLSource, 'from_query', side_effect=[ValueError('unavailable'), source()]) as fetch:
            await self.cog._play_next(self.guild)
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(self.queue.current.title, 'Second')

    async def test_search_selection_uses_queue_validation(self):
        self.cog._enqueue_and_play = AsyncMock()
        view = SearchView.__new__(SearchView)
        view.cog = self.cog
        interaction = SimpleNamespace(user=SimpleNamespace(id=42, display_name='Listener'),
                                      response=SimpleNamespace(defer=AsyncMock()))
        track = TrackInfo('Song', 'query')
        await view._make_callback(track)(interaction)
        self.cog._enqueue_and_play.assert_awaited_once_with(interaction, track)
        self.assertEqual(track.requester_id, 42)

    async def test_invalid_import_is_rejected_before_voice_connection(self):
        self.cog._ensure_voice = AsyncMock()
        for items in [[None], [{'u': 'url', 'd': 'bad'}], [{'t': 'no URL'}]]:
            interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
            code = base64.b64encode(json.dumps(items).encode()).decode()
            await MusicCog.queue_import.callback(self.cog, interaction, code)
            interaction.response.send_message.assert_awaited_once()
        self.cog._ensure_voice.assert_not_awaited()

    async def test_dm_commands_are_rejected(self):
        interaction = SimpleNamespace(guild=None, response=SimpleNamespace(send_message=AsyncMock()))
        self.assertFalse(await self.cog.play._check_can_run(interaction))
        interaction.response.send_message.assert_awaited_once()
