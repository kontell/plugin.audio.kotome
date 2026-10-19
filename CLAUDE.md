# Kotome

AudioBookShelf client for Kodi. Pure Python add-on — `plugin.audio` with
`<provides>audio</provides>`.

This branch, `master`, is 1.90 and up: Kodi 23 with the kontell/xbmc
consolidated build only (`xbmc.python` 3.2.0). `main` is the Omega/Piers line on
VideoPlayer and inputstream.tempo, capped at 1.89.x.

## Kodi knowledge lives in kodi-drive

Shared Kodi knowledge is **not** in this file. Use the `kodi-drive:*` skills, or
read `../kodi-drive/README.md`. Directly relevant: `kodi-playback-resume`,
`kodi-paplayer`, `kodi-playback-tempo`, `kodi-playlist-position`,
`kodi-addon-manifest`, `kodi-idle-screensaver`, `kodi-python-runtime`,
`kodi-addon-release`.

**Do not add generally-useful Kodi findings here** — contribute them to kodi-drive.
This file holds only what is specific to *this* add-on.

Was `plugin.audio.koshelf` / "Koshelf" until 1.0.0. The name collided with an
unrelated project, and Koshelf is abandoned rather than upgraded: Kodi treats the
changed id as a different add-on, and nothing is migrated from it. There is
deliberately no migration code — it existed briefly and was removed.

## What it needs from Kodi

| kontell/xbmc | For |
|---|---|
| #15, #17 | PAPlayer tempo, 0.5–5 |
| #23 | a resume heard from the resume point, not the file's opening first |
| #24 | progress on song rows (`InfoTagMusic.setResumePoint`) |
| #30, #31, #32 | PAPlayer chapters, `InfoTagMusic.setChapters`, Estuary's music OSD |
| #33 | files longer than 12–13 hours |
| #34 | `StartPercent`'s fraction |

`setResumePoint` and `setChapters` are called only where they exist, so a build
without #24 or #31 loses the progress or the chapters and nothing else.

## Architecture

- `main.py` — plugin entry point, routing, library browsing, the playback routes
- `service.py` — ABS session sync, book time and chapter properties, sleep timer
- `playing.py` — playing a book in Kodi: the session file, the files as songs, the queue, the resolve, the chapter jump, and the player and tempo the service watches. Shared by `main.py` and the service, and takes nothing from `sys.argv`
- `playback.py` — book time, tracks and chapters across a book's files. No Kodi imports
- `abs_api.py` — AudioBookShelf REST API client
- `abs_auth.py` — address normalisation, the three auth calls, and the `Credentials` record. Deliberately dialog-free
- `abs_http.py` — stdlib HTTP transport with typed errors. No Kodi imports at all

## Interpreter reuse

`<reuselanguageinvoker>true</reuselanguageinvoker>`, in the `xbmc.addon.metadata` extension — it is parsed nowhere else and is silently ignored under `xbmc.python.pluginsource`. It takes bootstrap from ~0.4 s to zero on repeat invocations.

Two consequences that shape the code:

- **Anything derived from `sys.argv` must be re-read per invocation.** `_refresh_invocation()` does this for `HANDLE` and `BASE_URL`; frozen at import they would be stale on every call after the first.
- **Every route must close its handle**, including failure paths and unknown actions. With reuse the invoker thread parks instead of exiting, Kodi never marks the script done, and the caller waits in `CScriptRunner`'s first loop, which has no timeout at all. Library nodes, favourites and widgets are the callers that wait.

And for development: **a code change does not take effect until the add-on is bounced**, because the parked interpreter still holds the old modules. `tools/dev-install.sh` does the bounce.

## HTTP

`requests` is not used and must not come back. Inside Kodi's embedded Python its import costs 0.5–1.2 s (measured, 21.3 desktop), against 0.15–0.28 s for `http.client` + `ssl` + `json` + `urllib` together. A plugin invocation makes one to three calls and exits, so its connection pooling never repays that while the import is paid on every folder the user opens.

