# Essusic

A feature-rich Discord music bot that streams audio from YouTube and Spotify. Built with discord.py 2.7+, yt-dlp, and spotipy. All commands are slash commands.

## Features

- **Multi-source playback** — YouTube, Spotify (tracks/playlists/albums), SoundCloud, and radio streams
- **Interactive player** — Transport controls, clickable seek bar, and volume buttons, always pinned to the bottom of the channel
- **Per-server queues** — Loop modes, smart shuffle, queue import/export, undo, and move/reorder
- **Audio controls** — Filters (bass boost, nightcore, vaporwave, 8D, karaoke), 10-band EQ with presets, speed control, loudness normalization, and crossfade
- **Radio mode** — Continuous playback of similar tracks seeded by artist via Spotify's related artists
- **Autoplay** — Automatically queues similar tracks when the queue runs out
- **Playlists** — Save/load server playlists with collaborator support
- **Favorites** — Per-user favorites that can be queued in one command
- **Search** — YouTube and Spotify search with interactive result buttons
- **Ratings & stats** — Rate tracks, view top played, top rated, and personal/server listening stats
- **Lyrics** — Fetch lyrics for the current or any track
- **DJ mode** — Restrict destructive commands to a DJ role, or enable approval queue mode
- **Localization** — Multi-language support via JSON locale files
- **24/7 mode** — Stay connected to voice even when idle
- **Crash recovery** — Queue state persisted to disk and restored on restart
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
| `/voteskip` | Vote to skip (majority required) |

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

### Prerequisites
- Python 3.12+
- FFmpeg
- Node.js (for yt-dlp)

### Run locally

```bash
git clone https://github.com/wleeaf/essusic.git
cd essusic
pip install -r requirements.txt
cp .env.example .env  # fill in tokens
python3 bot.py
```

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Bot token from the [Discord Developer Portal](https://discord.com/developers/applications) |
| `SPOTIFY_CLIENT_ID` | For Spotify | From the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) |
| `SPOTIFY_CLIENT_SECRET` | For Spotify | From the Spotify Developer Dashboard |
| `WEB_PORT` | No | Enables the web dashboard on this port |

### Run with Docker

```bash
docker compose up -d --build
```

YouTube cookies (`cookies.txt` in Netscape format) should be placed in `./data/cookies.txt` to avoid sign-in blocks.

## License

MIT
