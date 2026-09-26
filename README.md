# Essusic

Essusic is a self-hosted Discord music bot with slash commands, an interactive player, and separate queues for each server. It uses discord.py for voice, yt-dlp for media extraction, and FFmpeg for audio processing.

Play YouTube and SoundCloud links, search for songs, or queue Spotify tracks, albums, and playlists. **Spotify supplies metadata; audio is found and played through YouTube**, so matches can differ from the original recording.

## Features

- **Multi-source playback** — YouTube, Spotify (tracks/playlists/albums), SoundCloud, and radio streams
- **Interactive player** — Transport controls, clickable seek bar, and volume buttons, refreshed at the bottom of the channel on track changes
- **Per-server queues** — Loop modes, smart shuffle, queue import/export, undo, and move/reorder
- **Audio controls** — Filters (bass boost, nightcore, vaporwave, 8D, karaoke), 10-band EQ with presets, speed control, loudness normalization, and crossfade
- **Radio mode** — Artist-seeded recommendations when the Spotify app has access to the required endpoints
- **Autoplay** — Automatically queues similar tracks when the queue runs out
- **Playlists** — Save/load server playlists with collaborator support
- **Favorites** — Per-user favorites that can be queued in one command
- **Search** — YouTube and Spotify search with interactive result buttons
- **Ratings & stats** — Rate tracks, view top played, top rated, and personal/server listening stats
- **Lyrics** — Fetch lyrics for the current or any track
- **DJ mode** — Restrict destructive commands to a DJ role, or enable approval queue mode
- **Localization groundwork** — JSON locale loader and `/language`; English is the only bundled locale, and most responses are currently hard-coded
- **24/7 mode** — Stay connected to voice even when idle
- **Queue recovery** — Saves current and queued tracks to JSON; restored tracks are available when playback is started again
- **Dockerized** — Single-container deployment with docker compose

## Commands

### Playback
| Command | Description |
|---|---|
| `/play <query>` | Play from URL or search |
| `/playnext <query>` | Insert track to play next |
| `/skip` | Skip current track |
| `/back` | Play previous track |
| `/stop` | Stop, clear queue, disconnect |
| `/pause` / `/resume` | Pause or resume |
| `/replay` | Restart current track |
| `/voteskip` | Vote to skip (half the listeners, rounded up) |

### Queue
| Command | Description |
|---|---|
| `/queue` | Show queue (paginated) |
| `/myqueue` | Show your queued tracks |
| `/remove <pos>` | Remove track by position |
| `/move <from> <to>` | Reorder a track |
| `/skipto <pos>` | Jump to a position |
| `/clear` | Clear queue, keep current |
| `/shuffle` | Smart shuffle (avoids same-artist clusters) |
| `/undo` | Revert last queue change |
| `/queue-export` / `/queue-import` | Share queues as codes |

### Audio
| Command | Description |
|---|---|
| `/volume <1-100>` | Set volume |
| `/filter <name>` | Bass Boost, Nightcore, Vaporwave, 8D, Karaoke, None |
| `/seek <position>` | Seek (absolute `1:30` or relative `+30`/`-15`) |
| `/speed <0.5-2.0>` | Playback speed |
| `/normalize` | Toggle loudness normalization |
| `/eq <preset>` | EQ preset (Flat, Bass Heavy, Treble Heavy, Vocal, Electronic) |
| `/eqcustom <band> <gain>` | Adjust a single EQ band |
| `/crossfade <0-10>` | Crossfade between tracks (seconds) |

### Player & Info
| Command | Description |
|---|---|
| `/player` | Interactive player with buttons |
| `/nowplaying` | Current track with progress bar |
| `/loop` | Cycle loop mode: off / single / queue |
| `/autoplay` | Auto-queue similar tracks |
| `/lyrics [query]` | Fetch lyrics |
| `/grab` | DM current track info |
| `/help` | Browse commands by category |

### Search
| Command | Description |
|---|---|
| `/search <query>` | Search (YouTube or Spotify, per server setting) |
| `/youtube-search` / `/spotify-search` | Search a specific provider |
| `/searchmode` | Toggle default search provider |

### Favorites & Playlists
| Command | Description |
|---|---|
| `/fav` / `/unfav` / `/favs` | Manage favorites |
| `/playfavs` | Queue all your favorites |
| `/playlist save/load/delete/list` | Server playlists |
| `/playlist addtrack/removetrack` | Edit playlist tracks |
| `/playlist adduser/removeuser` | Manage collaborators |

### Radio & Recommendations
| Command | Description |
|---|---|
| `/radio <seed>` | Start radio by artist |
| `/radio-off` | Stop radio |
| `/similar` | Show similar tracks |

### Stats & Ratings
| Command | Description |
|---|---|
| `/top` / `/toprated` | Most played / highest rated |
| `/rate` | Rate current track |
| `/stats` / `/mystats` | Server or personal stats |

### Settings
| Command | Description |
|---|---|
| `/dj [role]` / `/djclear` / `/djmode` | DJ role and approval mode |
| `/maxqueue` / `/maxperuser` | Queue limits |
| `/setnpchannel` / `/clearnpchannel` | Dedicated now-playing channel |
| `/24-7` | Stay connected mode |
| `/language <lang>` | Server language |

## Setup

### Requirements

