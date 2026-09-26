import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from aiohttp.test_utils import TestClient, TestServer

from music.queue_manager import GuildQueue
from web.app import create_app


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.queue = GuildQueue()
        self.cog = SimpleNamespace(
            queues=SimpleNamespace(get=Mock(return_value=self.queue), save_settings=Mock()),
            _skip=Mock(),
        )
        self.guild = SimpleNamespace(id=1, voice_client=None)
        bot = SimpleNamespace(
            guilds=[self.guild], shard_count=1,
            get_guild=lambda guild_id: self.guild if guild_id == 1 else None,
            get_cog=lambda name: self.cog,
        )
        self.client = TestClient(TestServer(create_app(bot, 'test-token')))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.headers = {'Authorization': 'Bearer test-token'}

    async def test_api_rejects_missing_and_invalid_tokens(self):
        for method, endpoint in [('get', 'queue'), ('get', 'playlists'), ('get', 'stats'),
                                 ('post', 'skip'), ('post', 'volume')]:
            for token in ['', 'Bearer wrong']:
                response = await getattr(self.client, method)(
                    '/api/guilds/1/' + endpoint, headers={'Authorization': token})
                self.assertEqual(response.status, 401)
        self.cog.queues.get.assert_not_called()

    async def test_health_is_public_and_api_accepts_token(self):
        response = await self.client.get('/health')
        self.assertEqual(response.status, 200)
        response = await self.client.get('/api/guilds/1/queue', headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['volume'], 0.5)

    async def test_volume_rejects_invalid_json_and_types_without_mutation(self):
        for body in [None, [], {'level': 'loud'}, {'level': True}, {'level': 1.5},
                     {'level': 0}, {'level': 101}, {}]:
            response = await self.client.post('/api/guilds/1/volume',
                                              json=body, headers=self.headers)
            self.assertEqual(response.status, 400)
        response = await self.client.post('/api/guilds/1/volume', data='{', headers=self.headers)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.queue.volume, 0.5)
        self.cog.queues.save_settings.assert_not_called()

    async def test_valid_volume_is_saved(self):
        response = await self.client.post('/api/guilds/1/volume',
                                          json={'level': 25}, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.queue.volume, 0.25)
        self.cog.queues.save_settings.assert_called_once()

    async def test_unknown_or_malformed_guild_does_not_create_queue(self):
        for guild_id, status in [('abc', 400), ('999', 404)]:
            response = await self.client.get('/api/guilds/' + guild_id + '/queue', headers=self.headers)
            self.assertEqual(response.status, status)
        self.cog.queues.get.assert_not_called()

    async def test_skip_works_when_paused(self):
        self.guild.voice_client = SimpleNamespace(is_playing=lambda: False, is_paused=lambda: True)
        response = await self.client.post('/api/guilds/1/skip', headers=self.headers)
        self.assertEqual(response.status, 200)
        self.cog._skip.assert_called_once_with(self.guild)

    def test_empty_token_cannot_start_api(self):
        with self.assertRaises(ValueError):
            create_app(Mock(), '')
