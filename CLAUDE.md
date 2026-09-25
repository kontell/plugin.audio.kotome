# Kotome

AudioBookShelf client for Kodi. Pure Python add-on — `plugin.audio` with
`<provides>audio</provides>`.

## Kodi knowledge lives in kodi-drive

Shared Kodi knowledge is **not** in this file. Use the `kodi-drive:*` skills, or
read `../kodi-drive/README.md`.
Was `plugin.audio.koshelf` / "Koshelf" until 1.0.0. The name collided with an unrelated project, and Koshelf is abandoned rather than upgraded: Kodi treats the changed id as a different add-on, and nothing is migrated from it. There is deliberately no migration code — it existed briefly and was removed.

## Architecture

- `main.py` — plugin entry point, routing, library browsing, playback resolution
- `service.py` — background service for ABS session sync, chapter display, sleep timer, per-book speed tracking
- `abs_api.py` — AudioBookShelf REST API client
- `abs_auth.py` — address normalisation, the three auth calls, and the `Credentials` record. Deliberately dialog-free, which makes it the only module testable without a Kodi
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
- `POST /auth/refresh` with `x-refresh-token: <token>` → new access token, rotated refresh token.
- `user.token` is the deprecated non-expiring token — `server/models/User.js` annotates it `// TODO: Old non-expiring token`. Read as a fallback from older servers, never minted. Stored with `expires_at = 0` so nothing tries to refresh it.

Refreshing happens in two places: `ABSClient` answers a 401 with one refresh and one retry, and `start_playback()` refreshes ahead of time if under 30 minutes remain, because the stream URL carries the token and a book outlasts an hour easily.

Credentials live in hidden add-on settings, not a file, so a settings `<dependency>` can gate the account rows on `logged_in` — a dependency can read a setting and cannot read a file. The password is asked for once and never stored.

Directly relevant: `kodi-playback-resume` (which resume property each player core
takes, and in what units), `kodi-paplayer`, `kodi-addon-manifest`,
`kodi-idle-screensaver`, `kodi-python-runtime`, `kodi-addon-release`,
`kodi-inputstream`.

**Do not add generally-useful Kodi findings here** — contribute them to kodi-drive.
This file holds only what is specific to *this* add-on.

## Layout

| Path | |
|---|---|
| `main.py` | plugin entry point, routing, browsing, playback resolution |
| `service.py` | ABS session sync, chapter display, sleep timer, per-book speed |
| `abs_api.py` | AudioBookShelf REST client |

## Playback pipeline

1. `_resolve_playback()` creates an ABS play session, gets the stream URL and
   resume position.
2. Reads the `player` setting (0 = VideoPlayer, 1 = PAPlayer) and branches the
   ListItem setup.
3. Sets `inputstream` plus tempo properties (`tempo`, `tempo_file`, `start_time`)
   on both branches.
4. VideoPlayer branch: `VideoInfoTag` (mediaType `musicvideo`) + `StartOffset`
   (ms) + `ResumeTime`/`TotalTime` (s).
5. PAPlayer branch: `MusicInfoTag` + `audiobook_bookmark` (ms).
6. `setResolvedUrl()` hands off; the matching core opens the stream via
   `inputstream.tempo`.

**The `musicvideo` VideoInfoTag is what selects VideoPlayer**, not `<provides>`.
This add-on deliberately does *not* declare `<provides>video</provides>` — see
`kodi-addon-manifest` for what that would break.

Both cores route audio-only content to `WINDOW_VISUALISATION`; the visible
difference is which OSD infolabels populate. Details in `kodi-playback-resume`.

Requires `inputstream.tempo` 0.3.10+ (0.3.9 added VideoPlayer OSD content-time
tracking; 0.3.10 fixed a startup crash on FFmpeg-6 Kodi builds with Opus/webm).

## Sleep timer
`SLEEP_FILE` (`special://profile/sleep_timer`) is the source of truth: main.py writes the wall-clock end-time when a timer is set, the service polls for the file. `SleepModeController` in `service.py` owns the side effects.

The screen action uses **Kodi's own screensaver**, not an overlay. Earlier versions drew a full-screen `xbmcgui.WindowDialog`; that is gone and must not come back. A Python window outlives the script that created it, survives the add-on being disabled, and once ownerless the next Back press deadlocks Kodi's application thread inside `CScriptInvocationManager::Process()` — an unrecoverable whole-UI freeze that only `SIGKILL` ends. Reproduced on 21.3 with a gdb backtrace.

Two Kodi settings have to be borrowed and put back:

- `screensaver.mode` — an empty mode ("None", which most installs use) draws nothing.
- `screensaver.disableforaudio` — defaults to **true**, and with it set `ActivateScreenSaver` is a silent no-op during audio playback. Measured: `System.ScreenSaverActive` stayed false with it on and went true with it off, same call either way.