- Python 3.12 or newer
- FFmpeg on `PATH`
- Node.js 22 or newer on `PATH` for YouTube extraction ([yt-dlp EJS requirements](https://github.com/yt-dlp/yt-dlp/wiki/EJS))
- A Discord application with a bot token
- Optional Spotify API credentials for Spotify search and links

Invite the bot with the `bot` and `applications.commands` scopes. Give it View Channel, Send Messages, Embed Links, Attach Files, Read Message History, Connect, and Speak permissions in the channels it uses. The bot uses default gateway intents; privileged message-content access is not required. Run commands in a server and join a voice channel before `/play`.

### Run locally

```bash
git clone https://github.com/wleeaf/essusic.git
cd essusic
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Edit .env and set DISCORD_TOKEN.
python bot.py
```

Run from the repository root. By default, settings, queues, history, favorites, playlists, ratings, and optional YouTube cookies live in `./data/`, which is created as needed. Set `DATA_DIR` to use another directory. Existing local installations that used `/data` should set `DATA_DIR=/data` or move their saved files into `./data`.

### Configuration

| Variable | Required | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Bot token from the [Discord Developer Portal](https://discord.com/developers/applications) |
| `SPOTIFY_CLIENT_ID` | For Spotify | Client ID from the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) |
| `SPOTIFY_CLIENT_SECRET` | For Spotify | Spotify client secret |
| `DATA_DIR` | No | Storage directory; defaults to `data` locally and `/data` in the image |
| `WEB_PORT` | No | Enables the optional HTTP API on this port |
| `WEB_API_TOKEN` | When `WEB_PORT` is set | Secret bearer token granting operator access to all bot guilds |
| `WEB_HOST` | No | HTTP bind address; defaults to `127.0.0.1` |

For YouTube sessions that need cookies, place a Netscape-format cookie file at `data/cookies.txt` (or under `DATA_DIR`). Cookies and runtime data are excluded from Git and Docker build contexts. Cookies do not guarantee that YouTube will accept a request.

### Run with Docker Compose

Create `.env` with the bot token, then run:

```bash
docker compose up -d --build
docker compose logs -f bot
```

The image includes Python 3.12, FFmpeg, and Node.js 22. Compose mounts `./data` at `/data`; leave `DATA_DIR` unset in `.env` to use that mount. Back up `data/` to preserve saved playlists and settings. The Compose file does not publish HTTP or metrics ports.

`deploy.sh <ssh-host> [browser]` is an optional deployment helper for an **existing** checkout at `/opt/essusic` on the target. It pushes Git commits, copies YouTube cookies, pulls the remote checkout, and rebuilds the container. The target needs its own `.env`, Docker Compose, and repository access.

## Optional HTTP API

This is an operator JSON API, with no browser frontend or Discord OAuth login. Set both `WEB_PORT` and a strong `WEB_API_TOKEN`; without the token the HTTP server will not start. `/health` is public. All `/api` requests require `Authorization: Bearer <WEB_API_TOKEN>`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Process status and guild/shard counts |
| GET | `/api/guilds/{guild_id}/queue` | Current track, queue, and playback settings |
| POST | `/api/guilds/{guild_id}/skip` | Skip playing or paused audio |
| POST | `/api/guilds/{guild_id}/volume` | Set volume with JSON `{"level": 50}` (integer 1–100) |
| GET | `/api/guilds/{guild_id}/playlists` | Saved playlist summaries |
| GET | `/api/guilds/{guild_id}/stats` | Server play statistics |

The token is an operator credential, not a per-user Discord permission check. Keep the default loopback binding for local use. For container access, set `WEB_HOST=0.0.0.0` and explicitly publish the port; use HTTPS if exposing the API beyond a trusted local network.

Prometheus metrics are optional: install `prometheus-client` to start a metrics listener on port 9090. Metrics are not protected by the HTTP API token.

## Behavior and limitations

- Spotify availability depends on the application's API access. Radio, autoplay, and `/similar` use related-artist/top-track endpoints; these may be unavailable under [Spotify's API restrictions](https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api).
- Recovery restores the interrupted track at the front of the queue. It does not reconnect to voice automatically or resume at the saved timestamp. `/stop` and automatic disconnect clear recovery state.
- History retains the latest 500 playback starts per server. Listening-time statistics use track durations, rather than measuring how long each listener actually listened.
- Crossfade requires a known track duration and a queued next track. It is disabled during single-track looping. Media availability and extraction can still fail upstream.
- `/24-7` keeps the bot connected while idle or alone. Otherwise it disconnects when left alone, or after five minutes of idle playback.

## Development

```bash
python -m unittest discover -s tests -v
python -m compileall -q bot.py cogs music web
```

The regression suite runs without Discord/Spotify credentials, network media access, or FFmpeg. It covers playback transitions, concurrent requests, queue recovery, URL parsing, and the HTTP API. CI runs it on Python 3.12 and 3.14. Use [the manual test plan](test-todo.md) for real Discord voice playback and third-party integrations.

| Location | Responsibility |
|---|---|
| `bot.py` | Startup, command registration, optional HTTP/metrics services |
| `cogs/music_cog.py` | Slash commands, player UI, voice playback orchestration |
| `music/audio_source.py` | yt-dlp extraction, FFmpeg filters, PCM crossfade |
| `music/queue_manager.py` | Per-server state and JSON-backed managers |
| `music/spotify_resolver.py` | Spotify metadata and recommendations |
| `music/url_parser.py` | Input classification |
| `music/config.py` | Shared storage paths |
| `web/app.py` | Authenticated operator API |
| `tests/` | Automated regression tests |
