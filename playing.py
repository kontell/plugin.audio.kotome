"""Playing a book in Kodi: its session record, its files as songs, the queue,
the resolve, and the player and tempo the service watches.

main.py and service.py both use this. Nothing here is taken from `sys.argv`:
with the interpreter reused, the invocation's handle and path are passed in.
The arithmetic is in playback.py, which has no Kodi imports.
"""

import json
import os
import tempfile
import time
from urllib.parse import urlencode

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

import playback

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
PROFILE_DIR = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
SESSION_FILE = os.path.join(PROFILE_DIR, "session.json")
SPEEDS_FILE = os.path.join(PROFILE_DIR, "speeds.json")

# Kodi's tempo range (kontell/xbmc#17), in the steps its own OSD takes.
TEMPO_MIN = 0.5
TEMPO_MAX = 5.0
TEMPO_STEP = 0.05


# ── Kodi ──


def jsonrpc(method, params=None):
    """The result of a JSON-RPC call, or {} if it failed."""
    request = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        request["params"] = params
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as e:
        xbmc.log("Kotome: JSON-RPC error ({}): {}".format(method, e), xbmc.LOGWARNING)
        return {}
    result = response.get("result")
    return result if result is not None else {}


def active_player_id():
    players = jsonrpc("Player.GetActivePlayers") or []
    return players[0]["playerid"] if players else None


def tempo(player_id=None):
    """Kodi's tempo for what is playing, or None when nothing is."""
    if player_id is None:
        player_id = active_player_id()
    if player_id is None:
        return None
    result = jsonrpc(
        "Player.GetProperties", {"playerid": player_id, "properties": ["tempo"]}
    )
    return result.get("tempo")


def set_tempo(player_id, value):
    jsonrpc("Player.SetTempo", {"playerid": player_id, "tempo": value})


def tempo_steps():
    """Every tempo Kodi allows, in its OSD's steps."""
    count = int(round((TEMPO_MAX - TEMPO_MIN) / TEMPO_STEP))
    return [round(TEMPO_MIN + i * TEMPO_STEP, 2) for i in range(count + 1)]


# ── The session file ──