`abs_http` raises rather than returns: `Unauthorized` (401/403), `HttpError`, `Unreachable`. The distinction is load-bearing — an expired token and an absent server need opposite responses, and the old `except Exception` could not tell them apart.

## Authentication

AudioBookShelf 2.26+ model, read out of the server source rather than the docs, which still describe the old one:

- `POST /login` with `x-return-tokens: true` → `user.accessToken` (1 h) + `user.refreshToken` (30 d). Without the header the refresh token comes back only as a `Set-Cookie`, and a Kodi add-on has no cookie jar.
- `POST /auth/refresh` with `x-refresh-token: <token>` → new access token, rotated refresh token. Two installs sharing one account's refresh token sign each other out.
- `user.token` is the deprecated non-expiring token — `server/models/User.js` annotates it `// TODO: Old non-expiring token`. Read as a fallback from older servers, never minted. Stored with `expires_at = 0` so nothing tries to refresh it.

`ABSClient` answers a 401 with one refresh and one retry, and `start_playback()` refreshes ahead of time if under 30 minutes remain. The audio itself needs no token: it is the play session's track URL (below).

Credentials live in hidden add-on settings, not a file, so a settings `<dependency>` can gate the account rows on `logged_in` — a dependency can read a setting and cannot read a file. The password is asked for once and never stored.

## Playback

**PAPlayer plays everything.** Rows are songs (`InfoTagMusic`, media type `song`), because the listing row's tag picks the player: any video tag sends an item to VideoPlayer, and Kodi keeps the row's tag through the plugin resolve. There is no VideoPlayer path; 1.90 removed it once PAPlayer's chapters were confirmed on a device.

1. `route_play()` starts an ABS play session, and `playing.resolve()` writes the session file (`session.json`): the session's tracks (`playback.track_records`), chapters, the book's tempo, and what queued files need to resolve without the server.
2. It resolves the row to the **file that holds the resume position**, from that position, by `StartPercent` (six decimals, of that file).
3. The file's URL is `/public/session/<session id>/track/<index>`: ABS serves a session's tracks without a token while the session is open, so nothing in it expires during a long book.
4. `playing._queue_rest()` puts the book's later files behind it.

**A book of several files is one music playlist, one item per file.** PAPlayer's gapless transition joins them and keeps the tempo.

- Later files are queued as `?action=play_track&session_id=…&index=n`. `route_play_track` resolves one from the session file while its session is open, which is on Kodi's main thread shortly before the current file ends, so it makes no server call. A path from a closed session (Kodi's playlist history keeps them) starts a new session from the server's position instead, rebuilding the queue.
- Playing a row from a music window or a widget queues every row beside it (Play next song automatically). `_queue_rest` removes the others with JSON-RPC `Playlist.Remove`, which moves the player's index as rows before it go; `xbmc.PlayList.remove()` does not.
- Earlier files are not queued. A jump back goes through the Chapters row, which plays the book again from that file in the same session (`playing.play_from`). The service holds the session open across that stop (below).
- A book started outside a playlist (a favourite, PlayMedia, Play from the start) has nothing queued; the service plays its next file when one ends (`playing.play_next`).
- Kodi reports a file it opened by its resolved URL, and one it queued by the plugin path it was queued as. `playback.parse_track_url` reads both.

**What a stop means** is decided in one place, `playback.when_stopped`, which is unit-tested. The session file's `expect` is the file last asked for, and when: the resolve, `playing.play_from` and the service's `playing.play_next` write it. Until the service has heard that file, for up to 15 s, a silence or another file playing is a change of file. After that, a file that ended with nothing queued behind it, in a book with more, goes on to the next file. Anything else closes the session: a stop, the end of the book, or other media playing, which also clears the window properties and cancels the sleep timer.

Closing syncs the last second heard (`last_time`), or nothing if nothing of the session was heard: a failed start or a session file left by a killed Kodi has no position to give the server. A time in the book's first seconds does not count as heard while the start asked for (`expect.time`) is well past them (`playback.reached`), because the player can report the opening for a moment before it reaches a resume point.