Both are recorded in `sleep_state.json` with the original volume. That file is the crash-recovery breadcrumb — if it exists at service start with no live `SLEEP_FILE`, the values are restored and it is deleted.

On expiry: stop playback, restore volume, **leave the screen dark**. That needs one extra step, because stopping playback deactivates the screensaver — so it is re-armed once, on the first poll after the stop. Once only: a poll that reactivates every tick fights the user for control of their own screen and they cannot win from a remote. The borrowed settings come back when the screensaver goes and stays inactive, debounced over `WAKE_CONFIRM_POLLS`. Not keyed on `getGlobalIdleTime()`, which JSON-RPC input does not move, so a wake condition built on it cannot be tested at all.

`sleep_screen_action` values: `none`, `screensaver_black`, `screensaver_dim`, `screensaver_user`, `screen_off`. `screen_off` is DPMS and is **Linux/X11 only** — Kodi has no DPMS implementation on Android, where the builtin returns without acting and without logging, so it falls back to the screensaver there. There is no CEC option: `peripheral.libcec` does not exist for Android and was not present on a stock desktop Kodi either.

## Playback pipeline

1. `_resolve_playback()` creates an ABS play session (refreshing the token first if it is close to expiring), gets stream URL + resume position
2. Sets `inputstream` + tempo properties (`tempo`, `tempo_file`, `start_time`)
3. `VideoInfoTag` (mediaType `musicvideo`) + `StartOffset` (ms) + `ResumeTime`/`TotalTime` (s)
4. `setResolvedUrl()` hands the ListItem to Kodi; VideoPlayer opens the stream via inputstream.tempo, which handles tempo processing

There is no PAPlayer path. It was a setting until 0.23.0 and is not supported.

`SLEEP_FILE` (`special://profile/sleep_timer`) is the source of truth: `main.py`
writes the wall-clock end time, the service polls for it.

`SleepModeController` in `service.py` owns the side effects. On the file appearing
it saves the current screensaver mode and volume to `sleep_state.json`, then
watches `getGlobalIdleTime()` against `sleep_idle_seconds`.

`sleep_screen_action` selects what fires after idle:
**Playback resume**: `StartOffset` (milliseconds). Kodi consumes it via `CFileItem::SetStartOffset` and queues a `SeekTime` after demuxer open.

Kotome also sets `inputstream.tempo.start_time` (seconds). The C++ addon uses it to (a) pre-populate `m_currentPts` so `GetTime()` reads the resume position before the seek executes, and (b) arm a player-agnostic initial-seek hold that gates `DemuxRead` output until any `SeekTime > 100 ms` arrives — without this hold, the audio sink can play ~50 ms of pts=0 audio from the stream start before the resume seek lands. Requires inputstream.tempo 0.3.10+.

**Listing resume**: in-progress items carry `InfoTagVideo.setResumePoint(current, total)`, and finished ones `setPlaycount(1)`. Skins draw their own in-progress indicator from it and Kodi offers Resume / Start from beginning. Percentages must not go back into labels — a leading `[42%]` sorts before every plain title.

## Browsing

**One sorting system.** Library listings are sorted server-side, across every page, and register only `SORT_METHOD_UNSORTED`. Registering anything else silently overrides the sort picker: with `SORT_METHOD_TITLE` first, asking for "Recently added" returned the right page and Kodi re-sorted it alphabetically. The picker is a context-menu item, so it is on every row and every page.

**Facts go in info tags, not labels.** Which of the two a skin draws varies — Contuary's list view renders `ListItem.Title` and ignores the label entirely, so narrator and duration formatted into a label were invisible there while the duration passed as a tag got its own right-aligned column.

**Menu listings use an empty content type.** `setContent(handle, "files")` hands the listing to the skin's file view, which draws its own folder icon over the one the item carries — every row came out as the same grey folder even with the art set correctly. `CONTENT_MENU` is `""`, which is what plugin.video.kofin does for the same reason.

**Icons** are Kodi's own `Default*.png` names wherever one fits, because they resolve out of whichever skin is running and so match the user's theme. Check a candidate against a real skin before using it: `DefaultAddonAudio.png` draws a clapperboard and `DefaultTVShows.png` draws a television. Only what Kodi has no icon for is bundled, in `resources/icons/`, from Material Symbols via `tools/make-icons.py`.

A bundled icon has to be padded to match. Kodi's Default icons put the glyph at ~52% of the canvas on its larger dimension; a Material Symbol rendered straight to PNG fills 75–92% and reads as oversized next to them. `make-icons.py` crops to the ink and re-centres at `GLYPH_RATIO`, so the source SVG's own padding stops mattering.

**`ReloadSkin()` before believing a screenshot of an icon change.** Kodi's texture manager holds loaded textures in memory for the session, and an add-on disable/enable bounce does not touch them — the list redraws with the *old* images, identical to the pixel, with nothing logged. Measuring that stale frame once produced a "the change did nothing" reading and a recalibration in the wrong direction. The texture *cache* (`Textures13.db`) is a separate thing and was not involved; `Textures.GetTextures` showed these files were never in it.