def load_session():
    """The record of the play session open, or None."""
    try:
        with open(SESSION_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_session(record):
    """Replace the session file whole: main.py and the service each read it
    while the other may be writing it."""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=PROFILE_DIR, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(record, f)
    os.replace(tmp, SESSION_FILE)


def clear_session():
    try:
        os.remove(SESSION_FILE)
    except OSError:
        pass


# ── Speed ──


def load_book_speed(item_id):
    """The speed kept for a book, or None."""
    try:
        with open(SPEEDS_FILE) as f:
            return json.load(f).get(item_id)
    except (OSError, ValueError):
        return None


def save_book_speed(item_id, speed):
    speeds = {}
    try:
        with open(SPEEDS_FILE) as f:
            speeds = json.load(f)
    except (OSError, ValueError):
        pass
    speeds[item_id] = speed
    os.makedirs(PROFILE_DIR, exist_ok=True)
    with open(SPEEDS_FILE, "w") as f:
        json.dump(speeds, f)


def _setting_float(setting_id, default):
    try:
        return float(ADDON.getSetting(setting_id))
    except (ValueError, TypeError):
        return default


def book_tempo(item_id, media_type):
    """The book's own speed if it has one, else the default for its kind.

    For podcasts the speed is kept per show, so it is shared by its episodes.
    """
    saved = None
    if ADDON.getSetting("per_book_speed") != "false":
        saved = load_book_speed(item_id)
    if saved is None:
        saved = _setting_float(
            "podcast_speed" if media_type == "podcast" else "book_speed", 1.0
        )
    return round(max(TEMPO_MIN, min(TEMPO_MAX, saved)), 2)


# ── The book's files ──


def session_record(session, item_id, episode_id, server, metadata):
    """What the service and later resolves need of an ABS play session.

    Queued files resolve from this rather than from the server: the next file
    is resolved on Kodi's main thread, shortly before the current one ends.
    `metadata` is what each file shows: title, album, author, description, art.
    """
    media_type = session.get("mediaType", "book")
    return {
        "session_id": session["id"],
        "server": server,
        "item_id": item_id,
        "episode_id": episode_id,
        "media_type": media_type,
        "duration": float(session.get("duration") or 0),
        "start_time": float(session.get("currentTime") or 0),
        "tracks": playback.track_records(session.get("audioTracks")),
        "chapters": session.get("chapters", []),
        "tempo": book_tempo(item_id, media_type),
        "queued": False,
        "media_metadata": metadata,
    }


def play_track_url(record, position):
    """The plugin path a queued file plays from, resolved when its turn comes."""
    args = {
        "action": "play_track",
        "item_id": record["item_id"],
        "session_id": record["session_id"],
        "index": record["tracks"][position]["index"],
    }
    if record.get("episode_id"):
        args["episode_id"] = record["episode_id"]
    return "plugin://{}/?{}".format(ADDON_ID, urlencode(args))


def track_item(record, position, path):
    """One of the book's files, as a song PAPlayer plays.

    Each file carries the book's chapters that fall in it, on the file's own
    clock (kontell/xbmc#31). A file inside one chapter carries that one:
    PAPlayer shows no chapters for it, where without any it would fall back to
    the file's own, which can disagree with the server's.
    """
    track = record["tracks"][position]
    meta = record["media_metadata"]
    li = xbmcgui.ListItem(meta["title"], path=path)
    li.setArt(meta["art"])
    tag = li.getMusicInfoTag()
    tag.setMediaType("song")
    tag.setTitle(meta["title"])
    if meta.get("author"):
        tag.setArtist(meta["author"])
    if meta.get("album"):
        tag.setAlbum(meta["album"])
    if len(record["tracks"]) > 1:
        tag.setTrack(position + 1)
    tag.setDuration(int(round(track["duration"])))
    if meta.get("description"):
        tag.setComment(meta["description"])
    chapters = playback.track_chapters(record["chapters"], track)
    if chapters and hasattr(tag, "setChapters"):
        tag.setChapters(chapters)
    return li


def _resolved_item(record, position):
    """The file itself: the session's track URL, which needs no token."""
    track = record["tracks"][position]
    url = playback.track_url(record["server"], record["session_id"], track["index"])
    li = track_item(record, position, url)
    li.setContentLookup(False)
    if track.get("mime"):
        li.setMimeType(track["mime"])
    return li


def _queue_rest(record, position, invoked):
    """Queue the book's later files behind the one starting, and only those.

    Playing a row from a music window or a widget queues every row beside it
    (Play next song automatically, on by default), so the next book in the
    listing would play when this one ends. The other rows go, and the book's
    later files take their place.

    `invoked` is the plugin path being resolved. Returns whether the files were
    queued. When the row is not the playlist's current item there is no queue
    to join (PlayMedia, a favourite), and the service starts each next file
    itself.
    """
    playlist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
    current = playlist.getposition()
    if not 0 <= current < playlist.size():
        return False
    if playlist[current].getPath().rstrip("/") != invoked.rstrip("/"):
        return False

    # JSON-RPC rather than PlayList.remove(): it moves the player's index down
    # as rows before it go, which remove() does not. It is posted to Kodi's
    # main thread, which is waiting for this resolve, so it lands afterwards,
    # in order, and after the add() below.
    for pos in reversed(range(playlist.size())):
        if pos != current:
            jsonrpc("Playlist.Remove", {"playlistid": 0, "position": pos})
    for later in range(position + 1, len(record["tracks"])):
        li = track_item(record, later, play_track_url(record, later))
        li.setProperty("IsPlayable", "true")
        playlist.add(play_track_url(record, later), li)
    # A book plays once, in order.
    xbmc.executebuiltin("PlayerControl(RandomOff)")
    xbmc.executebuiltin("PlayerControl(RepeatOff)")
    return True


def resolve(handle, record, book_time, invoked):
    """Resolve the row to the file that holds `book_time`, from there.

    A book of several files plays as one playlist with one item per file;
    PAPlayer's gapless transition joins them, and keeps the tempo.
    """
    position = playback.track_at(record["tracks"], book_time)
    li = _resolved_item(record, position)
    # PAPlayer resumes from a resolved item's StartPercent; a StartOffset set
    # here does not survive the resolve. kontell/xbmc#34 keeps its fraction.
    li.setProperty(
        "StartPercent",
        "{:.6f}".format(playback.start_percent(record["tracks"][position], book_time)),
    )
    record["queued"] = _queue_rest(record, position, invoked)
    save_session(record)
    xbmcplugin.setResolvedUrl(handle, True, li)


def resolve_queued(handle, record, position):
    """A queued file, resolved from the session's record."""
    xbmcplugin.setResolvedUrl(handle, True, _resolved_item(record, position))


def play_from(record, position, book_time):
    """Play the book from `book_time` in file `position`, in the open session."""
    playlist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
    playlist.clear()
    for later in range(position, len(record["tracks"])):
        li = track_item(record, later, play_track_url(record, later))
        li.setProperty("IsPlayable", "true")
        if later == position:
            # An offset on the played item itself, unlike one on a resolved
            # item, is honoured: in seconds.
            li.setProperty(
                "StartOffset", str(book_time - record["tracks"][position]["start"])
            )
        playlist.add(play_track_url(record, later), li)
    record["queued"] = True
    # Kodi stops the file playing before it opens this one; the service must
    # not take that for the end of the book.
    record["switched_at"] = time.time()
    save_session(record)
    xbmc.Player().play(playlist)


def play_next(record, position):
    """Start the file after `position` of a book Kodi is not queueing.

    A book started outside a playlist (a favourite, PlayMedia) has nothing
    queued behind its first file. Returns whether there was a next file.
    """
    tracks = record.get("tracks") or []
    if position is None or position + 1 >= len(tracks):
        return False
    xbmc.executebuiltin("PlayMedia({})".format(play_track_url(record, position + 1)))
    xbmc.log(
        "Kotome: playing file {} of {}".format(position + 2, len(tracks)),
        xbmc.LOGINFO,
    )
    return True


def go_to(record, book_time):
    """Play the book from `book_time`: a seek when the file playing holds it,
    otherwise the book again from the file that does, in the same session."""
    tracks = record["tracks"]
    position = playback.track_at(tracks, book_time)
    player = xbmc.Player()
    if locate(record, player)[0] == position:
        player.seekTime(book_time - tracks[position]["start"])
    else:
        play_from(record, position, book_time)


# ── What the service watches ──


def player_state(player=None):
    """(the file playing, seconds into it), or (None, None) when nothing is."""
    player = player or xbmc.Player()
    try:
        return player.getPlayingFile(), player.getTime()
    except RuntimeError:
        return None, None


def locate(record, player=None):
    """(position in the book's tracks, book time) of what is playing, or
    (None, None) when it is not one of the session's files."""
    url, seconds = player_state(player)
    return playback.locate(
        record.get("tracks") or [], record.get("session_id"), url, seconds
    )


class KotomePlayer(xbmc.Player):
    """Player events the service loop acts on, recorded here and handled there.

    The callbacks arrive on Kodi's callback thread; the loop owns the state.
    """

    def __init__(self):
        super().__init__()
        self.starting = False
        self.av_started = False
        self.ended = False
        self.stopped = False

    def onPlayBackStarted(self):
        # A file opened. PAPlayer has reset the tempo to 1.0 for it, unless it
        # was queued behind the last one, so the tempo read until AV starts is
        # not the listener's.
        self.starting = True
        self.ended = False
        self.stopped = False

    def onAVStarted(self):
        self.av_started = True

    def onPlayBackEnded(self):
        self.ended = True

    def onPlayBackStopped(self):
        self.stopped = True

    def onPlayBackError(self):
        self.stopped = True


class TempoKeeper:
    """Keeps the book at its speed in Kodi's own tempo (kontell/xbmc#15).

    The book's speed is applied when its first file starts, and again whenever
    a file is opened rather than queued, since PAPlayer starts an opened file
    at 1.0. A change the listener makes, from the OSD or the keys, becomes the
    book's speed and is kept for it.
    """

    def __init__(self):
        self.wanted = None
        self.item_id = None
        self.last_saved = None
        self.seen = None

    def start(self, record):
        self.wanted = float(record.get("tempo") or 1.0)
        self.item_id = record.get("item_id")
        self.last_saved = self.wanted
        self.seen = None

    def apply(self):
        player_id = active_player_id()
        if player_id is None or self.wanted is None:
            return
        current = tempo(player_id)
        if current is not None and abs(current - self.wanted) > 0.001:
            set_tempo(player_id, self.wanted)
            xbmc.log("Kotome: tempo {:.2f}".format(self.wanted), xbmc.LOGINFO)

    def follow(self):
        """Adopt a tempo the listener chose, and keep it for the book."""
        player_id = active_player_id()
        if player_id is None:
            return
        current = tempo(player_id)
        if current is None or self.wanted is None:
            return
        if abs(current - self.wanted) <= 0.001:
            self.seen = None
        elif self.seen is None or abs(current - self.seen) > 0.001:
            # Read twice before it is believed: as a file ends, the player
            # reports 1.0 for a moment before it stops.
            self.seen = current
            return
        else:
            self.wanted = round(current, 2)
            self.seen = None
        if (
            self.item_id
            and ADDON.getSetting("per_book_speed") != "false"
            and self.wanted != self.last_saved
        ):
            save_book_speed(self.item_id, self.wanted)
            self.last_saved = self.wanted
