# Kotome

Kodi client for [AudioBookShelf](https://www.audiobookshelf.org/). Browse and
play audiobooks and podcasts from your ABS server with pitch-corrected tempo
control, resume across devices, and per-book playback speed memory.

> Kotome was called **Koshelf** until 1.0.0; the name collided with an
> unrelated project. Koshelf is no longer maintained. Kodi treats the new id
> as a separate add-on and nothing is carried across, so sign in once and set
> your preferences — then uninstall Koshelf whenever you like.

## Features

- Browse libraries, series, collections, authors, recently added books, and
  podcasts from your ABS server.
- Pitch-corrected playback speed (0.5×–5×) via
  [`inputstream.tempo`](https://github.com/kontell/inputstream.tempo).
    - Does *not* require syncing playback to display.
- Per-book and per-podcast playback speeds, remembered between sessions.
- Resume where ABS last had you. Position is synced back to the server while
  you listen, and in-progress books carry a real Kodi resume point — your skin
  draws its own progress indicator, and Kodi offers Resume or Start from
  beginning.
- Sorting is done by the server, so it covers the whole library rather than
  the page on screen. The picker is on the context menu of any item.
- The context menu also carries Hide watched, Mark as played / unplayed,
  and Reset resume position. These act on AudioBookShelf rather than on
  Kodi's own database, so the change follows you to your other devices.
- Sleep timer, with a volume fade-out and an optional screen action. It stops
  playback and leaves the screen dark, then puts your screensaver settings
  back when you return.

## Installation

1. Install the Kontell repository:
   [`repository.kontell`](https://github.com/kontell/repository.kontell).
2. From the repository, install:
   - **inputstream.tempo**
   - **Kotome**
3. Open Kotome. If you are not signed in, the root is a Sign in row that
   opens Settings. Under *General*, press **Find servers on local network**
   or type the address, then **Sign in**. Your password is exchanged for a
   token and is not stored.

## Settings worth knowing

- **Sleep timer → Screen action.** The screensaver options borrow Kodi's own
  screensaver and hand your settings back afterwards. *Turn the display off*
  uses DPMS, which only exists on Linux/X11 — on Android the screensaver is
  used instead.
- **General → Verify HTTPS certificate.** Turn this off only for a server
  behind a self-signed certificate.
- **General → Reuse language invoker.** On by default. Turn it off if several
  skin widgets load plugin:// paths at once. The change needs a Kodi restart.
- **Playback → Remember speed per book/podcast.** On by default.

Kotome ships fan art, but some skins hide add-on backdrops entirely — in
Contuary it is the *No fanart* setting.

## Supported platforms

| Platform | Kodi 21 (Omega) | Kodi 22 (Piers) |
|----------|----------------|-----------------|
| Linux x86_64 | yes | yes |
| Linux armv7 (Pi 2+) | yes | yes |
| Linux aarch64 (Pi 3+) | yes | yes |
| Android ARM32 | yes | yes |
| Android ARM64 | yes | yes |

Playback uses Kodi's VideoPlayer. The PAPlayer option earlier versions offered
is gone — it never worked without Kodi patches.

## Building

```bash
tox                     # what CI gates on: black, compileall
tools/build.py [OUTDIR] # Kodi-installable zip (default ./dist)
tools/dev-install.sh    # rsync into ~/.kodi/addons and bounce the service
```

`CLAUDE.md` carries the implementation notes — how the sleep timer borrows
Kodi's screensaver, why interpreter reuse constrains the routing, the
AudioBookShelf token model, and the Kodi behaviours that cost time to find.

## Licence

MIT.
