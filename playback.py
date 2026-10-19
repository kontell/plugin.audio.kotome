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
        index = track.get("index")
        records.append(
            {
                "index": int(index) if index is not None else len(records) + 1,
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


def locate(tracks, session_id, playing_url, file_seconds):
    """(position in `tracks`, book time) of the file playing, or (None, None)
    when it is not one of session `session_id`'s files.

    Kodi's time is within the file playing; book time adds the file's start.
    """
    playing_id, index = parse_track_url(playing_url)
    if playing_id is None or playing_id != session_id:
        return None, None
    position = track_by_index(tracks, index)
    if position is None:
        return None, None
    return position, tracks[position]["start"] + file_seconds


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


def reached(book_time, asked):
    """Whether playback at `book_time` counts as heard, for a start asked for
    at book time `asked`.

    A player can report the opening of a book for a moment before it has gone
    to a resume point, so its first seconds do not count while the start
    asked for is well past them.
    """
    return not (book_time < 5 and asked > 30)


# How long a file asked for has to start before a silence, or something else
# playing, is taken at its word.
OPEN_GRACE = 15


def when_stopped(session, heard, now, ended=False, grace=OPEN_GRACE):
    """What to do while none of this session's files plays: "wait", "next" or
    "close".

    `heard` maps a track index to when the service last heard that file play.
    `session["expect"]` is the file last asked for, and when: by the resolve,
    by a jump to another file, or by the service playing the next. Until that
    file has been heard, for up to `grace` seconds, the silence or the other
    file is the change of file: "wait". A file that ended with nothing queued
    behind it, in a book with more: "next". Anything else ends the session.
    """
    expect = session.get("expect")
    if (
        expect
        and now - expect["at"] < grace
        and heard.get(expect["index"], 0) < expect["at"]
    ):
        return "wait"
    position = session.get("position")
    if (
        ended
        and not session.get("queued")
        and position is not None
        and position + 1 < len(session.get("tracks") or [])
    ):
        return "next"
    return "close"
