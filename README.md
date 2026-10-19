# Kotome

Kodi client for [AudioBookShelf](https://www.audiobookshelf.org/). Browse and play audiobooks and podcasts from your ABS server with pitch-corrected tempo control, chapters, resume across devices, and per-book playback speed memory.

## Features

- Browse libraries, series, collections, authors, and podcasts from your ABS server.
- Pitch-corrected playback speed (0.5×–5×).
    - Does *not* require syncing playback to display.
- Per-book and per-podcast playback speeds.
- Use & set ABS resume points.
- Chapters: in the player, on the seek bar, from Next and Previous, and a list of the whole book's.
- Books in several files play as one.
- Sleep timer, with a volume fade-out and an optional screen action.

## Installation

1. Install via the Kontell [repository](https://github.com/kontell/repository.kontell).
2. Open Kotome's settings, enter your server address and Sign in.

## Supported platforms

| Platform | Kodi 21 (Omega) | Kodi 22 (Piers) | Kodi 23 |
|----------|----------------|-----------------|---------|
| Linux x86_64 | yes | yes | pre-release |
| Linux armv7 (Pi 2+) | yes | yes | pre-release |
| Linux aarch64 (Pi 3+) | yes | yes | pre-release |
| Android ARM32 | yes | yes | pre-release |
| Android ARM64 | yes | yes | pre-release |

Omega and Piers get the 1.x line from `main`, which uses [`inputstream.tempo`](https://github.com/kontell/inputstream.tempo) for its speed control.

Kodi 23 gets 1.90 and up from `master`, as pre-releases in the Kontell dev repository. They need the [kontell/xbmc](https://github.com/kontell/xbmc) consolidated build of Kodi 23, whose player has the tempo, chapters and fixes they rely on; stock Kodi 23 will not install them.

## Licence

MIT.
