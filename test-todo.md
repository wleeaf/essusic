# Essusic Test Plan

Test in order — earlier sections set up state for later ones.

---

## 0. Owner setup and isolation

Use dedicated test accounts and two Discord servers. Do not reuse production cookies in fixtures.

- [ ] Install via `/invite`; the new server works without operator music credentials.
- [ ] A non-owner administrator cannot open `/setup`; the owner receives an ephemeral private link.
- [ ] Open a link once, then confirm reuse and expired links fail. Confirm a new link revokes the old session.
- [ ] Upload fresh YouTube cookies for server A, test, and play; server B still requires its own setup.
- [ ] Configure different Spotify applications in A and B; verify search and playlist resolution use each server's app.
- [ ] Confirm expired/rejected cookies show an actionable failure without raw provider output.
- [ ] Replace/remove YouTube credentials during playback and crossfade: audio stops, its queue clears, and pending extraction cannot resume old audio.
- [ ] Transfer ownership, remove/reinvite the bot, and repeat after an offline interval: the former owner's credentials are removed.
- [ ] Restart the host: saved credentials remain usable with the same encryption key; browser setup sessions are revoked.
- [ ] Complete setup over public HTTPS and privately through the documented SSH tunnel.
- [ ] Check connection tests with real YouTube and Spotify access; offline tests only validate plumbing and isolation.

## 1. Basic Playback

- [ ] `/play <youtube url>` — single video, should join VC and start playing
- [ ] `/play <search keywords>` — should search YouTube and play the first result
- [ ] `/play <youtube playlist url>` — should queue all tracks from the playlist
- [ ] `/play <music.youtube.com/watch?v=xxx>` — YouTube Music single track
- [ ] `/play <music.youtube.com/playlist?list=xxx>` — YouTube Music playlist
- [ ] `/play <music.youtube.com/browse/MPRExxx>` — YouTube Music album (should queue all tracks)
- [ ] `/play <spotify track url>` — should resolve to YouTube and play
- [ ] `/play <spotify playlist url>` — should queue all tracks
- [ ] `/play <spotify album url>` — should queue all tracks
- [ ] `/pause` — audio should pause, player button should update
- [ ] `/resume` — audio should resume
- [ ] `/stop` — should stop playback, clear queue, and disconnect

## 2. Queue Management

- [ ] `/queue` — should show the current queue with track numbers
- [ ] `/shuffle` — shuffle the queue, verify order changed
- [ ] `/loop` three times (off → single → queue) — verify each mode:
    - **single**: same track repeats
    - **queue**: after last track, loops back to first
    - **off**: stops after last track (or triggers autoplay)
- [ ] `/remove <position>` — remove a track by position, verify it's gone from `/queue`
- [ ] `/move <from> <to>` — move a track, verify new position in `/queue`
- [ ] `/move 1 1` — same position, should say "already at that position"
- [ ] `/skipto <position>` — should skip to that track, discarding tracks before it
- [ ] `/clear` — should clear queue but keep current track playing
- [ ] `/maxqueue <number>` — set max, then try to exceed it with `/play`
- [ ] `/undo` — should undo the last queue mutation (remove/move/skipto/clear/shuffle)

## 3. Transport & Seeking

- [ ] `/skip` — should advance to next track
- [ ] `/back` — should play the previous track
- [ ] `/seek 1:30` — should seek to 1:30
- [ ] `/seek 90` — should seek to 90 seconds
- [ ] Player embed seek bar — click a segment, should seek to that time
- [ ] Player embed buttons — test prev, rewind 10s, pause/resume, forward 10s, next
- [ ] `/replay` — should restart the current track from the beginning
- [ ] `/voteskip` — if alone in VC, should auto-skip. With 2+ people, should start a vote

## 4. Audio Effects

- [ ] `/filter bassboost` → listen for bass boost → `/filter none` to reset
- [ ] `/filter nightcore` — should sound sped up and higher pitch
- [ ] `/filter vaporwave` — should sound slowed down
- [ ] `/filter 8d` — should hear panning effect
- [ ] `/filter karaoke` — vocals should be reduced
- [ ] `/speed 1.5` — should play faster → `/speed 1.0` to reset
- [ ] `/normalize` — toggle on, volume should level out
- [ ] `/volume 50` → `/volume 100` — verify volume changes
- [ ] Player volume buttons (speaker emoji row) — verify they adjust volume
- [ ] `/eq bass_heavy` — should boost bass → `/eq flat` to reset
- [ ] `/eqcustom` with custom band values

## 5. Search

- [ ] `/search <query>` — should use the default search provider
- [ ] `/youtube-search <query>` — should show YouTube results with a track selection menu
- [ ] `/spotify-search <query>` — should show Spotify results with a track selection menu
- [ ] `/searchmode` — toggle between YouTube and Spotify, verify `/search` uses the new default
- [ ] Pick a result from the search view — should queue and play it

