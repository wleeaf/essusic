import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from music.audio_source import TrackInfo
from music.queue_manager import FavoritesManager, GuildQueue, QueueManager
from music.url_parser import InputType, classify


class StateTests(unittest.TestCase):
    def test_pause_freezes_elapsed_and_resume_excludes_pause(self):
        queue = GuildQueue()
        queue.play_start_time = 100
        queue.speed = 2
        with patch('music.queue_manager.time.time', return_value=110):
            queue.pause_clock()
        with patch('music.queue_manager.time.time', return_value=150):
            self.assertEqual(queue.elapsed(), 20)
            queue.resume_clock()
        with patch('music.queue_manager.time.time', return_value=155):
            self.assertEqual(queue.elapsed(), 30)
        queue.clear()
        self.assertIsNone(queue.paused_at)
        self.assertEqual(queue.elapsed(), 0)

    def test_recovery_preserves_metadata_and_dj_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / 'settings.json'
            manager = QueueManager(settings)
            queue = manager.get(1)
            current = TrackInfo('Live', 'https://example.com/live', is_live=True,
                                artist='Artist', requester='Listener', requester_id=42)
            queued = TrackInfo('Song', 'ytsearch:Song', duration=180, requester_id=43)
            queue.current = current
            queue.add(queued)
            queue.dj_queue_mode = True
            manager.save_settings()
            manager.save_queue_state(1)
            restored = QueueManager(settings).get(1)
            self.assertEqual(list(restored.queue), [current, queued])
            self.assertTrue(restored.dj_queue_mode)
            manager.clear_queue_state(1)
            self.assertFalse(QueueManager(settings).get(1).queue)

    def test_data_directory_controls_default_storage(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, DATA_DIR=directory):
            manager = QueueManager()
            manager.get(1).add(TrackInfo('Song', 'query'))
            manager.save_queue_state(1)
            manager.save_settings()
            FavoritesManager().add(42, TrackInfo('Song', 'query'))
            self.assertEqual({p.name for p in Path(directory).iterdir()},
                             {'queue_state.json', 'settings.json', 'favorites.json'})

    def test_url_classification(self):
        cases = [
            ('https://youtube.com/live/abc', InputType.YOUTUBE_URL, None),
            ('https://youtube.com/watch?v=a&list=b', InputType.YOUTUBE_URL, 'https://youtube.com/watch?v=a'),
            ('https://music.youtube.com/browse/abc', InputType.YOUTUBE_PLAYLIST, None),
            ('https://open.spotify.com/intl-tr/track/abc?si=x', InputType.SPOTIFY_TRACK, 'abc'),
            ('https://example.com/open.spotify.com/track/abc', InputType.SEARCH_QUERY, None),
            ('https://example.com/live', InputType.RADIO_STREAM, None),
            ('https://soundcloud.com/artist/sets/album', InputType.SOUNDCLOUD_PLAYLIST, None),
            ('Artist - Song', InputType.SEARCH_QUERY, None),
        ]
        for query, kind, value in cases:
            with self.subTest(query=query):
                self.assertEqual(classify(query), (kind, value or query))

    def test_shared_youtube_song_links_ignore_playlist_context(self):
        cases = [
            ('https://music.youtube.com/watch?v=dUVY1ijQPBc&list=LM',
             'https://www.youtube.com/watch?v=dUVY1ijQPBc'),
            ('https://www.youtube.com/watch?v=dUVY1ijQPBc&list=PLexample&index=3&t=42',
             'https://www.youtube.com/watch?v=dUVY1ijQPBc&t=42'),
            ('https://music.youtube.com/watch?v=dUVY1ijQPBc&list=RDexample&start_radio=1',
             'https://www.youtube.com/watch?v=dUVY1ijQPBc'),
            ('https://youtu.be/dUVY1ijQPBc?list=LM&si=share',
             'https://youtu.be/dUVY1ijQPBc?si=share'),
            ('https://youtube.com/shorts/dUVY1ijQPBc?list=PLexample',
             'https://youtube.com/shorts/dUVY1ijQPBc'),
            ('music.youtube.com/watch?v=dUVY1ijQPBc&list=LM',
             'https://www.youtube.com/watch?v=dUVY1ijQPBc'),
        ]
        for original, expected in cases:
            with self.subTest(original=original):
                self.assertEqual(classify(original), (InputType.YOUTUBE_URL, expected))

    def test_explicit_youtube_playlist_links_still_request_collection(self):
        for url in (
            'https://music.youtube.com/playlist?list=PLexample',
            'https://youtube.com/playlist?list=RDexample',
            'https://music.youtube.com/browse/MPREexample',
            'https://youtube.com/watch?list=PLexample',
        ):
            with self.subTest(url=url):
                self.assertEqual(classify(url), (InputType.YOUTUBE_PLAYLIST, url))
