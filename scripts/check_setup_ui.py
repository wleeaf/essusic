"""Exercise the setup page offline and capture desktop/mobile previews.

Optional dependency: pip install playwright && playwright install chromium
Run from the repository root: python scripts/check_setup_ui.py
All credentials, guilds and provider replies below are synthetic.
"""

import asyncio
import base64
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import web
from playwright.async_api import async_playwright

from music.credentials import CredentialStore
from music.providers import MediaService
from web.app import create_app
from web.setup import SetupPortal


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        store = CredentialStore(Path(tmp), base64.b64encode(b"p" * 32).decode())
        media = MediaService(store)
        media._extract = Mock(
            return_value={"url": "https://example.invalid/mock-audio"}
        )
        guild = SimpleNamespace(id=1, owner_id=100, name="The Listening Room")
        cog = SimpleNamespace(
            credentials=store, media=media, revoke_source_playback=AsyncMock()
        )
        bot = SimpleNamespace(
            get_guild=lambda gid: guild if gid == 1 else None, get_cog=lambda name: cog
        )
        # Bind an available loopback port before constructing the public origin.
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        portal = SetupPortal(bot, cog, f"http://127.0.0.1:{port}")
        runner = web.AppRunner(create_app(bot, "", portal))
        await runner.setup()
        await web.SockSite(runner, sock).start()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                page = await browser.new_page(
                    viewport={"width": 1440, "height": 1100}, device_scale_factor=1
                )
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(portal.issue(1, 100))
                await page.locator("#workspace").wait_for(state="visible")
                assert "#" not in page.url
                assert await page.locator("#guild-name").inner_text() == guild.name
                Path("docs").mkdir(exist_ok=True)
                await page.screenshot(path="docs/setup-preview.png", full_page=True)
                await page.locator("#cookies").set_input_files(
                    {
                        "name": "youtube-cookies.txt",
                        "mimeType": "text/plain",
                        "buffer": b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tpreview-only\n",
                    }
                )
                await page.get_by_role("button", name="Save YouTube session").click()
                await page.locator('#youtube-state[data-state="configured"]').wait_for()
                assert await page.locator("#cookies").input_value() == ""
                await page.locator("[data-test=youtube]").click()
                await page.locator('#youtube-state[data-state="ready"]').wait_for()
                await page.locator("#client-id").fill("a" * 32)
                await page.locator("#client-secret").fill("b" * 32)
                await page.get_by_role(
                    "button", name="Save Spotify credentials"
                ).click()
                await page.locator('#spotify-state[data-state="configured"]').wait_for()
                assert await page.locator("#client-secret").input_value() == ""
                await page.set_viewport_size({"width": 390, "height": 844})
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                await page.screenshot(
                    path="docs/setup-mobile-preview.png", full_page=True
                )
                for width in (320, 768):
                    await page.set_viewport_size({"width": width, "height": 844})
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    ), width
                await page.locator("[data-remove=youtube]").click()
                await page.locator('#youtube-state[data-state="missing"]').wait_for()
                assert store.source(1, "youtube") is None
                await page.locator("#logout").click()
                await page.locator("#workspace").wait_for(state="hidden")
                await page.reload()
                await page.locator("#locked").wait_for(state="visible")
                assert not errors, errors
                await browser.close()
                print(
                    "Setup browser checks passed; desktop and mobile previews saved in docs/."
                )
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