## 6. Now Playing & Lyrics

- [ ] `/nowplaying` — should show embed with progress bar, thumbnail, requester
- [ ] `/player` — should send/refresh the interactive PlayerView
- [ ] `/lyrics` — should show lyrics for the current track
- [ ] `/lyrics <song name>` — should show lyrics for a specific song
- [ ] Verify the PlayerView auto-updates every ~10 seconds (progress bar moves)

## 7. Favorites

- [ ] `/fav` — save the current track to favorites
- [ ] `/favs` — list your favorites, verify the track is there
- [ ] `/fav` again on same track — should say already in favorites
- [ ] `/playfavs` — should queue all your favorites
- [ ] `/unfav <position>` — remove a favorite by position
- [ ] `/favs` — verify it's removed

## 8. Playlists

- [ ] `/playlist save <name>` — save the current queue as a playlist
- [ ] `/playlist list` — should show the saved playlist
- [ ] `/playlist load <name>` — should queue all tracks from the playlist
- [ ] `/playlist delete <name>` — should delete the playlist

## 9. Similar / Radio / Autoplay

- [ ] `/similar` — should show 5 similar tracks from related artists (pick one to queue)
- [ ] `/radio <artist name>` — should start radio mode, queueing similar tracks automatically
- [ ] `/radio-off` — should stop radio mode
- [ ] `/autoplay` — toggle on, let the queue empty naturally — bot should auto-queue a similar track
- [ ] `/autoplay` — toggle off

## 10. History & Stats

- [ ] `/top` — should show the 10 most played tracks in the server
- [ ] `/stats` — should show server listening stats
- [ ] `/mystats` — should show your personal listening stats
- [ ] `/rate` — rate the current track (thumbs up/down buttons)
- [ ] `/toprated` — should show highest rated tracks

## 11. Queue Import/Export

- [ ] `/queue-export` — should give you a code
- [ ] `/queue-import <code>` — should import tracks from the code

## 12. DJ Mode

- [ ] `/dj <role>` — set a DJ role
- [ ] Have a non-DJ user try `/skip` — should be denied or require approval
- [ ] `/djmode` — enable DJ approval mode
- [ ] Have a non-DJ use `/play` — should send approval request to the channel
- [ ] Approve it from a DJ account — track should be queued
- [ ] `/djmode` and `/djclear` — reset

## 13. Per-User Limits

- [ ] `/maxperuser <number>` — set a low limit (e.g. 3)
- [ ] Try queueing more than 3 tracks — should be rejected after the limit
- [ ] `/maxperuser 0` — disable the limit

## 14. 24/7 Mode

- [ ] `/24-7` — toggle on
- [ ] Leave the VC — bot should stay connected
- [ ] Rejoin — bot should still be there
- [ ] `/24-7` — toggle off
- [ ] Leave the VC — bot should disconnect when left alone

## 15. Misc

- [ ] `/grab` — should DM you the current track info
- [ ] `/help` — should show the help embed
- [ ] Bot presence — verify it shows "Listening to {track}" while playing
- [ ] Bot presence — verify it clears when stopped
- [ ] Play a track, let it finish, verify it auto-advances to the next queued track
- [ ] Verify the player message **moves to the bottom** on track change (silent message)
- [ ] `/setnpchannel` — set a dedicated now-playing channel, verify embeds appear there
- [ ] `/clearnpchannel` — clear it

## 16. Edge Cases

- [ ] `/play` while not in a voice channel — should get "You need to be in a voice channel"
- [ ] `/skip` with nothing playing — should get an error message
- [ ] `/seek 99:99` on a 3-minute track — should handle gracefully
- [ ] `/remove 999` — should say invalid position
- [ ] Everyone leaves VC while music is playing (24/7 off) — bot should auto-disconnect
- [ ] Queue 50+ tracks to hit the default max — verify the limit message
- [ ] `/play` a YouTube Mix link (`list=RD...`) — should prompt with mix confirmation view


## 17. Interface checks

- [ ] Player — verify labeled transport buttons, pause/resume label, artwork, repeat/volume fields, and the next-track preview
- [ ] Seeking — verify the first timestamp is `0:00`; live streams have no time buttons and disabled rewind/forward
- [ ] Queue — check a single page, 7+ tracks, and an empty queue; verify positions remain correct after paging
- [ ] Search — select a track from the menu; verify it is added without replacing the current track
- [ ] Favorites / playlists / charts — page through long lists and verify no items are missing
- [ ] Lyrics — verify long lyrics stay in one paginated message and preserve line breaks
- [ ] Help — open every category and return to “Start here”
- [ ] Requests — verify pending, approved, declined, and expired cards; resolved cards must not change to expired
- [ ] Votes / ratings — verify counts update and timeout disables controls
- [ ] Settings / validation — verify consistent cards for confirmations and errors, including ephemeral replies
- [ ] Discord mobile / light theme — check readability, control labels, and page navigation on a real client