**Book time** is `tracks[i].start + Player.getTime()`, with `i` from the file playing. `playback.locate` is the one reader: it gives a book time only while one of the session's own files plays, and none for anything else. ABS sync, the chapter properties, the sleep timer's end of chapter and the Chapters row all use it. Kodi's own time and seek bar are per file.

**Chapters.** Each file carries the server's chapters that fall in it, on the file's clock, through `setChapters`; a chapter that crosses a file boundary appears in both. A file inside one chapter carries that one, so PAPlayer, which shows none for fewer than two, does not fall back to the file's own. Where the server has none for a file (one library item holding several books), Kodi falls back to the file's own, and the service clears its chapter properties so a skin falls back with it.

**Tempo** is Kodi's (`Player.SetTempo`), not inputstream.tempo. `playing.TempoKeeper`, run by the service, applies the book's speed when its first file starts and whenever a file is *opened* rather than queued, since PAPlayer starts an opened file at 1.0. A tempo the listener chooses is adopted after two agreeing readings — as a file ends, the player reports 1.0 for a moment — and kept per book in `speeds.json` (per show, for podcasts).

**Listing resume**: in-progress rows carry `InfoTagMusic.setResumePoint(current, total)` and finished ones `setPlayCount(1)`. Listings are content `songs`: Estuary draws a song's progress only in its song list. Percentages must not go back into labels — a leading `[42%]` sorts before every plain title.

**Play from the start** is a context-menu `PlayMedia` with `from_start=1`; a resolve otherwise always resumes from the server's position.

## Window properties

`Kotome.NowPlaying.Title`, `Kotome.NowPlaying.Author`, `Kotome.Chapter`, `Kotome.ChapterCount`, `Kotome.ChapterName` (book-wide), `Kotome.Book.Time`, `Kotome.Book.Remaining` (at the tempo), `Kotome.SleepTimerRemaining`. These are a public interface — a skin can be keyed on them. The `Koshelf.*` names went in 1.90.

The root's Now playing, Chapters and Sleep timer rows are gated on `Kotome.NowPlaying.Title`, which the service sets while it tracks a session; it refreshes a Kotome listing when that changes.

## Sleep timer

`SLEEP_FILE` (`special://profile/sleep_timer`) is the source of truth: main.py writes the wall-clock end-time when a timer is set, the service polls for the file. `SleepModeController` in `service.py` owns the side effects.

The screen action uses **Kodi's own screensaver**, not an overlay. Earlier versions drew a full-screen `xbmcgui.WindowDialog`; that is gone and must not come back. A Python window outlives the script that created it, survives the add-on being disabled, and once ownerless the next Back press deadlocks Kodi's application thread inside `CScriptInvocationManager::Process()` — an unrecoverable whole-UI freeze that only `SIGKILL` ends. Reproduced on 21.3 with a gdb backtrace.

Two Kodi settings have to be borrowed and put back:

- `screensaver.mode` — an empty mode ("None", which most installs use) draws nothing.
- `screensaver.disableforaudio` — defaults to **true**, and with it set `ActivateScreenSaver` is a silent no-op during audio playback. Measured: `System.ScreenSaverActive` stayed false with it on and went true with it off, same call either way.

Both are recorded in `sleep_state.json` with the original volume. That file is the crash-recovery breadcrumb — if it exists at service start with no live `SLEEP_FILE`, the values are restored and it is deleted.

On expiry: stop playback, restore volume, **leave the screen dark**. That needs one extra step, because stopping playback deactivates the screensaver — so it is re-armed once, on the first poll after the stop. Once only: a poll that reactivates every tick fights the user for control of their own screen and they cannot win from a remote. The borrowed settings come back when the screensaver goes and stays inactive, debounced over `WAKE_CONFIRM_POLLS`. Not keyed on `getGlobalIdleTime()`, which JSON-RPC input does not move, so a wake condition built on it cannot be tested at all.

