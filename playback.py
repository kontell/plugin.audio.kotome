"""Book time, tracks and chapters: the arithmetic of a book played file by file.

No Kodi imports, so it is testable off a Kodi box. ABS gives a play session's
audio tracks with `startOffset` and `duration` in book time, and its chapters
with `start` and `end` in book time. Kodi plays one track at a time and reports
time within it.
"""

import re
from urllib.parse import parse_qs, urlsplit

# A track's URL names the session and the track's index in it:
# /public/session/<session id>/track/<index>
_TRACK_URL = re.compile(r"/public/session/([^/?#]+)/track/(\d+)")


def track_records(audio_tracks):
    """What a session file keeps of each track, in book order."""
    records = []
    for track in audio_tracks or []:
        records.append(
            {
                "index": int(track.get("index") or len(records) + 1),
                "start": float(track.get("startOffset") or 0),
                "duration": float(track.get("duration") or 0),
                "mime": track.get("mimeType") or "",
            }
        )
    records.sort(key=lambda t: t["start"])
    return records


def track_at(tracks, book_time):
    """Position in `tracks` of the track that plays at `book_time`.

    A time past the end is the last track's; one before the start, the first's.
    """
    position = 0
    for i, track in enumerate(tracks):
        if track["start"] <= book_time:
            position = i
        else:
            break
    return position


def start_percent(track, book_time):
    """How far into `track` `book_time` is, as a percentage, or 0 for its start.

    PAPlayer resumes from a resolved item's StartPercent, which survives the
    plugin resolve where StartOffset does not.
    """
    duration = track["duration"]
    offset = book_time - track["start"]
    if duration <= 0 or offset <= 0:
        return 0.0
    return min(100.0, offset * 100.0 / duration)


def track_url(server_url, session_id, index):
    """ABS serves a session's tracks without a token while the session is open."""
    return "{}/public/session/{}/track/{}".format(
        server_url.rstrip("/"), session_id, index
    )


def parse_track_url(url):
    """(session id, track index) of the file playing, or (None, None).

    Kodi reports a file it opened by its resolved URL, and one it queued
    behind another by the plugin path it was queued as, so both are read.
    """
    match = _TRACK_URL.search(url or "")
    if match:
        return match.group(1), int(match.group(2))
    query = parse_qs(urlsplit(url or "").query)
    if query.get("action") == ["play_track"]:
        try:
            return query["session_id"][0], int(query["index"][0])
        except (KeyError, ValueError):
            pass
    return None, None


def track_by_index(tracks, index):
    """Position in `tracks` of the track with ABS index `index`, or None."""
    for i, track in enumerate(tracks):
        if track["index"] == index:
            return i
    return None


def track_chapters(chapters, track):
    """The book's chapters that fall in `track`, on the track's own clock.

    `[(start, end, title)]` in seconds from the start of the file, clipped to
    it. A chapter that crosses a file boundary appears in both files.
    """
    start = track["start"]
    end = start + track["duration"]
    out = []
    for chapter in chapters or []:
        c_start = float(chapter.get("start") or 0)
        c_end = float(chapter.get("end") or 0)
        if c_end <= start or c_start >= end:
            continue
        out.append(
            (
                max(c_start, start) - start,
                min(c_end, end) - start,
                chapter.get("title") or "",
            )
        )
    return out


def chapter_at(chapters, book_time):
    """(position from 1, chapter) of the chapter playing at `book_time`.

    (0, None) when there are no chapters or the time falls in none of them.
    """
    for i, chapter in enumerate(chapters or []):
        if (
            float(chapter.get("start") or 0)
            <= book_time
            < float(chapter.get("end") or 0)
        ):
            return i + 1, chapter
    return 0, None


def clock(seconds):
    """h:mm:ss, or m:ss under an hour."""
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "{}:{:02d}:{:02d}".format(hours, minutes, secs)
    return "{}:{:02d}".format(minutes, secs)