| Value | |
|---|---|
| `screensaver_black` / `screensaver_dim` | swap Kodi's screensaver and trigger it |
| `screen_off_cec` | `CECStandby` |
| `screen_off_android` | DPMS toggle |
| `none` | nothing |

Fires once per idle period, re-arming on interaction. Volume ramps to zero over
`sleep_rampdown_seconds`, backing off if the user adjusts it mid-ramp.
Speed settings (step, min, max) are written as JSON to `special://temp/inputstream_tempo_config.<OWNER>`. inputstream.tempo's `speed.py` reads this for keyboard/dialog stepping. Per-book speeds are stored in `speeds.json` in the addon profile.

On expiry: stop, restore volume and screensaver, leave the screen dark. On cancel
or playback stop: restore everything including waking the screen.

**`sleep_state.json` is the crash-recovery breadcrumb** — if it exists at service
start with no live `SLEEP_FILE`, restore the saved values. `kodi-idle-screensaver`
explains why that file is not optional.
`special://temp/inputstream_tempo_active` exists while tempo is the active inputstream. It names the owning add-on and its rate/config files, so the tempo keymap acts on ours and not another add-on's — a patched YouTube drives the same add-on, and on shared paths an audiobook at 2.0x and a video at 1.5x overwrote each other's rate. The sentinel itself stays shared: it is the single "the keys are live" flag, and `_release_sentinel()` only removes it if we are the named owner.

Controls whether speed.py keys/dialog are active, whether runner.py sets the `InputstreamTempo.Active` window property, and whether the "Now playing" root menu item appears.

## Window properties

`Kotome.NowPlaying.Title`, `Kotome.NowPlaying.Author`, `Kotome.ChapterName`, `Kotome.SleepTimerRemaining`. These are a public interface — a skin can be keyed on them. The `Koshelf.*` names are written alongside for the whole 1.x line and go in 2.0.

## Cross-add-on wiring

- `special://temp/inputstream_tempo_config` — speed step/min/max as JSON, read by
  `inputstream.tempo`'s `speed.py`.
- `special://temp/inputstream_tempo_active` — sentinel; gates `speed.py`'s keys,
  `runner.py`'s `InputstreamTempo.Active` window property, and the "Now playing"
  root menu item.
- `speeds.json` in the add-on profile — per-book speeds.
New format (`settings version="1"`) with string IDs in `resources/language/resource.language.en_gb/strings.po`. Account, Playback, Sleep timer, then Advanced.

Two traps found the hard way:

- **The operator is `!is`, not `isnot`.** `isnot` logs `unknown operator` and discards the *whole* dependency, so the setting it guards shows unconditionally.
- **A new `30xxx` string id does not resolve after an add-on disable/enable bounce.** The settings dialog renders every new label blank until Kodi itself restarts, which is what clears the add-on string cache.

## Build

```bash
tox                       # what CI gates on: black, compileall
tools/build.py [OUTDIR]   # Kodi-installable zip, default ./dist
tools/dev-install.sh      # rsync into the addons dir, bounce the service
```

`tools/build.py` walks the tree and excludes dev-only paths. It replaced a
`build.sh` that copied a hand-written list of seven paths — `resources/black.png`,
which the sleep timer's screen-off overlay draws, had never been in a published
zip. `kodi-addon-release` covers why include-lists fail this way.

No mypy gate: strict over the three modules reports ~104 errors, nearly all
missing annotations across `main.py`'s 1200 lines. `tox.ini` records what adding
it would take — start with `abs_api.py`.
tox                          # what CI gates on (black, compileall)
black --check --diff .
tools/build.py [OUTDIR]      # Kodi-installable zip (default ./dist)
tools/dev-install.sh         # rsync into ~/.kodi/addons, bounce the service
tools/make-icons.py          # regenerate resources/icons/ (needs inkscape)
tools/make-fanart.py         # regenerate resources/fanart.jpg (needs Pillow)
```

`tools/build.py` walks the tree and excludes dev-only paths rather than listing what to include, so a newly added resource cannot be silently absent from a release — which is how `resources/black.png` missed every published zip for the whole life of the feature that drew it.

`compileall` is a weak gate: it catches syntax and nothing else. Importing every module under Kodistubs catches the ordering and name errors it misses, and has already caught one (`_ACTION_ICONS` referencing a constant defined below it).

There is no mypy gate: `tox.ini` records what adding it would take (start with `abs_http.py` — it is the smallest and the only module with no Kodi imports).

Workflows: `ci.yml` (black, compileall, an `assets` job that fails on a referenced
resource missing from the tree, and a PR zip), `release.yml`, `notify-repo.yml`.

`release.yml` warns when `addon.xml`'s hand-maintained `<news>` does not mention
the version being released — because `<news>` is present, the repo generator will
not fall back to `changelog.txt`, so a stale one advertises the wrong notes.
