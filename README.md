# Kotome

Kodi client for [AudioBookShelf](https://www.audiobookshelf.org/). Browse and
play audiobooks and podcasts from your ABS server with pitch-corrected tempo
control, resume across devices, and per-book playback speed memory.

## Features

- Browse libraries, series, collections, authors, and podcasts from your ABS
  server.
- Pitch-corrected playback speed (0.5×–5×) via
  [`inputstream.tempo`](https://github.com/kontell/inputstream.tempo).
    - Does *not* require syncing playback to display.
- Per-book and per-podcast playback speeds.
- Use & set ABS resume points.
- Sleep timer, with a volume fade-out and an optional screen action.
- Chapters available with [Contuary](https://github.com/kontell/skin.contuary) integration.

## Installation

1. Install the Kontell repository:
   [`repository.kontell`](https://github.com/kontell/repository.kontell).
2. From the repository, install:
   - **inputstream.tempo**
   - **Kotome**
3. Open Kotome's settings, enter your server address under and press
   **Sign in**. Your password is exchanged for a token and is not stored.

## Settings worth knowing

- *Sleep timer → Screen action.* The screensaver options borrow Kodi's own
  screensaver and hand your settings back afterwards. *Turn the display off*
  uses DPMS, which only exists on Linux/X11 — on deviceid the screensaver is
  used instead.
- *General → Verify HTTPS certificate.* Turn this off only for a server
  behind a self-signed certificate.
- *Playback → Remember speed per book/podcast.* On by default.

## Supported platforms

| Platform | Kodi 21 (Omega) | Kodi 22 (Piers) |
|----------|----------------|-----------------|
| Linux x86_64 | yes | yes |
| Linux armv7 (Pi 2+) | yes | yes |
| Linux aarch64 (Pi 3+) | yes | yes |
| Android ARM32 | yes | yes |
| Android ARM64 | yes | yes |

## Licence

MIT.