`sleep_screen_action` values: `none`, `screensaver_black`, `screensaver_dim`, `screensaver_user`, `screen_off`. `screen_off` is DPMS and is **Linux/X11 only** — Kodi has no DPMS implementation on Android, where the builtin returns without acting and without logging, so it falls back to the screensaver there.

End of chapter is the book-time chapter's end, at the tempo.

## Browsing

**One sorting system.** Library listings are sorted server-side, across every page, and register only `SORT_METHOD_UNSORTED`. Registering anything else silently overrides the sort picker: with `SORT_METHOD_TITLE` first, asking for "Recently added" returned the right page and Kodi re-sorted it alphabetically. The picker is a context-menu item, so it is on every row and every page.

**Facts go in info tags, not labels.** Which of the two a skin draws varies — Contuary's list view renders `ListItem.Title` and ignores the label entirely. A song has no field for the narrator, which leads the description (`Narrated by …`), and no cast: author photos are the row's artist art.

**Menu listings use an empty content type.** `setContent(handle, "files")` hands the listing to the skin's file view, which draws its own folder icon over the one the item carries. `CONTENT_MENU` is `""`, which is what plugin.video.kofin does for the same reason.

**Icons** are Kodi's own `Default*.png` names wherever one fits, because they resolve out of whichever skin is running and so match the user's theme. Check a candidate against a real skin before using it: `DefaultAddonAudio.png` draws a clapperboard and `DefaultTVShows.png` draws a television. Only what Kodi has no icon for is bundled, in `resources/icons/`, from Material Symbols via `tools/make-icons.py`, which crops to the ink and re-centres at `GLYPH_RATIO` so a bundled icon matches Kodi's padding.

**`ReloadSkin()` before believing a screenshot of an icon change.** Kodi's texture manager holds loaded textures for the session, and an add-on bounce does not touch them.

## Settings

New format (`settings version="1"`) with string IDs in `resources/language/resource.language.en_gb/strings.po`. Account, Playback, Sleep timer, then Advanced.

Two traps found the hard way:

- **The operator is `!is`, not `isnot`.** `isnot` logs `unknown operator` and discards the *whole* dependency, so the setting it guards shows unconditionally.
- **A new `30xxx` string id does not resolve after an add-on disable/enable bounce.** The settings dialog renders every new label blank until Kodi itself restarts.

## Build

```bash
tox                          # what CI gates on (black, compileall, unittest)
tools/build.py [OUTDIR]      # Kodi-installable zip (default ./dist)
tools/dev-install.sh         # rsync into ~/.kodi/addons, bounce the service
tools/make-icons.py          # regenerate resources/icons/ (needs inkscape)
tools/make-fanart.py         # regenerate resources/fanart.jpg (needs Pillow)
```

`tools/build.py` walks the tree and excludes dev-only paths rather than listing what to include, so a newly added resource cannot be silently absent from a release — which is how `resources/black.png` missed every published zip for the whole life of the feature that drew it.

`compileall` is a weak gate: it catches syntax and nothing else. Importing every module under Kodistubs catches the ordering and name errors it misses. Kodistubs 21 has neither `setResumePoint` nor `setChapters` on `InfoTagMusic`, which is one reason they are called behind `hasattr`.

There is no mypy gate: `tox.ini` records what adding it would take (start with `abs_http.py`).

GitHub Actions:

- `ci.yml` — every PR and push: `black`, `compileall`, an `assets` job that fails when a referenced resource is missing from the tree, and a `package` job uploading an installable PR zip.
- `release.yml` — on a `v*` tag: re-runs the gates, asserts the tag matches `addon.xml`, drafts a release. On `master` the draft is a **pre-release**, which repository.kontell serves on the `kodi23` dev channel only. `addon.xml` has no `<news>`, so the repository generator takes the release notes from `changelog.txt`.
- `notify-repo.yml` — tells `repository.kontell` about a published release. Publish the draft yourself; a release published by a workflow using the default `GITHUB_TOKEN` raises no event.
